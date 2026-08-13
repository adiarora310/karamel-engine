#!/usr/bin/env python3
"""The model call, in one place.

Every generating component shelled out to `claude -p` on the operator's personal
Claude subscription. That is $0 marginal for one person and wrong the moment a
second one exists: it bills someone else's work to a personal seat, it cannot be
metered per customer, and it dies whenever that CLI session expires.

This replaces it with the Anthropic API on Karamel's own key.

FOUR THINGS THAT MATTER

1. The voice card is cached, not re-sent. It is ~20KB and rides every call, and
   a delivered draft costs four to six calls (maker, critic, up to two refine
   rounds). Sent as a plain prompt that is the same 20KB paid at full rate every
   time. Passed as a cached `system` block it is written once and read at about
   a tenth the price for an hour. Callers pass `system=` for anything stable and
   keep only the volatile part in the prompt; a byte of drift in the system text
   silently costs a full re-write, so it must not carry timestamps or ids.

2. Refusals are not exceptions. The API returns HTTP 200 with
   stop_reason="refusal" and possibly empty content, so code that reads
   content[0] breaks rather than handling it. Checked before the content is
   touched, and server-side fallbacks are on by default so a decline is retried
   on another model in the same call instead of surfacing as a failure.

3. Cost is recorded per tenant. "$0 marginal" stops being true here, and the
   first question a second customer raises is what they cost. Every call writes
   tokens and a price to a per-tenant ledger.

4. base_url is configurable. Day one points at Anthropic directly. Pointing it
   at a Karamel-run endpoint later is a config change, not a client change.

CLI:
  --check       validate config and print the resolved settings, no call
  --ping        one real minimal call, proves the key works
  --cost [T]    what a tenant has spent today
  --cost [T] --breakdown   where it went, by call type, with cache stats
  --selftest    pure-logic tests, never opens a socket
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from shared import CONFIG_DIR, append_jsonl, now_iso

LLM_CFG = CONFIG_DIR / "llm.json"

# Opus 5 unless a config says otherwise. Not downgraded for cost: the whole
# product is whether a draft sounds like a specific person, which is the axis a
# smaller model gives up first. Cost is controlled by caching the voice card and
# by `effort`, both of which are levers that do not trade away voice.
# Where the tokens come from.
#   "api"  Karamel's own Anthropic key. Metered, cached, survives unattended.
#   "cli"  the Claude Code subscription already signed in on THIS machine.
#          Zero marginal cost, and the only honest way for a second person to
#          run this on their own plan: a subscription is an entitlement on a
#          signed-in session, not an endpoint, so it can never be shared over a
#          network or proxied. Each install uses its own.
PROVIDERS = ("api", "cli")
DEFAULT_PROVIDER = "api"

# Where the CLI usually lands. PATH first; these are the fallbacks for a launchd
# agent, whose PATH is not a login shell's.
CLI_CANDIDATES = (
    "~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
)
CLI_TIMEOUT = 300

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000        # thinking + text share this ceiling on Opus 5
DEFAULT_EFFORT = "high"

# Per-call-type effort and ceiling, because one global setting priced every call
# as if it were the hardest one.
#
# Measured on the host, 2026-08-12: a $0.4913 run of three drafts was 77% output
# tokens, and the critic spent ~1,460 output tokens per call to produce a
# seven-line verdict. Almost all of that is thinking. The maker's thinking buys
# voice, which IS the product. The critic's buys a re-derivation of a rubric it
# is handed in full, and its job became mechanical when CLEAN started being
# computed from quoted evidence rather than judged.
#
# The critic is BACK AT HIGH, deliberately, after briefly running at medium.
#
# Medium looked good: two of three drafts passed at round 0 instead of one. But
# those were different drafts, so that result cannot distinguish "the maker got
# luckier" from "the critic got more lenient", and a slacker gate looks exactly
# like an improvement from the outside. The difference is about $3 a month. The
# gate being trustworthy is the entire product.
#
# The experiment to settle it is `critic.py --compare-effort`, which grades one
# draft at each level. Until somebody runs it, high is the answer.
PROFILES = {
    "maker":   {"effort": "high", "max_tokens": 16000},
    "critic":  {"effort": "high", "max_tokens": 16000},
    # Liveness checks. Nothing to think about.
    "ping":    {"effort": "low", "max_tokens": 1000},
    "doctor":  {"effort": "low", "max_tokens": 1000},
    "setup-check": {"effort": "low", "max_tokens": 1000},
}

# USD per million tokens, list price. Used only to record what a tenant costs;
# it is not billing. Cache reads are ~0.1x input and writes ~1.25x, which is the
# entire reason the voice card is a cached system block.
PRICING = {
    "claude-opus-5":   {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}


class LLMError(RuntimeError):
    """Anything that stopped a completion, with the reason a human needs."""


def load_config():
    """Settings from ~/.config/karamel/llm.json, env overriding.

    ANTHROPIC_API_KEY is honoured so a machine that already has one working
    does not need a second copy of the same secret on disk."""
    cfg = {
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "effort": DEFAULT_EFFORT,
        "base_url": None,
        "api_key": None,
        "claude_bin": None,
    }
    if LLM_CFG.exists():
        mode = LLM_CFG.stat().st_mode & 0o077
        if mode:
            raise LLMError(
                f"{LLM_CFG} is group/world readable ({oct(mode)}). It holds an "
                f"API key. Run: chmod 600 {LLM_CFG}"
            )
        try:
            cfg.update(json.loads(LLM_CFG.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            raise LLMError(f"{LLM_CFG} is unreadable: {e}") from e

    cfg["provider"] = str(cfg.get("provider") or DEFAULT_PROVIDER).lower()
    if cfg["provider"] not in PROVIDERS:
        raise LLMError(
            f"unknown provider {cfg['provider']!r} in {LLM_CFG}. "
            f"Use one of: {', '.join(PROVIDERS)}."
        )

    # The CLI needs no key at all, and demanding one was the thing that made
    # "run it on your own subscription" impossible without editing code.
    if cfg["provider"] == "cli":
        return cfg

    cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key")
    cfg["base_url"] = os.environ.get("ANTHROPIC_BASE_URL") or cfg.get("base_url")
    if not cfg["api_key"]:
        raise LLMError(
            f"No API key. Put one in {LLM_CFG} as {{\"api_key\": \"sk-ant-...\"}} "
            f"(chmod 600), or export ANTHROPIC_API_KEY. To use a Claude Code "
            f"subscription on this machine instead, set "
            f"{{\"provider\": \"cli\"}} and no key is needed."
        )
    cfg["api_key"] = str(cfg["api_key"]).strip()
    ok, problem = validate_key(cfg["api_key"])
    if not ok:
        raise LLMError(problem)
    return cfg


def validate_key(key):
    """(ok, problem). Cheap local checks, run before the key reaches an HTTP
    header.

    The console displays a key masked with bullet characters, and a bulleted
    string is what people copy when they select the text instead of pressing
    Copy. Those bullets are not ASCII, so httpx raised UnicodeEncodeError eight
    frames deep in _normalize_header_value: a stack trace about codecs for what
    is really "you pasted the wrong thing"."""
    if not key:
        return False, "the API key is empty"
    if not key.isascii():
        odd = sorted({c for c in key if not c.isascii()})[:3]
        shown = " ".join(repr(c) for c in odd)
        return False, (
            f"the API key contains non-ASCII characters ({shown}). That is "
            f"almost certainly the masked form the console displays. Use the "
            f"Copy button on the key rather than selecting the text."
        )
    if any(c.isspace() for c in key):
        return False, ("the API key contains a space or newline. Copy it again "
                       "without surrounding whitespace.")
    if not key.startswith("sk-ant-"):
        return False, (f"the API key does not start with 'sk-ant-' (it starts "
                       f"{key[:8]!r}). That is not an Anthropic API key.")
    if len(key) < 40:
        return False, (f"the API key is only {len(key)} characters, which is too "
                       f"short to be real. It was probably truncated on paste.")
    return True, ""


def claude_bin(cfg=None):
    """Path to the Claude Code CLI, or raise with what to do about it."""
    import shutil

    cfg = cfg or {}
    explicit = cfg.get("claude_bin")
    if explicit:
        if Path(explicit).expanduser().exists():
            return str(Path(explicit).expanduser())
        raise LLMError(f"claude_bin is set to {explicit}, which does not exist")
    found = shutil.which("claude")
    if found:
        return found
    for cand in CLI_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    raise LLMError(
        "provider is 'cli' but the Claude Code CLI was not found on PATH or in "
        + ", ".join(CLI_CANDIDATES)
        + ". Install it and sign in, or set claude_bin in the config."
    )


class _CliUsage:
    """The shape record() reads. The CLI reports no token counts, so these are
    genuinely zero rather than estimated: a guessed number in a cost ledger is
    worse than an honest blank, because it will be quoted back as fact."""
    input_tokens = 0
    output_tokens = 0
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


def _complete_cli(prompt, system, cfg, tenant, label):
    """One `claude -p` call on this machine's subscription.

    The system block is folded into the prompt. The CLI has no cache_control, so
    there is no cached voice card here and nothing to keep byte-identical: the
    whole card is re-read every call. That costs nothing in dollars and does
    consume the subscription's own allowance faster."""
    full = f"{system}\n\n---\n\n{prompt}" if system else prompt

    # CLAUDE_* is stripped because this may itself be running inside Claude
    # Code, and an inherited session variable makes the child behave as part of
    # the parent's session rather than as a fresh headless call.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    try:
        r = subprocess.run(
            [claude_bin(cfg), "-p", full], capture_output=True, text=True,
            timeout=CLI_TIMEOUT, cwd="/tmp", env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude -p timed out after {CLI_TIMEOUT}s") from e
    except OSError as e:
        raise LLMError(f"could not run the Claude CLI: {e}") from e

    if r.returncode != 0:
        # The CLI reports its own failures on STDOUT, not stderr. Reading only
        # stderr produced "claude -p failed: " with nothing after it, and made
        # a logged-out session undiagnosable for weeks.
        detail = (r.stderr.strip() or r.stdout.strip()
                  or f"exit {r.returncode}, no output")
        low = detail.lower()
        if "login" in low or "not logged in" in low or "unauthor" in low:
            raise LLMError(
                "the Claude Code subscription on this machine is not signed in. "
                "Run `claude` once and sign in. This is the failure mode of the "
                "cli provider: sessions expire, and nothing else announces it."
            )
        raise LLMError(f"claude -p failed: {detail[:300]}")

    out = r.stdout.strip()
    record(tenant, "claude-code-cli", _CliUsage(), label)
    if not out:
        raise LLMError("claude -p returned an empty response")
    return out


def _client(cfg):
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("the anthropic package is not installed: pip3 install anthropic") from e
    kwargs = {"api_key": cfg["api_key"]}
    if cfg.get("base_url"):
        kwargs["base_url"] = cfg["base_url"]
    return anthropic.Anthropic(**kwargs)


def price(model, usage):
    """USD for one call, from the usage object. Cache reads are billed at ~0.1x
    input and writes at ~1.25x, which is what makes the cached voice card worth
    the plumbing."""
    rate = PRICING.get(model)
    if rate is None:
        return None
    fresh = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        fresh * rate["input"] / 1e6
        + write * rate["input"] * 1.25 / 1e6
        + read * rate["input"] * 0.10 / 1e6
        + out * rate["output"] / 1e6
    )


def profile_for(label, cfg=None):
    """(effort, max_tokens) for one call type. Explicit arguments still win."""
    cfg = cfg or {}
    prof = dict(PROFILES.get(label) or {})
    prof.update((cfg.get("profiles") or {}).get(label) or {})
    return {
        "effort": prof.get("effort") or cfg.get("effort") or DEFAULT_EFFORT,
        "max_tokens": prof.get("max_tokens") or cfg.get("max_tokens")
                      or DEFAULT_MAX_TOKENS,
    }


def default_tenant():
    """Whose ledger a call lands in when the caller did not name one.

    Was the literal "unknown", and since not one caller passed a tenant, every
    call in the system landed there: `--cost adi` reported zero against a real
    bill. Per-tenant cost is one of the four reasons this module exists, and it
    had never once worked."""
    try:
        import tenants
        return tenants.LEGACY_TENANT
    except Exception:
        return os.environ.get("KARAMEL_OWNER") or "unknown"


def _ledger_path(tenant):
    return CONFIG_DIR / f"llm_spend_{tenant}.jsonl"


def record(tenant, model, usage, label):
    """Append one call to a tenant's spend ledger. Best effort: a ledger write
    must never be the thing that loses a finished draft."""
    try:
        append_jsonl(_ledger_path(tenant), {
            "ts": now_iso(),
            "label": label,
            "model": model,
            "input": getattr(usage, "input_tokens", 0),
            "output": getattr(usage, "output_tokens", 0),
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read": getattr(usage, "cache_read_input_tokens", 0),
            "usd": round(price(model, usage) or 0, 6),
        })
    except Exception as e:
        print(f"[spend ledger write failed: {e}]", file=sys.stderr)


def spend_today(tenant, date=None):
    """(calls, usd) for a tenant today."""
    from karamel_common import now_et

    day = date or now_et().date().isoformat()
    p = _ledger_path(tenant)
    if not p.exists():
        return 0, 0.0
    calls, usd = 0, 0.0
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (row.get("ts") or "").startswith(day):
            calls += 1
            usd += row.get("usd") or 0
    return calls, usd


def complete(prompt, system=None, tenant=None, label="", cfg=None,
             max_tokens=None, effort=None):
    """One completion. Returns the text.

    `system` is the stable part (a voice card) and is cached; `prompt` is the
    volatile part and is not. Splitting them is the whole cost story: the same
    20KB card either gets paid for on every call or written once and read at a
    tenth the price.

    Raises LLMError on a refusal, an empty response, or an API failure, so no
    caller ever has to distinguish "the model declined" from "it returned text".
    """
    cfg = cfg or load_config()
    tenant = tenant or default_tenant()
    if cfg.get("provider") == "cli":
        # effort and max_tokens have no equivalent on the CLI. Ignored rather
        # than faked, so nobody tunes a dial that is not connected to anything.
        return _complete_cli(prompt, system, cfg, tenant, label)
    client = _client(cfg)
    model = cfg["model"]
    prof = profile_for(label, cfg)
    max_tokens = max_tokens or prof["max_tokens"]
    effort = effort or prof["effort"]

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # A declined request is retried on another model inside the same call
        # rather than coming back as a failure. "default" routes by refusal
        # category, so there is no fallback model list to maintain here.
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
        "output_config": {"effort": effort},
    }
    if system:
        # The cache breakpoint. Anything that varies per call must stay out of
        # this block: one changed byte and the whole card is re-written at
        # 1.25x instead of read at 0.1x.
        kwargs["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]

    try:
        import anthropic
        resp = client.beta.messages.create(**kwargs)
    except anthropic.NotFoundError as e:
        raise LLMError(f"model {model!r} not found: {e}") from e
    except anthropic.AuthenticationError as e:
        raise LLMError(f"API key rejected: {e}") from e
    except anthropic.RateLimitError as e:
        raise LLMError(f"rate limited: {e}") from e
    except anthropic.APIStatusError as e:
        # Out of credit is the most common 400 on a new account and it is not a
        # code problem, so it gets its own sentence rather than a wall of JSON.
        # API credit is separate from a Claude subscription: having Pro or Max
        # does not put a balance on the API, which is exactly the assumption
        # that makes this error confusing the first time.
        if "credit balance is too low" in str(e):
            raise LLMError(
                "the Anthropic workspace has no API credit. This is separate "
                "from a Claude subscription: Pro or Max does not fund the API. "
                "Add credit at console.anthropic.com under Plans & Billing."
            ) from e
        raise LLMError(f"API error {e.status_code}: {e}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"could not reach the API: {e}") from e

    record(tenant, getattr(resp, "model", model), resp.usage, label)

    # Before touching content. A refusal is a 200 with a stop_reason and
    # possibly nothing in content at all, so reading content[0] first turns a
    # handled outcome into an IndexError.
    if resp.stop_reason == "refusal":
        detail = getattr(resp, "stop_details", None)
        cat = getattr(detail, "category", None) if detail else None
        raise LLMError(f"the model declined this request (category: {cat})")

    if resp.stop_reason == "max_tokens":
        # Every caller wants a complete artefact: a whole post, or a whole
        # seven-line verdict. Half of either is worse than a clean failure,
        # and a truncated verdict parses as a bad grade rather than as an error.
        raise LLMError(
            f"the response hit the {max_tokens} token ceiling for {label!r} and "
            f"is truncated. Raise max_tokens for this call type in llm.json, or "
            f"lower its effort so less of the budget goes to thinking."
        )

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if not text.strip():
        raise LLMError(
            f"empty response (stop_reason={resp.stop_reason}). If this is "
            f"max_tokens, thinking and text share the same ceiling on this model."
        )
    return text.strip()


# ------------------------------- tests ---------------------------------------

def selftest():

    # --- provider: cli --------------------------------------------------
    # The whole point is that this path needs no key. Demanding one is what
    # made "run it on your own subscription" impossible without editing code.
    import tempfile as _tf
    _saved = {k: os.environ.pop(k, None)
              for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
    _real_cfg = globals()["LLM_CFG"]
    try:
        with _tf.TemporaryDirectory() as d:
            cli = Path(d) / "llm.json"
            cli.write_text(json.dumps({"provider": "cli"}))
            cli.chmod(0o600)
            globals()["LLM_CFG"] = cli
            c = load_config()
            assert c["provider"] == "cli", c
            assert c["api_key"] is None, "the cli provider must not need a key"

            # An unknown provider is refused rather than silently treated as api.
            bad = Path(d) / "bad.json"
            bad.write_text(json.dumps({"provider": "carrier-pigeon"}))
            bad.chmod(0o600)
            globals()["LLM_CFG"] = bad
            try:
                load_config()
                raise AssertionError("unknown provider must be refused")
            except LLMError as e:
                assert "carrier-pigeon" in str(e), e

            # api stays the default, so nobody is switched by an upgrade.
            plain = Path(d) / "plain.json"
            plain.write_text(json.dumps({"api_key": "sk-ant-api03-" + "z" * 60}))
            plain.chmod(0o600)
            globals()["LLM_CFG"] = plain
            assert load_config()["provider"] == "api"

            # A missing binary names the fix instead of raising FileNotFoundError
            # somewhere downstream.
            try:
                claude_bin({"claude_bin": "/nope/claude"})
                raise AssertionError("a missing claude_bin must be refused")
            except LLMError as e:
                assert "does not exist" in str(e), e
    finally:
        globals()["LLM_CFG"] = _real_cfg
        for k, v in _saved.items():
            if v is not None:
                os.environ[k] = v

    # Per-call-type tuning. The critic must not silently inherit the maker's
    # budget: at effort=high it spent ~1,460 output tokens on a seven-line
    # verdict, which was 37% of a measured run.
    base = {"effort": "high", "max_tokens": 16000}
    assert profile_for("maker", base)["effort"] == "high"
    # The critic runs at the maker's setting until an A/B says otherwise. A
    # cheaper gate that passes more is not a cheaper gate.
    assert profile_for("critic", base)["effort"] == "high"
    # An unknown label falls back to the config, never to the cheapest setting:
    # a new call type must not be quietly downgraded because nobody listed it.
    assert profile_for("brand-new-thing", base) == base
    # An install can override without editing code.
    over = dict(base, profiles={"critic": {"effort": "low", "max_tokens": 999}})
    assert profile_for("critic", over) == {"effort": "low", "max_tokens": 999}
    # A partial override keeps the rest of the profile.
    part = dict(base, profiles={"critic": {"max_tokens": 6000}})
    assert profile_for("critic", part)["effort"] == "high"
    assert profile_for("critic", part)["max_tokens"] == 6000

    # Key validation, all of it local. Seen live on the host: the masked form
    # the console displays was pasted, and the failure surfaced as
    # UnicodeEncodeError from inside httpx's header encoder rather than as a
    # sentence about the key.
    masked = "sk-ant-P" + "\u2022" * 20
    ok, why = validate_key(masked)
    assert not ok and "non-ASCII" in why and "Copy button" in why, why
    assert not validate_key("")[0]
    assert not validate_key("sk-ant-" + "x" * 5)[0]              # too short
    assert not validate_key("hunter2" + "x" * 50)[0]             # wrong prefix
    assert not validate_key("sk-ant-api03-" + "x" * 40 + " ")[0] # inner space
    assert validate_key("sk-ant-api03-" + "x" * 60)[0]
    import tempfile

    global LLM_CFG
    real = LLM_CFG
    tmp = Path(tempfile.mkdtemp())
    # Both are cleared: this machine has them set, and a selftest that reads
    # the ambient environment passes or fails for reasons unrelated to the code.
    saved = {k: os.environ.pop(k, None)
             for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
    try:
        # No config and no env: says what to do, rather than KeyError-ing later.
        LLM_CFG = tmp / "absent.json"
        try:
            load_config(); raise AssertionError("missing key must raise")
        except LLMError as e:
            assert "ANTHROPIC_API_KEY" in str(e) and "chmod 600" in str(e)

        # A world-readable key file is refused, not warned about.
        loose = tmp / "loose.json"
        loose.write_text(json.dumps({"api_key": "sk-ant-api03-" + "x" * 60}))
        os.chmod(loose, 0o644)
        LLM_CFG = loose
        try:
            load_config(); raise AssertionError("loose perms must raise")
        except LLMError as e:
            assert "readable" in str(e)

        ok = tmp / "ok.json"
        ok.write_text(json.dumps({"api_key": "sk-ant-api03-" + "x" * 60}))
        os.chmod(ok, 0o600)
        LLM_CFG = ok
        cfg = load_config()
        assert cfg["model"] == "claude-opus-5", "must not silently downgrade"
        assert cfg["effort"] == "high"
        assert cfg["base_url"] is None, "day one talks to Anthropic directly"

        # base_url is a config change, not a client change: the whole point of
        # having it is that pointing at a Karamel endpoint later touches nothing
        # else.
        moved = tmp / "moved.json"
        moved.write_text(json.dumps(
            {"api_key": "sk-ant-api03-" + "k" * 60,
             "base_url": "https://llm.karamel.internal"}))
        os.chmod(moved, 0o600)
        LLM_CFG = moved
        assert load_config()["base_url"] == "https://llm.karamel.internal"

        # env overrides the file, so a box with a working key needs no second copy
        from_env = "sk-ant-api03-" + "e" * 60
        os.environ["ANTHROPIC_API_KEY"] = from_env
        assert load_config()["api_key"] == from_env
        # A key that fails validation is refused wherever it came from: the env
        # is not more trustworthy than the file.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-P" + "\u2022" * 20
        try:
            load_config()
            raise AssertionError("a masked key from the env must be refused")
        except LLMError as e:
            assert "non-ASCII" in str(e), e
        del os.environ["ANTHROPIC_API_KEY"]
    finally:
        LLM_CFG = real
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # --- pricing: the cached-card case is the one that pays for this module ---
    class U:
        def __init__(self, i=0, o=0, w=0, r=0):
            self.input_tokens, self.output_tokens = i, o
            self.cache_creation_input_tokens, self.cache_read_input_tokens = w, r

    assert price("unknown-model", U(1000)) is None

    card = 6000  # ~20KB voice card in tokens
    cold = price("claude-opus-5", U(i=card, o=400))
    warm = price("claude-opus-5", U(i=100, o=400, r=card))
    assert warm < cold, "a cache read must cost less than sending it fresh"
    # A read is ~a tenth of fresh input, so the saving is most of the card.
    assert cold - warm > (card * 5.00 / 1e6) * 0.8, (cold, warm)

    write = price("claude-opus-5", U(i=100, o=400, w=card))
    assert write > warm, "the first call writes the cache and costs more"

    # Output dominates on short posts, so effort and length are the other lever.
    assert price("claude-opus-5", U(o=1000)) == 25.00 / 1e3

    print("llm selftest: all assertions passed")


def breakdown(tenant, date=None):
    """Where a tenant's money actually went today, by call type.

    Two questions this answers and a total cannot. Which role is expensive,
    maker or critic, since they can be tuned independently. And whether the
    cache is working: a cached voice card should show large cache_read and
    near-zero fresh input on every call after the first."""
    from shared import read_jsonl

    day = date or now_iso()[:10]
    rows = [r for r in read_jsonl(_ledger_path(tenant))
            if str(r.get("ts", "")).startswith(day)]
    if not rows:
        return None
    by = {}
    for r in rows:
        b = by.setdefault(r.get("label") or "?",
                          {"n": 0, "usd": 0.0, "in": 0, "out": 0,
                           "cw": 0, "cr": 0})
        b["n"] += 1
        b["usd"] += r.get("usd") or 0
        b["in"] += r.get("input") or 0
        b["out"] += r.get("output") or 0
        b["cw"] += r.get("cache_write") or 0
        b["cr"] += r.get("cache_read") or 0
    return rows, by


def print_breakdown(tenant, date=None):
    got = breakdown(tenant, date)
    if got is None:
        print(f"{tenant}: nothing recorded today")
        return 0
    rows, by = got
    total = sum(b["usd"] for b in by.values())
    print(f"{tenant}: {len(rows)} call(s), ${total:.4f}\n")
    print(f"  {'label':10} {'n':>3} {'$':>9} {'out tok':>9} {'fresh in':>9} "
          f"{'cache rd':>9}  {'$/call':>8}")
    for label, b in sorted(by.items(), key=lambda kv: -kv[1]["usd"]):
        print(f"  {label:10} {b['n']:>3} {b['usd']:>9.4f} {b['out']:>9,} "
              f"{b['in']:>9,} {b['cr']:>9,}  {b['usd'] / b['n']:>8.4f}")
    out_tok = sum(b["out"] for b in by.values())
    in_tok = sum(b["in"] for b in by.values())
    cr = sum(b["cr"] for b in by.values())
    cw = sum(b["cw"] for b in by.values())
    rate = PRICING.get(DEFAULT_MODEL, {})
    out_usd = out_tok * rate.get("output", 0) / 1e6
    print(f"\n  output tokens are {out_usd / total * 100:.0f}% of the bill "
          f"(${out_usd:.4f} of ${total:.4f})" if total else "")
    if cr or cw:
        reused = cr / (cr + cw + in_tok) * 100 if (cr + cw + in_tok) else 0
        print(f"  cache: {cw:,} written, {cr:,} read, {reused:.0f}% of input "
              f"served from cache")
    else:
        print("  cache: NOTHING read. The voice card is being re-sent at full "
              "price on every call.")
    return 0


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    if "--cost" in sys.argv:
        i = sys.argv.index("--cost")
        tenant = sys.argv[i + 1] if i + 1 < len(sys.argv) else "adi"
        if "--breakdown" in sys.argv:
            return print_breakdown(tenant)
        calls, usd = spend_today(tenant)
        print(f"{tenant}: {calls} call(s) today, ${usd:.4f}")
        return 0

    try:
        cfg = load_config()
    except LLMError as e:
        print(f"llm config: NOT USABLE\n  {e}", file=sys.stderr)
        return 1

    if "--check" in sys.argv:
        print("llm config: ok")
        print(f"  provider   {cfg['provider']}")
        if cfg["provider"] == "cli":
            try:
                print(f"  claude     {claude_bin(cfg)} (subscription, $0)")
            except LLMError as e:
                print(f"  claude     NOT FOUND: {e}")
                return 1
            print("  note       no prompt caching and no token counts on the "
                  "CLI, and the\n             session can expire silently. "
                  "doctor --watch is what catches that.")
            return 0
        print(f"  model      {cfg['model']}")
        print(f"  effort     {cfg['effort']}")
        print(f"  max_tokens {cfg['max_tokens']}")
        print(f"  endpoint   {cfg['base_url'] or 'api.anthropic.com (default)'}")
        print(f"  key        ...{str(cfg['api_key'])[-6:]}")
        return 0

    if "--ping" in sys.argv:
        try:
            out = complete("Reply with exactly: ok", tenant="ping", label="ping",
                           cfg=cfg, max_tokens=1000)
        except LLMError as e:
            print(f"ping FAILED: {e}", file=sys.stderr)
            return 1
        print(f"ping ok: {out!r}")
        calls, usd = spend_today("ping")
        print(f"  recorded {calls} call(s), ${usd:.6f}")
        return 0

    print(__doc__.strip().split("CLI:")[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
