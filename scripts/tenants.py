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

CADENCES = frozenset({"twice_daily", "hourly"})
TWICE_DAILY_HOURS = (9, 15)

DEFAULTS = {
    # Whether a banger must name or show a feeling (critic.has_feeling).
    # Measured per person, never assumed: 76% of Adi's post-length writing
    # carries one, about 1 in 10 of Khoa's verdict lines does ("LOIs do not
    # count as customers."). Off unless their own writing says otherwise.
    "require_feeling": False,
    # How often the heartbeat drafts for them. The heartbeat agent fires every
    # hour; each tenant decides whether this hour is theirs. "twice_daily" is
    # the original 09:00 and 15:00 (machine-local), `originals_per_day` drafts
    # each time, so an install that never sets this behaves exactly as before.
    # "hourly" drafts `drafts_per_run` (default 1) every hour inside their
    # posting window.
    # A softer feeling rule for a voice that is warm overall but deadpan line
    # by line: no per-draft veto, but after two shipped drafts with no feeling
    # the next one must carry one. Khoa: 66% of his own writing carries a
    # feeling, his drafts with the hard rule off came out at 37%.
    "feeling_quota": False,
    "cadence": "twice_daily",
    "drafts_per_run": None,
    # With cadence "hourly": draft only every Nth hour of the posting window,
    # counted from its start. 3 with an 08-23 window is 08, 11, 14, 17, 20.
    # Khoa, 2026-10-19: every hour "looks like a bot, nobody is online that much".
    "every_hours": 1,
    # This person's own categories, for the subject line and the topic refill.
    # Empty means the legacy four, which are Adi's and were labelling Khoa's
    # drafts "Pop culture" and "Economy".
    "lanes": [],
    # Where posted-draft metrics come from (engagement.py). "links": only drafts
    # whose post link came back in a reply. "browser": also read the account's
    # public profile through the signed-in Chrome on this machine, to find posted
    # drafts, reposts and follower counts. Never a password either way.
    "engagement_source": "links",
    # Per-person exceptions to rules that were measured on one person's
    # writing. presence_plural: "we/us/our" counts as the author being in the
    # post (Thomas writes in the plural throughout and never says "I"; all ten
    # of his real lines failed the singular-only rule). allowed_constructions:
    # SLOP_PATTERNS labels this person genuinely uses ("not-X-it's-Y
    # contrast" is Thomas's core reframe), exempted from the deterministic
    # refusal and from the critic's CLEAN list for them only.
    "presence_plural": False,
    "allowed_constructions": [],
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
    # The character ceiling for an original. None means critic.DEFAULT_MAX_CHARS,
    # which is the platform's native unit rather than a measured optimum: see the
    # note there. Per tenant because the right length is a property of whose
    # audience is reading, and the only way to find it is to watch one.
    "max_chars": None,
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
        self.require_feeling = bool(data.get("require_feeling",
                                             DEFAULTS["require_feeling"]))
        self.feeling_quota = bool(data.get("feeling_quota", DEFAULTS["feeling_quota"]))
        cadence = (data.get("cadence") or DEFAULTS["cadence"]).lower()
        if cadence not in CADENCES:
            raise ValueError(f"tenant {data.get('id')!r} has cadence {cadence!r}; "
                             f"expected one of {sorted(CADENCES)}")
        self.cadence = cadence
        dpr = data.get("drafts_per_run")
        self.drafts_per_run = int(dpr) if dpr not in (None, "") else None
        self.every_hours = max(1, int(data.get("every_hours") or 1))
        self.lanes = [str(x).strip() for x in (data.get("lanes") or []) if str(x).strip()]
        src = (data.get("engagement_source") or DEFAULTS["engagement_source"]).lower()
        if src not in ("links", "browser"):
            raise ValueError(f"tenant {data.get('id')!r} has engagement_source {src!r}")
        self.engagement_source = src
        self.presence_plural = bool(data.get("presence_plural", False))
        self.allowed_constructions = [str(x) for x in (data.get("allowed_constructions") or [])]
        raw_max = data.get("max_chars", DEFAULTS["max_chars"])
        self.max_chars = int(raw_max) if raw_max else None
        # Fallback topics for when this tenant's queue is empty. Empty by
        # default and deliberately NOT inherited: heartbeat's module-level
        # SEED_TOPICS are Adi's interests (Dieter Rams, founder posts, the
        # Lakers), so a second tenant with nothing queued would have received
        # drafts about another person's obsessions, in their own voice.
        self.seed_topics = data.get("seed_topics") or []
        # Ignore the posting window: run at every hour of every day. Off unless
        # a record asks for it, so it can never arrive by default on somebody
        # else's install.
        #
        # For watching the loop run end to end without waiting for a Saturday to
        # finish. It shifts the clock and nothing else: the halt, the daily read
        # cap, the reply cap, the notifier's daily cap and both reply-mining
        # gates all still apply, because those bound what the system may do
        # rather than when it may do it. A flag that relaxed those would not be
        # a testing convenience, it would be turning the safety rails off.
        self.always_on = bool(data.get("always_on"))
        # Their own hours, when the default does not suit. Shape:
        #   {"start": 7, "end": 21, "days": "all"}   all days, 07:00-20:59
        #   {"start": 9, "end": 18, "days": "weekdays"}
        # Absent means the built-in default in karamel_common.in_posting_window.
        # Validated here rather than trusted: a string where an hour belongs
        # would raise inside the scheduler, on a background agent, where the
        # traceback lands in a .err file nobody reads.
        win = data.get("posting_window") or None
        if win:
            try:
                start, end = int(win.get("start", 7)), int(win.get("end", 21))
                if not (0 <= start < end <= 24):
                    raise ValueError(f"start={start} end={end}")
                days = str(win.get("days", "all")).lower()
                if days not in ("all", "weekdays"):
                    raise ValueError(f"days={days!r}")
                win = {"start": start, "end": end, "days": days}
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"tenant {data.get('id')!r} has an unusable posting_window "
                    f"({e}). Expected start/end hours 0-24 with start < end, "
                    f"and days of 'all' or 'weekdays'."
                )
        self.posting_window = win
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
    def per_run(self):
        """Drafts per heartbeat run that is theirs."""
        if self.drafts_per_run is not None:
            return self.drafts_per_run
        return 1 if self.cadence == "hourly" else self.originals_per_day

    def due(self, local=None):
        """Is this hour one of theirs?

        twice_daily keeps the original machine-local 09:00 and 15:00 so other
        installs do not change behaviour when the agent starts firing hourly.
        hourly follows their posting window on THEIR clock (self.timezone), so
        a tenant set to hourly is not emailed at 03:00 where they live.

        It read Eastern time for everyone until 2026-10-20, which was right
        for two New Yorkers and wrong the moment Thomas (Europe/Berlin) was
        set up: his "08:00" would have arrived at 14:00 his time."""
        if self.cadence == "hourly":
            from karamel_common import in_posting_window
            here = local if (local is not None and local.tzinfo is not None) else self.now()
            here = here.astimezone(self.tz)
            if not in_posting_window(here, tenant=self):
                return False
            start = int((self.posting_window or {}).get("start", 7))
            return (here.hour - start) % self.every_hours == 0
        local = local or datetime.now()
        return local.hour in TWICE_DAILY_HOURS

    def send_hours(self):
        """The hours (their posting-window clock) a draft goes out, for display."""
        if self.cadence != "hourly":
            return list(TWICE_DAILY_HOURS)
        w = self.posting_window or {"start": 7, "end": 21}
        return [h for h in range(int(w["start"]), int(w["end"]))
                if (h - int(w["start"])) % self.every_hours == 0]

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
    def edits_path(self):
        """What they asked to change and what they actually posted, one row per
        reply. The learning signal from ongoing use (gen.recent_edits)."""
        return self.data_dir / "edits.jsonl"

    @property
    def samples_path(self):
        """RETIRED for the X palette on 2026-10-19: palettes now come from
        approved drafts (emotion.approved_texts), not past writing. Kept for
        callers that still pass it; the files were moved aside.

        Was: their own real writing, one {"text": ...} per line, read for the
        emotional palette and never quoted into a draft. Kept apart from the voice
        card on purpose: the card also quotes counter-examples and bad drafts
        it warns against, and a palette measured from those is contaminated."""
        return self.data_dir / "samples.jsonl"

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
            "require_feeling": self.require_feeling,
            "cadence": self.cadence, "drafts_per_run": self.drafts_per_run,
            "feeling_quota": self.feeling_quota,
            "every_hours": self.every_hours, "lanes": self.lanes,
            "engagement_source": self.engagement_source,
            "presence_plural": self.presence_plural,
            "allowed_constructions": self.allowed_constructions,
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

    # Cadence: absent behaves as it always did; hourly is one draft an hour
    # inside their window.
    plain = Tenant({"id": "z", "originals_per_day": 3})
    assert plain.cadence == "twice_daily" and plain.per_run == 3
    assert plain.due(datetime(2026, 10, 19, 9, 0)) and plain.due(datetime(2026, 10, 19, 15, 2))
    assert not plain.due(datetime(2026, 10, 19, 11, 0))
    hourly = Tenant({"id": "z", "cadence": "hourly",
                     "posting_window": {"start": 8, "end": 23}})
    assert hourly.per_run == 1
    assert Tenant({"id": "z", "cadence": "hourly", "drafts_per_run": 2}).per_run == 2
    every3 = Tenant({"id": "z", "cadence": "hourly", "every_hours": 3,
                     "posting_window": {"start": 8, "end": 23}})
    assert every3.send_hours() == [8, 11, 14, 17, 20], every3.send_hours()
    assert hourly.send_hours() == list(range(8, 23))
    # Their clock, not New York's.
    berlin = Tenant({"id": "z", "cadence": "hourly", "every_hours": 3,
                     "timezone": "Europe/Berlin",
                     "posting_window": {"start": 8, "end": 23, "days": "all"}})
    ny = ZoneInfo("America/New_York")
    assert berlin.due(datetime(2026, 10, 21, 2, 0, tzinfo=ny))      # 08:00 Berlin
    assert not berlin.due(datetime(2026, 10, 21, 9, 0, tzinfo=ny))  # 15:00 Berlin, not a send hour
    assert berlin.due(datetime(2026, 10, 21, 8, 0, tzinfo=ZoneInfo("Europe/Berlin")))
    nyc = Tenant({"id": "z", "cadence": "hourly", "every_hours": 3,
                  "timezone": "America/New_York",
                  "posting_window": {"start": 8, "end": 23, "days": "all"}})
    assert nyc.due(datetime(2026, 10, 21, 8, 0, tzinfo=ny))
    assert not nyc.due(datetime(2026, 10, 21, 2, 0, tzinfo=ny))
    assert Tenant({"id": "z", "lanes": ["VC", " ", "Deep tech"]}).lanes == ["VC", "Deep tech"]
    try:
        Tenant({"id": "z", "cadence": "every-minute"})
        raise AssertionError("an unknown cadence must fail at load")
    except ValueError:
        pass

    # always_on is off unless a record asks for it, so it cannot arrive by
    # default on somebody else's install.
    assert Tenant({"id": "z"}).always_on is False
    assert Tenant({"id": "z", "always_on": True}).always_on is True
    assert Tenant({"id": "z", "always_on": False}).always_on is False

    # It moves the clock and nothing else. These bound what the system may do
    # rather than when, and a testing flag that relaxed them would be turning
    # the safety rails off under a name that sounds like a schedule.
    loud = Tenant({"id": "z", "always_on": True})
    assert loud.reply_mining is False, "always_on must not open the reply gate"
    assert loud.max_reads_per_day == 100, "always_on must not raise the read cap"

    # A tenant's own hours. Absent means the built-in default; present, it is
    # normalised here so a bad value fails at load rather than inside a
    # background agent, where the traceback goes to a .err file nobody reads.
    assert Tenant({"id": "z"}).posting_window is None
    w = Tenant({"id": "z", "posting_window": {"start": 7, "end": 21}}).posting_window
    assert w == {"start": 7, "end": 21, "days": "all"}, w
    w2 = Tenant({"id": "z", "posting_window":
                 {"start": "9", "end": "18", "days": "WEEKDAYS"}}).posting_window
    assert w2 == {"start": 9, "end": 18, "days": "weekdays"}, w2
    for bad in ({"start": 21, "end": 7}, {"start": -1, "end": 9},
                {"start": 0, "end": 25}, {"days": "sometimes"},
                {"start": "morning"}):
        try:
            Tenant({"id": "z", "posting_window": bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted an unusable window: {bad}")

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
