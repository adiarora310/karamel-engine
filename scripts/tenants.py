#!/usr/bin/env python3
"""Tenant registry: who Karamel runs for, and where each one's state lives.

Until now "multi-tenant from the first line (policy 1.4)" was true of exactly
one file. safety.py keys every counter by tenant; everything else hardcoded
`TENANT = "adi"` at module level and resolved all state from a single HOME. A
second user on the same box would have silently shared Adi's drafts, his
engagement log, his reply counter and his Telegram chat.

This module is the seam. A tenant is a row, not a machine: the original-content
path (gen.py -> critic.py -> approval) needs no browser, no X session and no
per-user OS state, so isolation belongs at the data layer rather than in a VM.

TWO RULES WORTH NOT LOSING

1. `adi` keeps the legacy flat paths. His system is live and auto-deploys via
   notifier.git_pull(). Re-pointing his data at data/tenants/adi/ would strand
   254 candidates, 256 drafts and 21 engagements on the next pull. LEGACY_TENANT
   exists solely so that cannot happen; new tenants are namespaced from day one.

2. A tenant config CANNOT turn reply mining on. The field here is intent; the
   authority stays safety.REPLY_MINING_TENANTS, a frozenset in code that the
   selftest pins. Effective value is the AND of the two. This is deliberate:
   reply mining is the pattern that got a real account labelled, and enabling it
   should require a diff that fails a test, not an edit to a JSON file nobody
   reviews. See effective_reply_mining().

CLI:
  --list                       every registered tenant
  --show ID                    one tenant, resolved paths included
  --create ID --name "..."     register a tenant
      --email ADDR             deliver by email to this address
      --telegram-chat ID       deliver by Telegram to this chat
      --seed "topic"           a subject to draft about (repeatable, and
                               REQUIRED: a tenant with no seeds and an empty
                               queue generates nothing at all)
      --voice-card PATH        card to write in, relative to the project
      --timezone TZ            e.g. America/Los_Angeles
  --selftest                   pure-logic tests, no persistent side effects
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared import (
    CONFIG_DIR, DATA, DRAFTS, ENGAGEMENT, PAUSE_STATE, PROJECT, TELEGRAM_CFG,
)

TENANT_DIR = CONFIG_DIR / "tenants"

# The tenant who owns this box: their files sit at the flat top-level paths
# rather than under data/tenants/<id>/, and they always resolve even with no
# registry on disk.
#
# Configurable because a self-hosted install has a different owner. It was the
# literal string "adi", which on someone else's machine means the person running
# it is not the owner of their own installation: their data would be namespaced
# under a stranger's name and a bare `heartbeat.py` would look for a tenant that
# does not exist. KARAMEL_OWNER is set by deploy/install.py into each agent's
# environment.
def _owner_marker():
    """The owner recorded in .karamel, written by the bootstrap. None if absent.

    The env var is only set inside the launchd agents. Anything run by hand,
    which is most of debugging, had nothing but the literal fallback below, so
    the same command produced different answers depending on whether an agent
    or a person invoked it. This file is what makes both agree."""
    try:
        return (json.loads((PROJECT / ".karamel").read_text())
                .get("owner") or None)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


# The fallback is a placeholder, deliberately not a person's name. It was the
# author's first name, which shipped in the public package in four files: on
# someone else's Mac a bare `heartbeat.py` looked for a tenant named after a
# stranger, found nothing, and said so in a way that gave no hint why. The
# package audit catches the full name and the handle, but cannot blanket-block
# three letters that also appear inside ordinary English words.
LEGACY_TENANT = (os.environ.get("KARAMEL_OWNER") or _owner_marker()
                 or "owner")

VALID_CHANNELS = ("telegram", "email", "none")
VALID_SOURCES = ("following", "list")


def default_author():
    """Whose voice a generator writes in when the caller did not say.

    Resolved from the registry, never compiled in. The author's full name was
    baked into three prompt builders and one grader default, so every copy of
    this software instructed the model to write in one particular stranger's
    voice, and shipped that name to everyone who received it. Callers that know
    their tenant should still pass tenant.name; this is the floor, not the
    mechanism."""
    try:
        t = load_tenant(LEGACY_TENANT)
        if t and t.name:
            return t.name
    except Exception:
        pass
    return "the author"

DEFAULTS = {
    "timezone": "America/New_York",
    "originals_per_day": 3,
    "reply_mining": False,   # product default: retired. See effective_reply_mining.
    "enabled": True,
    # Adi's hard rule, and the single highest-precision AI tell in his voice
    # card, so it stays the default. It is a PERSON'S rule, not a house rule:
    # the critic failed any draft containing one and the maker rewrote them into
    # commas, both unconditionally. A tenant whose real prose uses em dashes
    # would have had every characteristic draft either mangled or rejected, with
    # the voice card saying one thing and a deterministic check overruling it.
    "allow_em_dash": False,
}

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def is_valid_id(tenant_id):
    """A tenant id must already equal its own slug.

    safety._slug() collapses '../../adi' and 'ADI/../' to 'adi', which is right
    for deriving a counter filename and wrong for an identity. Rather than slug
    on the way in and hope, ids that are not already clean are rejected outright,
    so the id we admit and the paths we then build from it cannot diverge.
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        return False
    return _SLUG_RE.sub("-", tenant_id.lower()).strip("-") == tenant_id


class Tenant:
    def __init__(self, data):
        self.id = data["id"]
        self.name = data.get("name", self.id)
        self.voice_card = data.get("voice_card", "03_voice_card.md")
        self.channel = data.get("channel", {"type": "none"})
        self.timezone = data.get("timezone", DEFAULTS["timezone"])
        self.originals_per_day = data.get(
            "originals_per_day", DEFAULTS["originals_per_day"]
        )
        self.reply_mining = data.get("reply_mining", DEFAULTS["reply_mining"])
        self.allow_em_dash = data.get("allow_em_dash", DEFAULTS["allow_em_dash"])
        # Fallback topics for when this tenant's queue is empty. Empty by
        # default and deliberately NOT inherited: heartbeat's module-level
        # SEED_TOPICS are Adi's interests (Dieter Rams, founder posts, the
        # Lakers), so a second tenant with nothing queued would have received
        # drafts about another person's obsessions, in their own voice.
        self.seed_topics = data.get("seed_topics") or []
        # --- reply-mining config. Only consulted when effective_reply_mining()
        # is True, which needs BOTH this tenant's flag and the code-level
        # allowlist in safety.py.
        # Where the listener reads from.
        #   "following" - their own chronological Following timeline. Zero setup,
        #                 and it is the curation they already did, one follow at
        #                 a time. Default.
        #   "list"      - a curated X List, which needs list_id. Better
        #                 signal-to-noise, but somebody has to build it.
        # Never "for you": that timeline is ranked by engagement, so it surfaces
        # whatever is most inflammatory that hour, which is the opposite of what
        # a voice card ruling out partisan politics wants in its queue.
        self.source = (data.get("source") or "following").lower()
        # Whose X List, when source is "list". No default: a tenant borrowing
        # another's list would scrape strangers and draft at them in the wrong
        # voice.
        self.list_id = data.get("list_id") or None
        # Their own handle, so their own posts can be filtered out of their own
        # feed. Without it the listener drafts replies to the tenant, from the
        # tenant, which reads exactly as broken as it is.
        self.handle = (data.get("handle") or "").lower().lstrip("@")
        # A tenant's X session lives in its own Chrome profile on its own debug
        # port. Sharing 9222 would mean two tenants driving one logged-in
        # browser, i.e. one person's account posting the other's replies.
        self.cdp_port = int(data.get("cdp_port") or 9222)
        # Who matters most to THIS person. tier_of() drives the listener's
        # signal filter and the drafter's prompt, and the global set is Adi's 25
        # VCs, so every one of another tenant's targets would score tier 2 and
        # be filtered as background noise.
        self.tier1_handles = {
            h.lower().lstrip("@") for h in (data.get("tier1_handles") or [])
        }
        self.max_reads_per_day = int(data.get("max_reads_per_day") or 100)
        self.enabled = data.get("enabled", DEFAULTS["enabled"])
        self.created_at = data.get("created_at")
        self._raw = data

    # ---- paths -----------------------------------------------------------

    @property
    def is_legacy(self):
        return self.id == LEGACY_TENANT

    @property
    def data_dir(self):
        """Where this tenant's JSONL lives. Legacy tenant keeps the flat dir."""
        return DATA if self.is_legacy else DATA / "tenants" / self.id

    @property
    def drafts_path(self):
        return DRAFTS if self.is_legacy else self.data_dir / "drafts.jsonl"

    @property
    def engagement_path(self):
        return ENGAGEMENT if self.is_legacy else self.data_dir / "engagement.jsonl"

    @property
    def original_drafts_path(self):
        return self.data_dir / "original_drafts.jsonl"

    @property
    def topic_queue_path(self):
        return self.data_dir / "topic_queue.jsonl"

    @property
    def generated_path(self):
        return self.data_dir / "generated.jsonl"

    @property
    def critiques_path(self):
        return self.data_dir / "critiques.jsonl"

    @property
    def reflections_dir(self):
        return self.data_dir / "reflections"

    @property
    def reflections_log(self):
        return self.data_dir / "reflections.jsonl"

    @property
    def voice_card_path(self):
        p = Path(self.voice_card)
        return p if p.is_absolute() else PROJECT / self.voice_card

    def reads_path(self, date):
        """Today's read counter. The legacy owner keeps the flat filename so a
        running day's count is not reset by this change; anyone else is keyed."""
        stem = "reads" if self.is_legacy else f"reads_{self.id}"
        return CONFIG_DIR / f"{stem}_{date}.txt"

    @property
    def pause_path(self):
        """This tenant's pause/halt file.

        The legacy owner resolves to the SHARED pause_state.json, which is what
        /halt and /pause from Telegram write and what every existing component
        reads, so Adi's behaviour is byte-identical. A new tenant gets their own,
        so a tripwire on their X session halts them and not the whole box."""
        return PAUSE_STATE if self.is_legacy else CONFIG_DIR / f"pause_{self.id}.json"

    def tier_of(self, handle):
        """1 if this handle is one of THIS tenant's priority targets, else 2."""
        return 1 if str(handle).lower().lstrip("@") in self.tier1_handles else 2

    @property
    def tz(self):
        """The tenant's zone. A draft is stamped and windowed in the author's
        local time, not the server's: one Mac will serve people in several."""
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo(DEFAULTS["timezone"])

    def now(self):
        return datetime.now(self.tz)

    # ---- policy ----------------------------------------------------------

    def effective_reply_mining(self):
        """(allowed, reason). The AND of this tenant's intent and the code-level
        allowlist in safety.py. Config alone can never widen it: that would turn
        a product safety claim into an editable field."""
        from safety import reply_mining_allowed

        code_ok, why = reply_mining_allowed(self.id)
        if not code_ok:
            return False, why
        if not self.reply_mining:
            return False, f"tenant '{self.id}' config has reply_mining off"
        return True, f"tenant '{self.id}' permitted by both config and safety.py"

    def to_dict(self):
        d = dict(self._raw)
        d.update({
            "id": self.id, "name": self.name, "voice_card": self.voice_card,
            "channel": self.channel, "timezone": self.timezone,
            "originals_per_day": self.originals_per_day,
            "reply_mining": self.reply_mining, "enabled": self.enabled,
            "allow_em_dash": self.allow_em_dash,
            "seed_topics": self.seed_topics,
            "source": self.source,
            "list_id": self.list_id,
            "handle": self.handle,
            "cdp_port": self.cdp_port,
            "tier1_handles": sorted(self.tier1_handles),
            "max_reads_per_day": self.max_reads_per_day,
        })
        return d

    def __repr__(self):
        return f"<Tenant {self.id} channel={self.channel.get('type')} enabled={self.enabled}>"


# ---- validation ----------------------------------------------------------


def validate_channel(channel):
    """(ok, reason). Shape only; delivery lives in the channel backends."""
    if not isinstance(channel, dict):
        return False, "channel must be an object"
    kind = channel.get("type")
    if kind not in VALID_CHANNELS:
        return False, f"channel.type must be one of {VALID_CHANNELS}, got {kind!r}"
    if kind == "email" and not channel.get("address"):
        return False, "email channel needs an address"
    if kind == "telegram" and not channel.get("chat_id"):
        return False, "telegram channel needs a chat_id"
    return True, "ok"


def validate(data):
    """(ok, reason) for a tenant record, before it is written."""
    tid = data.get("id")
    if not is_valid_id(tid):
        return False, (
            f"invalid tenant id {tid!r}: must be lowercase [a-z0-9-] and already "
            "equal its own slug"
        )
    ok, why = validate_channel(data.get("channel", {"type": "none"}))
    if not ok:
        return False, why
    src = (data.get("source") or "following").lower()
    if src not in VALID_SOURCES:
        return False, f"source must be one of {VALID_SOURCES}, got {src!r}"
    if src == "list" and not data.get("list_id"):
        return False, "source 'list' needs a list_id"
    n = data.get("originals_per_day", DEFAULTS["originals_per_day"])
    if not isinstance(n, int) or not 0 <= n <= 20:
        return False, f"originals_per_day must be an int 0..20, got {n!r}"
    return True, "ok"


# ---- storage -------------------------------------------------------------


def tenant_path(tenant_id):
    return TENANT_DIR / f"{tenant_id}.json"


def legacy_tenant():
    """The pre-registry owner, synthesized rather than required on disk.

    Adi's system runs today with no tenants/ directory at all, and the notifier
    auto-deploys this branch's code the moment it merges. If resolving him
    depended on a config file someone remembered to create, the first pull would
    turn every heartbeat run into "no such tenant: adi" and drafting would stop
    silently on a working system.

    The channel mirrors exactly what heartbeat did before: Telegram when creds
    are on the box, stdout when they are not.
    """
    channel = (
        {"type": "telegram"} if TELEGRAM_CFG.exists() else {"type": "none"}
    )
    return Tenant({
        "id": LEGACY_TENANT,
        # No hardcoded person. A fresh install greeted its new owner as "Adi
        # Arora", which is the kind of detail that tells someone this was built
        # for somebody else and they are running a copy.
        "name": os.environ.get("KARAMEL_OWNER_NAME") or LEGACY_TENANT.replace("-", " ").title(),
        "channel": channel,
        "reply_mining": True,   # still gated by safety.REPLY_MINING_TENANTS
    })


def load_tenant(tenant_id):
    """Tenant, or None if not registered. The legacy tenant always resolves."""
    if not is_valid_id(tenant_id):
        return None
    p = tenant_path(tenant_id)
    if not p.exists():
        return legacy_tenant() if tenant_id == LEGACY_TENANT else None
    try:
        return Tenant(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError, KeyError):
        # A corrupt config must not silently strand the owner of the box.
        return legacy_tenant() if tenant_id == LEGACY_TENANT else None


def list_tenants(include_disabled=False):
    if not TENANT_DIR.exists():
        return []
    out = []
    for p in sorted(TENANT_DIR.glob("*.json")):
        t = load_tenant(p.stem)
        if t and (include_disabled or t.enabled):
            out.append(t)
    return out


def save_tenant(tenant):
    ok, why = validate(tenant.to_dict())
    if not ok:
        raise ValueError(why)
    TENANT_DIR.mkdir(parents=True, exist_ok=True)
    tenant_path(tenant.id).write_text(json.dumps(tenant.to_dict(), indent=2) + "\n")
    tenant.data_dir.mkdir(parents=True, exist_ok=True)
    return tenant


def create_tenant(tenant_id, name=None, channel=None, **kw):
    from shared import now_iso

    if load_tenant(tenant_id) is not None:
        raise ValueError(f"tenant '{tenant_id}' already exists")
    data = dict(DEFAULTS)
    data.update(kw)
    data.update({
        "id": tenant_id,
        "name": name or tenant_id,
        "channel": channel or {"type": "none"},
        "created_at": now_iso(),
    })
    return save_tenant(Tenant(data))


# ---- selftest ------------------------------------------------------------


def selftest():
    # Ids are validated, never slugged into shape. Each of these normalizes to
    # "adi" under safety._slug and must be refused as an identity here.
    assert is_valid_id("adi")
    assert is_valid_id("acme-corp") and is_valid_id("user-2")
    for bad in ("ADI", "../../adi", "ADI/../", "adi ", "a/b", "", "..", None, 7):
        assert not is_valid_id(bad), bad

    # Legacy tenant keeps the flat paths; anyone else is namespaced.
    legacy = Tenant({"id": LEGACY_TENANT})
    assert legacy.is_legacy
    assert legacy.drafts_path == DRAFTS
    assert legacy.engagement_path == ENGAGEMENT
    assert legacy.data_dir == DATA

    other = Tenant({"id": "acme"})
    assert not other.is_legacy
    assert other.data_dir == DATA / "tenants" / "acme"
    assert other.drafts_path == DATA / "tenants" / "acme" / "drafts.jsonl"
    assert other.drafts_path != DRAFTS
    assert other.engagement_path != ENGAGEMENT

    # No two tenants can collide on any path.
    a, b = Tenant({"id": "one"}), Tenant({"id": "two"})
    for attr in ("data_dir", "drafts_path", "engagement_path", "original_drafts_path"):
        assert getattr(a, attr) != getattr(b, attr), attr

    # Channel shape.
    assert validate_channel({"type": "email", "address": "a@b.com"})[0]
    assert not validate_channel({"type": "email"})[0]
    assert not validate_channel({"type": "telegram"})[0]
    assert validate_channel({"type": "telegram", "chat_id": 1})[0]
    assert not validate_channel({"type": "sms", "address": "x"})[0]
    assert not validate_channel("email")[0]

    # Record validation.
    assert validate({"id": "acme", "channel": {"type": "none"}})[0]
    assert not validate({"id": "ACME", "channel": {"type": "none"}})[0]
    assert not validate({"id": "acme", "originals_per_day": 99})[0]
    assert not validate({"id": "acme", "originals_per_day": "3"})[0]

    # The reflector's outputs are per tenant too. They are a written analysis of
    # one person's editing habits against their own voice card, so a shared path
    # would mean one tenant's weekly report describing another's writing.
    leg = legacy_tenant()
    assert leg.reflections_dir == DATA / "reflections"
    assert leg.reflections_log == DATA / "reflections.jsonl"
    other = Tenant({"id": "someone"})
    assert other.reflections_dir == DATA / "tenants" / "someone" / "reflections"
    assert other.reflections_log != leg.reflections_log

    # THE INVARIANT: config alone cannot turn reply mining on. 'acme' asks for
    # it and is refused, because safety.py's frozenset does not name it.
    greedy = Tenant({"id": "acme", "reply_mining": True})
    allowed, why = greedy.effective_reply_mining()
    assert allowed is False, "a config field widened reply mining"
    assert "retired" in why

    # Both switches are tested against a name that is actually on the allowlist,
    # not against whoever owns this machine. Using LEGACY_TENANT here passed only
    # on the one install where the owner happened to be the name compiled into
    # safety.py, and failed on every self-hosted copy.
    #
    # The allowlist is empty on a fresh install, because the names live in a
    # module that is not distributed. Indexing into it unconditionally passed
    # here and raised IndexError on every clone, which is the same shape of bug
    # the comment above describes and the reason a build is tested by cloning it
    # rather than by running the tests in the tree that produced it.
    from safety import REPLY_MINING_TENANTS
    if REPLY_MINING_TENANTS:
        named = sorted(REPLY_MINING_TENANTS)[0]
        # Named in code, off in config, is still off.
        assert Tenant({"id": named, "reply_mining": False})\
            .effective_reply_mining()[0] is False
        # Both agreeing is the only way through.
        assert Tenant({"id": named, "reply_mining": True})\
            .effective_reply_mining()[0] is True
    else:
        # Shipped default: nobody is named, so config alone cannot open the gate
        # for anybody, which is the whole invariant stated at its strongest.
        assert Tenant({"id": "anybody", "reply_mining": True})\
            .effective_reply_mining()[0] is False

    # The legacy owner resolves with no registry on disk. Without this, the
    # first pull after this branch merges turns every heartbeat run into
    # "no such tenant: adi" on a system that was working.
    l = legacy_tenant()
    assert l.id == LEGACY_TENANT and l.is_legacy
    assert l.drafts_path == DRAFTS and l.data_dir == DATA
    assert l.channel["type"] in ("telegram", "none")

    # Owning the box does NOT grant reply mining. On a self-hosted install the
    # owner is whoever set KARAMEL_OWNER, and they are not in the shipped
    # allowlist, so the listener refuses for them exactly as SETUP.md promises.
    # The earlier version asserted True here and only passed because the owner
    # happened to be the one name compiled into safety.py.
    from safety import REPLY_MINING_TENANTS
    expected = LEGACY_TENANT in REPLY_MINING_TENANTS
    assert l.effective_reply_mining()[0] is expected, (
        f"owner {LEGACY_TENANT!r} mining={not expected}: owning the machine is "
        "not the same as being on the allowlist")

    # A stranger still does not resolve out of thin air.
    _real = tenant_path
    globals()["tenant_path"] = lambda t: Path("/definitely/not/here.json")
    try:
        assert load_tenant(LEGACY_TENANT) is not None, "legacy must always resolve"
        assert load_tenant("acme") is None, "unregistered tenant must not resolve"
    finally:
        globals()["tenant_path"] = _real

    # --- reply-mining isolation ----------------------------------------------
    a, b = Tenant({"id": LEGACY_TENANT}), Tenant({"id": "acme"})

    # The legacy owner keeps the flat filenames, so a running day's read count
    # is not reset and /halt from Telegram still reaches him.
    assert a.reads_path("2026-08-10") == CONFIG_DIR / "reads_2026-08-10.txt"
    assert a.pause_path == PAUSE_STATE

    # Everyone else is keyed, and cannot collide with him or each other.
    c = Tenant({"id": "beta"})
    assert b.reads_path("2026-08-10") == CONFIG_DIR / "reads_acme_2026-08-10.txt"
    assert b.reads_path("2026-08-10") != a.reads_path("2026-08-10")
    assert b.reads_path("2026-08-10") != c.reads_path("2026-08-10")
    assert b.pause_path != a.pause_path and b.pause_path != c.pause_path

    # Tier 1 is this tenant's own targets. The global set is Adi's 25 VCs, so
    # without this every one of another tenant's targets scores 2 and is
    # filtered as low signal.
    t1 = Tenant({"id": "x", "tier1_handles": ["@MixedCase", "plain"]})
    assert t1.tier_of("mixedcase") == 1 and t1.tier_of("@mixedcase") == 1
    assert t1.tier_of("PLAIN") == 1
    assert t1.tier_of("someone-else") == 2
    assert Tenant({"id": "y"}).tier_of("mixedcase") == 2, "no targets means no tier 1"

    # Reading source defaults to their own Following timeline: no setup, and it
    # is curation they already did. "for you" is not selectable at all.
    assert Tenant({"id": "z"}).source == "following"
    assert Tenant({"id": "z", "source": "LIST", "list_id": "1"}).source == "list"
    assert validate({"id": "z", "source": "following"})[0]
    assert not validate({"id": "z", "source": "list"})[0]           # needs a list_id
    assert validate({"id": "z", "source": "list", "list_id": "1"})[0]
    assert not validate({"id": "z", "source": "for-you"})[0]
    assert not validate({"id": "z", "source": "home"})[0]

    # Their own handle, normalised, so their own posts can be excluded.
    assert Tenant({"id": "z", "handle": "@MixedCase"}).handle == "mixedcase"
    assert Tenant({"id": "z"}).handle == ""

    # A tenant with no list of their own has none. Borrowing another tenant's
    # list would read strangers and draft at them in the wrong voice.
    assert Tenant({"id": "z"}).list_id is None
    # Ports are per tenant: one Chrome holds one person's X session.
    assert Tenant({"id": "z"}).cdp_port == 9222
    assert Tenant({"id": "z", "cdp_port": 9223}).cdp_port == 9223

    print("tenants selftest: all assertions passed")


def _args(flag):
    """Every value given for a repeatable flag, in order."""
    out = []
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            out.append(sys.argv[i + 1])
    return out


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    if "--list" in sys.argv:
        ts = list_tenants(include_disabled=True)
        if not ts:
            print(f"no tenants registered (looked in {TENANT_DIR})")
            return 0
        for t in ts:
            mining = "on" if t.effective_reply_mining()[0] else "off"
            print(
                f"{t.id:16s} {t.name:24s} channel={t.channel.get('type'):9s} "
                f"enabled={str(t.enabled):5s} reply_mining={mining}"
            )
        return 0

    if "--show" in sys.argv:
        t = load_tenant(_arg("--show"))
        if t is None:
            print(f"no such tenant: {_arg('--show')}", file=sys.stderr)
            return 1
        print(json.dumps(t.to_dict(), indent=2))
        print(f"\nresolved paths{' (LEGACY, flat)' if t.is_legacy else ''}:")
        print(f"  data_dir        {t.data_dir}")
        print(f"  drafts          {t.drafts_path}")
        print(f"  engagement      {t.engagement_path}")
        print(f"  original_drafts {t.original_drafts_path}")
        print(f"  voice card      {t.voice_card_path}")
        allowed, why = t.effective_reply_mining()
        print(f"\nreply_mining={allowed} ({why})")
        return 0

    if "--create" in sys.argv:
        tid = _arg("--create")
        channel = {"type": "none"}
        if _arg("--email"):
            channel = {"type": "email", "address": _arg("--email")}
        elif _arg("--telegram-chat"):
            channel = {"type": "telegram", "chat_id": _arg("--telegram-chat")}
        # Without seeds a new tenant generates nothing: heartbeat draws its
        # subjects from the tenant's own list, and only the legacy owner falls
        # back to the built-in one. --create used to produce a tenant that
        # looked registered, resolved its paths, and silently had no work to do.
        extra = {}
        seeds = _args("--seed")
        if seeds:
            extra["seed_topics"] = [{"topic": s_, "register": "analytical"}
                                    for s_ in seeds]
        if _arg("--voice-card"):
            extra["voice_card"] = _arg("--voice-card")
        if _arg("--timezone"):
            extra["timezone"] = _arg("--timezone")
        try:
            t = create_tenant(tid, name=_arg("--name"), channel=channel, **extra)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        print(f"created {t.id} -> {tenant_path(t.id)}")
        print(f"data dir: {t.data_dir}")
        return 0

    print(__doc__.strip().split("CLI:")[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
