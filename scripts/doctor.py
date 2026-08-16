#!/usr/bin/env python3
"""Karamel doctor: what is broken, and the exact command that fixes it.

Two jobs, because there are two ways this system fails and they need opposite
designs.

ASKED: `./karamel doctor` runs every check and prints a remedy beside each
failure. Read-only, always. A diagnostic that repairs things destroys the
evidence it was run to collect, and this system's halt file doubles as the
record of a platform-enforcement tripwire.

UNASKED: `doctor.py --watch` runs on a schedule and tells the OPERATOR when
something broke. This is the half that matters once the software is on someone
else's Mac. Everything already writes to `~/.config/karamel/*.err`, and nobody
reads those; a failure on a machine you cannot see is indistinguishable from a
quiet day. Watch mode reports only what is NEW since the last run, so a
recurring error is one message rather than one every five minutes.

  python3 doctor.py                 run the checks, print a report
  python3 doctor.py --deep          also spend a cent proving the API key works
  python3 doctor.py --json          same checks, machine readable
  python3 doctor.py --watch         notify on new failures (for launchd)
  python3 doctor.py --selftest      pure-logic tests, no network
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from shared import (
    CONFIG_DIR, PROJECT, agent_plists, is_agent_line, owner_id,
)

# A check that fails here means no drafts at all, versus one that degrades
# something. The distinction drives both the exit code and whether watch mode
# wakes anybody up.
FATAL, WARN = "FATAL", "WARN"

STATE = CONFIG_DIR / "doctor_state.json"
# Two identical alerts an hour apart are a reminder. Two a minute apart are a
# reason to mute the channel, after which the next real failure is invisible.
REALERT_SECONDS = 6 * 3600
MAX_ALERT_CHARS = 1200


class Check:
    """One question with a yes/no answer and, when the answer is no, the
    single command that changes it. A finding without a remedy is trivia."""

    def __init__(self, name, ok, detail="", remedy="", level=FATAL):
        self.name, self.ok, self.detail = name, ok, detail
        self.remedy, self.level = remedy, level

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "remedy": self.remedy, "level": self.level}


def _owner():
    """Delegates to shared.owner_id: one resolution order for every caller.

    This was a local copy ending in a literal personal name, which shipped."""
    return owner_id()


# ------------------------------------------------------------------ the checks

def check_python():
    v = sys.version_info
    ok = v >= (3, 9)
    return Check("python", ok, f"{v.major}.{v.minor}.{v.micro}",
                 "Install Python 3.9 or newer." if not ok else "")


def check_key(deep=False):
    """The single most common cause of total silence, so it is checked first
    and, with --deep, checked by using it rather than by looking at it.

    Two providers to check, not one. On the cli provider there is no key at all
    and the thing that breaks is an expired sign-in, which announces itself
    nowhere."""
    try:
        import llm
        cfg = llm.load_config()
    except ImportError as e:
        return Check("model", False, f"cannot import llm: {e}",
                     "pip3 install anthropic")
    except Exception as e:                          # LLMError and friends
        return Check("model", False, str(e).split("\n")[0],
                     f"Fix {CONFIG_DIR / 'llm.json'}.")

    if cfg.get("provider") == "cli":
        try:
            binary = llm.claude_bin(cfg)
        except Exception as e:
            return Check("model", False, str(e).split("\n")[0],
                         "Install Claude Code and sign in, or switch the "
                         "provider back to api.")
        if not deep:
            return Check("model", True,
                         f"cli, {binary} (subscription, not called)")
        try:
            llm.complete("Reply with exactly: ok", tenant="doctor",
                         label="doctor", cfg=cfg)
        except Exception as e:
            return Check("model", False, f"cli call failed: {str(e)[:160]}",
                         "Run `claude` once on this machine and sign in.")
        return Check("model", True, "cli, live call succeeded (subscription)")

    if not deep:
        return Check("model", True,
                     f"{cfg['model']}, key ...{str(cfg['api_key'])[-6:]} (not called)")
    try:
        import llm
        llm.complete("Reply with exactly: ok", tenant="doctor", label="doctor",
                     max_tokens=1000, cfg=cfg)
    except Exception as e:
        detail = str(e).replace("\n", " ")[:160]
        remedy = "The key is present but not working. Check billing or rotate it."
        if "no API credit" in detail or "credit balance" in detail:
            remedy = ("Add credit at console.anthropic.com, Plans & Billing. "
                      "A Claude subscription does not fund the API.")
        elif "401" in detail or "authentication" in detail.lower():
            remedy = (f"The key is well-formed but rejected. Check it is the "
                      f"whole key and that the workspace has credit: "
                      f"{CONFIG_DIR / 'llm.json'}")
        return Check("model", False, f"live call failed: {detail}", remedy)
    return Check("model", True, f"{cfg['model']}, live call succeeded")


def check_tenant():
    try:
        import tenants
    except ImportError as e:
        return Check("tenant record", False, f"cannot import tenants: {e}", "")
    owner = _owner()
    t = tenants.load_tenant(owner)
    if t is None:
        return Check("tenant record", False, f"no tenant {owner!r}",
                     "./karamel setup")
    return Check("tenant record", True,
                 f"{t.name} ({t.id}), source={t.source}, tz={t.timezone}")


def check_voice_card():
    try:
        import tenants
        t = tenants.load_tenant(_owner())
    except Exception as e:
        return Check("voice card", False, f"cannot resolve tenant: {e}", "")
    if t is None:
        return Check("voice card", False, "no tenant", "./karamel setup")
    p = t.voice_card_path
    if not p.exists():
        return Check("voice card", False, f"missing: {p}", "./karamel voice")
    n = len(p.read_text())
    # A card short enough to fit in a tweet cannot carry a voice, and it is also
    # under the 512-token floor for the prompt cache, so it silently costs more.
    if n < 2000:
        return Check("voice card", False, f"only {n} bytes: {p.name}",
                     "./karamel voice", level=WARN)
    return Check("voice card", True, f"{n} bytes, {p.name}")


def check_channel():
    try:
        import tenants
        t = tenants.load_tenant(_owner())
    except Exception as e:
        return Check("delivery", False, f"cannot resolve tenant: {e}", "")
    if t is None:
        return Check("delivery", False, "no tenant", "./karamel setup")
    kind = (t.channel or {}).get("type", "none")
    if kind == "none":
        return Check("delivery", False, "no channel configured, drafts go nowhere",
                     "./karamel setup")
    cfg = CONFIG_DIR / ("telegram.json" if kind == "telegram" else "email.json")
    if kind in ("telegram", "email") and not cfg.exists():
        return Check("delivery", False, f"{kind} selected but {cfg.name} missing",
                     "./karamel setup")
    if cfg.exists() and (cfg.stat().st_mode & 0o077):
        return Check("delivery", False, f"{cfg} is readable by other users",
                     f"chmod 600 {cfg}")
    return Check("delivery", True, kind)


def check_halt():
    try:
        from shared import load_pause_state
        st = load_pause_state() or {}
    except Exception as e:
        return Check("halt state", False, f"unreadable: {e}", "")
    if not st.get("halt") and not st.get("paused"):
        return Check("halt state", True, "clear")
    # Whether a human did this or a tripwire did is the whole question, and the
    # field names are the only honest record of it. Never guess.
    if st.get("halt_reason") or st.get("halted_at"):
        why = str(st.get("halt_reason", "unknown"))
        kind = classify_halt(why)
        if kind == "infrastructure":
            return Check("halt state", False,
                         f"halted by a tripwire, but the reason is a local "
                         f"fault, not enforcement: {why.splitlines()[0][:160]}",
                         "Fix the fault, then clear the halt. Nothing here "
                         "suggests the platform did this.")
        return Check("halt state", False, f"HALTED by tripwire: {why[:200]}",
                     "Read the reason before clearing. This may be enforcement.")
    return Check("halt state", False,
                 f"stopped by hand ({st.get('set_by_command') or 'unknown command'})",
                 "./karamel start", level=WARN)


# Halts whose reason names a local fault. The tripwire fires on anything that
# stops a read, so "the browser was not there" and "the platform stopped us"
# arrive through the same field and read identically at 2am. They call for
# opposite responses: one is a five minute fix, the other is a reason to stop
# touching the account. The reason string distinguishes them and nothing else
# does, so it is worth reading rather than defaulting everything to the scary
# interpretation and training people to ignore it.
_INFRA_HALT = re.compile(
    r"CDP attach failed|connect_over_cdp|Protocol error|ECONNREFUSED|"
    r"Connection refused|localhost:\d+|websocket|Timeout \d+ms exceeded|"
    r"Chrome bot profile down|playwright", re.IGNORECASE)
# Anything actually suggesting the platform acted. Checked first: a message can
# contain both, and enforcement wins ties.
_ENFORCEMENT_HALT = re.compile(
    r"rate limit|429|suspend|restrict|locked|challenge|captcha|unusual activity|"
    r"logged out|login|verify your|automation detected", re.IGNORECASE)


def classify_halt(reason):
    """'enforcement', 'infrastructure', or 'unknown'."""
    if _ENFORCEMENT_HALT.search(reason or ""):
        return "enforcement"
    if _INFRA_HALT.search(reason or ""):
        return "infrastructure"
    return "unknown"


def check_playwright():
    """The reading half's one external dependency.

    listener, drafter and evaluator import it lazily inside their run, so a
    missing package is not a startup error anyone sees. It is an ImportError
    every twenty minutes, in a log file, on a machine nobody is watching, while
    the writing half keeps delivering and every other check here stays green.

    Only fatal when this tenant actually reads X. An install that writes
    originals and never scrapes does not need it, and reporting a missing
    package as a failure would teach the reader to ignore this report."""
    try:
        import tenants
        from safety import effective_reply_mining
        t = tenants.load_tenant(_owner())
        reading = bool(t and effective_reply_mining(t)[0])
    except Exception:
        reading = False
    try:
        import playwright  # noqa: F401
    except ImportError:
        if not reading:
            return Check("playwright", True,
                         "not installed, and not needed: this install does not "
                         "read X", level=WARN)
        return Check("playwright", False,
                     "not installed, so nothing can read your timeline",
                     "python3 -m pip install --user playwright")
    return Check("playwright", True,
                 "installed" + ("" if reading else ", though this install does "
                                                   "not read X"))


def check_agents():
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check("agents", False, f"launchctl unavailable: {e}", "")
    rows = [l.split() for l in r.stdout.splitlines() if is_agent_line(l)]
    if not rows:
        # Distinguish "never installed" from "installed but not running". The
        # remedy is different and the old message gave the same one for both.
        if agent_plists():
            return Check("agents", False,
                         f"{len(agent_plists())} plist(s) present, none loaded",
                         "./karamel start")
        return Check("agents", False, "nothing installed", "./karamel start")
    bad = [(c[-1], c[1]) for c in rows if len(c) >= 3 and c[1] not in ("0", "-")]
    if bad:
        detail = ", ".join(f"{lbl} exited {code}" for lbl, code in bad)
        return Check("agents", False, f"{len(rows)} loaded, {detail}",
                     "./karamel logs", level=WARN)
    return Check("agents", True, f"{len(rows)} loaded, last exit clean")


def check_recent_draft():
    """The only check that asks whether the product happened, rather than
    whether the machinery looks right. Everything can pass and still deliver
    nothing, which is the failure people actually notice."""
    try:
        import tenants
        t = tenants.load_tenant(_owner())
    except Exception as e:
        return Check("recent output", False, f"cannot resolve tenant: {e}", "")
    if t is None:
        return Check("recent output", False, "no tenant", "./karamel setup")
    p = t.generated_path
    if not p.exists() or not p.stat().st_size:
        return Check("recent output", False, "nothing generated yet",
                     "./karamel draft", level=WARN)
    age_h = (time.time() - p.stat().st_mtime) / 3600
    if age_h > 36:
        return Check("recent output", False,
                     f"last draft {age_h:.0f}h ago", "./karamel doctor --deep",
                     level=WARN)
    return Check("recent output", True, f"last draft {age_h:.1f}h ago")


def check_gate():
    """Does anything actually clear the critic? A gate nothing passes is
    indistinguishable from a gate that is working, from the outside. This
    reads history rather than grading anything, so it costs nothing."""
    try:
        import tenants
        t = tenants.load_tenant(_owner())
    except Exception as e:
        return Check("critic gate", False, f"cannot resolve tenant: {e}", "")
    if t is None or not t.generated_path.exists():
        return Check("critic gate", True, "no history yet", level=WARN)
    rows = []
    for line in t.generated_path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return Check("critic gate", True, "no history yet", level=WARN)
    recent = rows[-20:]
    passed = sum(1 for r in recent if r.get("verdict") == "PASS")
    if passed:
        return Check("critic gate", True, f"{passed}/{len(recent)} recent runs passed")
    axes = {}
    for r in recent:
        for a, v in (r.get("scores") or {}).items():
            axes.setdefault(a, []).append(v)
    worst = ""
    try:
        import critic
        blocking = [a for a in critic.AXES
                    if axes.get(a) and
                    sum(axes[a]) / len(axes[a]) < critic.THRESHOLDS[a]]
        worst = f" always below bar on {', '.join(blocking)}" if blocking else ""
    except ImportError:
        pass
    return Check("critic gate", False,
                 f"0 of {len(recent)} recent runs passed{worst}",
                 "./karamel draft, and read the scores", level=WARN)


def run_checks(deep=False):
    return [check_python(), check_key(deep), check_tenant(), check_voice_card(),
            check_channel(), check_playwright(), check_halt(), check_agents(),
            check_recent_draft(), check_gate()]


# ------------------------------------------------------------------ log tailing

def err_logs():
    return sorted(p for p in CONFIG_DIR.glob("*.err") if p.is_file())


def new_stderr(state):
    """New bytes written to any .err since the last watch run.

    Offsets, not timestamps: a component that logs every five minutes has a
    fresh mtime forever, and mtime cannot tell one new line from a thousand.
    A file that shrank was rotated or truncated, so it is re-read from zero."""
    found, offsets = {}, dict(state.get("offsets") or {})
    for p in err_logs():
        size = p.stat().st_size
        prev = offsets.get(p.name, 0)
        if size < prev:
            prev = 0
        if size > prev:
            with open(p, "r", errors="replace") as f:
                f.seek(prev)
                chunk = f.read()
            if chunk.strip():
                found[p.name] = chunk.strip()
        offsets[p.name] = size
    return found, offsets


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def should_alert(state, key, now=None):
    """True if this exact problem has not been reported recently. Without this
    a stuck component reports every five minutes until the channel is muted.

    Never-seen is its own case, not a timestamp of zero. Defaulting to 0 made
    "never reported" arithmetically identical to "reported at the epoch", which
    is only harmless while the clock is large."""
    now = now if now is not None else time.time()
    last = (state.get("alerted") or {}).get(key)
    if last is None:
        return True
    return (now - last) >= REALERT_SECONDS


def mark_alerted(state, key, now=None):
    state.setdefault("alerted", {})[key] = now if now is not None else time.time()


def support_email():
    """Who to forward a broken install to, or None.

    Read from dashboard.json, which is already where share_with lives, rather
    than compiled in. An address in the source would ship in the public package
    to everyone who ever clones it, and it would be wrong on the operator's own
    machine, where this email would tell them to forward it to themselves."""
    try:
        return json.loads((CONFIG_DIR / "dashboard.json").read_text()).get(
            "support_email") or None
    except Exception:
        return None


def build_alert(failures, stderr_by_log, support=None):
    """The message someone receives when their install breaks.

    Fixes first, detail underneath. This arrives on a phone, hours later, with
    no terminal in front of it and usually to somebody who did not install this
    and cannot read a stack trace. What they need in the first screen is the
    one command that might fix it; what broke is context for whoever they
    forward it to."""
    lines = ["Oops, something doesn't look right."]

    remedies = [c.remedy for c in failures if getattr(c, "remedy", "")]
    lines.append("")
    lines.append("Here's what you can do:")
    if remedies:
        # Deduplicated: several checks failing off one cause tend to name the
        # same command, and three copies of it reads as three problems.
        seen = []
        for r in remedies:
            if r not in seen:
                seen.append(r)
        for r in seen:
            lines.append(f"  {r}")
    else:
        lines.append("  Nothing you can run will fix this one.")

    lines.append("")
    lines.append("What went wrong:")
    for c in failures:
        lines.append(f"  {c.name}: {c.detail}")
    for name, chunk in stderr_by_log.items():
        tail = chunk.strip().splitlines()[-6:]
        lines.append("")
        lines.append(f"  {name}:")
        lines.extend(f"    {l[:200]}" for l in tail)

    if support:
        lines.append("")
        lines.append(f"If that does not fix it, forward this email to "
                     f"{support} and we will fix it for you.")

    out = "\n".join(lines)
    return out[:MAX_ALERT_CHARS]


def watch(dry=False):
    """One unattended pass. Reports new stderr and newly-failing FATAL checks."""
    state = load_state()
    stderr_by_log, offsets = new_stderr(state)
    state["offsets"] = offsets

    checks = run_checks(deep=False)
    failures = [c for c in checks if not c.ok and c.level == FATAL]

    fresh_fail = [c for c in failures if should_alert(state, f"check:{c.name}")]
    fresh_logs = {n: t for n, t in stderr_by_log.items()
                  if should_alert(state, f"log:{n}")}

    if not fresh_fail and not fresh_logs:
        save_state(state)
        print(f"watch: nothing new ({len(failures)} failing, all already reported)")
        return 0

    msg = build_alert(fresh_fail, fresh_logs, support=support_email())
    try:
        import channels
        import tenants
        t = tenants.load_tenant(_owner())
        if t is None:
            raise RuntimeError(f"no tenant {_owner()!r}")
        subject = "[Karamel] Oops, something doesn't look right..."
        channels.send(t, msg, dry=dry, subject=subject)

        # And to the operator directly, when one is configured. The email tells
        # the person to forward it, which is fine when they notice, and this is
        # a machine nobody is looking at: the whole reason this file exists is
        # that a break on somebody else's Mac is indistinguishable from a quiet
        # week. Depending on the one person who cannot debug it to relay the
        # alert puts the only signal behind the least reliable step.
        #
        # Sent separately rather than as a Cc so a delivery failure to one
        # address cannot swallow the other, and so the operator's copy is
        # unaffected by whatever is wrong with the tenant's mail.
        support = support_email()
        addr = ((t.channel or {}).get("address") or "").lower()
        if support and support.lower() != addr:
            try:
                class _Operator:
                    id, name = t.id, t.name
                    channel = {"type": "email", "address": support}
                channels.send(_Operator(), f"[{t.name} / {t.id}]\n\n{msg}",
                              dry=dry,
                              subject=f"[Karamel] {t.name}'s install needs a look")
            except Exception as e:
                print(f"could not copy the operator at {support}: {e}",
                      file=sys.stderr)
    except Exception as e:
        # The alert path itself failing is the one error that cannot be
        # delivered. Say it loudly on stdout, which launchd captures.
        print(f"watch: COULD NOT DELIVER ALERT: {e}\n{msg}", file=sys.stderr)
        save_state(state)
        return 1

    for c in fresh_fail:
        mark_alerted(state, f"check:{c.name}")
    for n in fresh_logs:
        mark_alerted(state, f"log:{n}")
    save_state(state)
    print(f"watch: alerted on {len(fresh_fail)} check(s), {len(fresh_logs)} log(s)")
    return 0


# ------------------------------------------------------------------- reporting

def report(checks):
    width = max(len(c.name) for c in checks)
    bad = 0
    for c in checks:
        if c.ok:
            mark = "ok  "
        elif c.level == WARN:
            mark = "warn"
        else:
            mark = "FAIL"
            bad += 1
        print(f"  {mark}  {c.name.ljust(width)}  {c.detail}")
        if not c.ok and c.remedy:
            print(f"        {' ' * width}  -> {c.remedy}")
    print()
    if bad:
        print(f"{bad} thing(s) will stop this working. Fix the FAIL lines above.")
    else:
        warns = sum(1 for c in checks if not c.ok)
        print("Nothing is broken." if not warns
              else f"Working, with {warns} thing(s) worth a look.")
    return 1 if bad else 0


def selftest():
    st = {"alerted": {"check:x": 1000.0}}
    assert not should_alert(st, "check:x", now=1000.0 + REALERT_SECONDS - 1)
    assert should_alert(st, "check:x", now=1000.0 + REALERT_SECONDS + 1)
    assert should_alert(st, "check:never-seen", now=0.0)

    # The halt seen live on the host: a CDP attach failure, which the tripwire
    # records through exactly the same field enforcement would use. Calling that
    # "this may be enforcement" is how a five minute fix becomes a week of not
    # touching the account.
    real = ("CDP attach failed (Chrome bot profile down?): "
            "BrowserType.connect_over_cdp: Protocol error "
            "(Browser.setDownloadBehavior): Browser context management is not "
            "supported.")
    assert classify_halt(real) == "infrastructure", classify_halt(real)
    assert classify_halt("rate limited by the platform") == "enforcement"
    assert classify_halt("account restricted") == "enforcement"
    assert classify_halt("something nobody anticipated") == "unknown"
    # Both present: enforcement wins, because the cost of the two mistakes is
    # not symmetric.
    assert classify_halt("connect_over_cdp failed after account suspended") \
        == "enforcement"

    c = Check("k", False, "d", "do this")
    assert c.to_dict()["remedy"] == "do this"

    # Fixes first, detail underneath. This arrives on a phone, hours later, to
    # somebody who did not install this and cannot read a stack trace: the first
    # screen has to be the command that might fix it.
    msg = build_alert([Check("model key", False, "no key", "add one")],
                      {"drafter.err": "Traceback\nboom"})
    assert msg.startswith("Oops, something doesn't look right."), msg
    assert "model key" in msg and "add one" in msg and "boom" in msg, msg
    assert msg.index("Here's what you can do") < msg.index("What went wrong"), \
        "the fix comes before the diagnosis"
    assert msg.index("add one") < msg.index("boom"), \
        "a remedy must not sit below a stack trace"
    assert len(build_alert([], {"a.err": "x" * 5000})) <= MAX_ALERT_CHARS

    # The forward line only when an address is configured. Compiling one in
    # would ship a personal address to everyone who clones this, and on the
    # operator's own machine it would say to forward the mail to themselves.
    assert "forward this email" not in msg, msg
    withsup = build_alert([Check("k", False, "d", "do this")], {},
                          support="ops@example.com")
    assert "forward this email to ops@example.com" in withsup, withsup
    assert withsup.rstrip().endswith("we will fix it for you."), withsup

    # Several checks failing off one cause name the same command; three copies
    # of it reads as three separate problems.
    dup = build_alert([Check("a", False, "x", "same fix"),
                       Check("b", False, "y", "same fix")], {})
    assert dup.count("same fix") == 1, dup

    # A failure with no remedy still says something rather than leaving the
    # section it promised empty.
    none = build_alert([Check("a", False, "x", "")], {})
    assert "Nothing you can run" in none, none

    # A shrunken log was rotated or truncated, and must be re-read from zero
    # rather than reported as "nothing new" forever after.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.err"
        p.write_text("boom\n")
        real_glob = globals()["err_logs"]
        globals()["err_logs"] = lambda: [p]
        try:
            found, offs = new_stderr({"offsets": {}})
            assert found == {"t.err": "boom"}, found
            assert offs["t.err"] == p.stat().st_size

            # Same file, already consumed: nothing new.
            found2, offs2 = new_stderr({"offsets": offs})
            assert found2 == {}, found2

            # Rotated smaller: the new content is reported, not swallowed.
            p.write_text("x\n")
            found3, _ = new_stderr({"offsets": {"t.err": 9999}})
            assert found3 == {"t.err": "x"}, found3

            # Whitespace-only appends are not an incident.
            p.write_text("x\n\n   \n")
            found4, _ = new_stderr({"offsets": {"t.err": 2}})
            assert found4 == {}, found4
        finally:
            globals()["err_logs"] = real_glob
    print("selftest: all assertions passed")
    return True


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    if "--watch" in sys.argv:
        return watch(dry="--dry" in sys.argv)

    deep = "--deep" in sys.argv
    checks = run_checks(deep=deep)
    if "--json" in sys.argv:
        print(json.dumps({"checks": [c.to_dict() for c in checks],
                          "ok": all(c.ok for c in checks)}, indent=2))
        return 0 if all(c.ok or c.level == WARN for c in checks) else 1
    print(f"Karamel doctor  ({PROJECT})")
    print(f"config {CONFIG_DIR}")
    if not deep:
        # Provider-aware, because on the cli path there is no key and no API
        # call, and offering to prove one works is just wrong on the machine
        # most likely to be reading this.
        try:
            import llm
            provider = llm.load_config().get("provider", "api")
        except Exception:
            provider = "api"
        print("(--deep also makes one real call to prove the "
              + ("subscription" if provider == "cli" else "key") + " works)")
    print()
    return report(checks)


if __name__ == "__main__":
    sys.exit(main())
