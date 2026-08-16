"""Shared helpers for Karamel v2.1 read-side components (listener, drafter).

Delegates ALL cross-component state (pause/halt, JSONL IO, em-dash, compose
URL, paths, time) to shared.py so there is ONE source of truth with the
notifier and poller. This module only adds what's specific to the v2.1
read side: CDP/list config, the per-tenant read counter, the posting-window
gate, and the tripwire halt. Tier-1 targets moved to the tenant record: they
name real people one specific customer cares about and do not belong in code
that ships to another.

Machine-agnostic: shared.py resolves paths from Path.home(), so the same
code runs on a daily-driver Mac and on the always-on host that runs it,
whatever each machine's account is called.
"""
import json
from datetime import datetime

# Single source of truth for shared state. Re-export the primitives the
# listener/drafter use so callers import everything from karamel_common.
from shared import (  # noqa: F401
    DATA, PROJECT, CONFIG_DIR, ET,
    append_jsonl, read_jsonl, has_em_dash, compose_url, now_iso, now_utc,
    load_pause_state, save_pause_state, send_message,
)

ROOT = PROJECT

DEFAULTS = {
    # No default. It identifies a person, and a shipped default means a
    # fresh install silently scrapes a stranger's list. Set it per tenant.
    "list_id": "",
    "cdp_port": 9222,
    "max_tweets_per_run": 30,
    # Halved from 200 in cycle 4 (doc 15 §5.5) after the platform-manipulation
    # label. The original note assumed the posting window would bind first; on
    # 2026-08-10 the cap bound at 14:31 ET with 6.5h of weekday window left.
    # Live value is overridden to 200 in ~/.config/karamel/karamel.json, which
    # load_config() layers over these defaults. Check there before trusting 100.
    "max_reads_per_day": 100,
    "max_age_minutes": 60,
    "min_likes_non_tier1": 5,
    # A repost puts a stranger in front of him. It has to clear a much higher
    # bar than someone he chose to follow.
    "repost_min_likes": 150,
    # Tier 3 means "cheap checks were inconclusive, needs a relevance judgment".
    # Off until something makes that judgment, because the only thing that can
    # today is the drafter, at one model call per post.
    "keep_needs_relevance": False,
}



def now_et():
    return datetime.now(ET)


def load_config():
    cfg = dict(DEFAULTS)
    cfg_path = CONFIG_DIR / "karamel.json"
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def in_posting_window(dt=None, tenant=None):
    """Mon-Fri 7am-9pm ET, Sun 4pm-9pm ET. Saturday is off entirely.

    The single source of truth for "is the system awake". notifier.
    skip_window_reason delegates here and only names the reason, so the two
    cannot disagree. They did for months: its Sunday branch had no upper bound.

    A tenant carrying always_on is awake at every hour of every day. It exists
    so one person can watch the whole loop run repeatedly instead of waiting for
    a Saturday to end, and it is per tenant rather than a global edit so that
    turning it on for the operator cannot quietly turn it on for everyone else.

    It moves ONLY the clock. The halt, the daily read cap, the reply cap, the
    notifier's daily cap and both reply-mining gates are untouched, because they
    are what bound behaviour rather than schedule it. Read with getattr so this
    file keeps knowing nothing about the tenant module.
    """
    if tenant is not None and getattr(tenant, "always_on", False):
        return True
    dt = dt or now_et()

    # A tenant may state its own hours. The default below is one person's
    # habit, not a law, and the alternative to a config field is editing this
    # function for every install, which is how one tenant's preference becomes
    # everybody's schedule.
    #
    # days: "all" is every day including Saturday; "weekdays" is Mon-Fri. The
    # window is half open, start <= hour < end, so end=21 means nothing fires
    # at 21:00 and the last run is in the 20:xx hour.
    win = getattr(tenant, "posting_window", None) if tenant is not None else None
    if win:
        start = int(win.get("start", 7))
        end = int(win.get("end", 21))
        if win.get("days", "all") == "weekdays" and dt.weekday() > 4:
            return False
        return start <= dt.hour < end

    wd, hr = dt.weekday(), dt.hour  # Mon=0 .. Sun=6
    if wd <= 4:
        return 7 <= hr < 21
    if wd == 6:
        return 16 <= hr < 21
    return False


def _read_pause(path=None):
    """Pause state from a specific file, or the shared one when path is None."""
    if path is None:
        return load_pause_state()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Fail closed. An unreadable pause file must read as paused, never as
        # permission to keep scraping.
        return {"halt": True, "halt_reason": f"unreadable pause state at {path}"}


def is_paused(tenant=None):
    """Bool view of pause state. Honors halt, vacation (paused_indefinitely),
    and timed pauses, so the read side stops whenever the notifier would.

    With a tenant, the answer is the OR of the shared state and that tenant's
    own. Global stays a superset on purpose: /halt from a phone is the kill
    switch for the whole box, while a tripwire on one tenant's X session should
    stop that tenant and leave everyone else running."""
    if tenant is not None and _paused_state(_read_pause(tenant.pause_path)):
        return True
    st = load_pause_state()
    if st.get("halt") or st.get("paused_indefinitely"):
        return True
    return _paused_state(st)


def _paused_state(st):
    if st.get("halt") or st.get("paused_indefinitely"):
        return True
    pu = st.get("paused_until")
    if pu:
        try:
            return datetime.fromisoformat(pu.replace("Z", "+00:00")) > now_utc()
        except ValueError:
            return False
    return False


def set_halt(reason, tenant=None):
    """Tripwire: HALT. Without a tenant this is the old behaviour, a full stop
    written to the shared state. With one, it halts THAT tenant only.

    Scoping matters here. A login wall on one person's X session says their
    session expired, not that the machine is compromised, and halting everyone
    for it means one customer's expired cookie takes the whole product down.
    The reason and timestamp fields are unchanged, so the test that tells a
    tripwire from a human typing /halt still works: set_halt writes halt_reason
    and halted_at, the poller's /halt writes halt_set_ts."""
    if tenant is None or tenant.is_legacy:
        st = load_pause_state()
        st["halt"] = True
        st["halt_reason"] = reason
        st["halted_at"] = now_iso()
        save_pause_state(st)
        send_telegram_if_configured(f"🛑 TRIPWIRE HALT: {reason}. /resume to restart.")
        return
    st = _read_pause(tenant.pause_path)
    st.update({"halt": True, "halt_reason": reason, "halted_at": now_iso()})
    tenant.pause_path.parent.mkdir(parents=True, exist_ok=True)
    tenant.pause_path.write_text(json.dumps(st, indent=2))
    send_telegram_if_configured(
        f"🛑 TRIPWIRE HALT for {tenant.id}: {reason}. Other tenants keep running."
    )


def send_telegram_if_configured(text):
    """Best-effort alert. Creds exist only on the host Mac; on the daily Mac
    (no creds) degrade to stdout instead of crashing the component."""
    if not (CONFIG_DIR / "telegram.json").exists():
        print(f"[no telegram creds] would have sent: {text}")
        return
    try:
        send_message(text)
    except Exception as e:  # alert path must never crash the caller
        print(f"[telegram alert failed: {e}] message was: {text}")


def _reads_path(tenant=None):
    date = now_et().date().isoformat()
    if tenant is None:
        return CONFIG_DIR / f"reads_{date}.txt"
    return tenant.reads_path(date)


def reads_today(tenant=None):
    """This tenant's reads today. Keyed per tenant because the cap is a
    per-account safety limit: one shared counter meant a second tenant's
    scraping consumed the first's budget, and whoever ran second got cut off
    for activity that was not theirs."""
    try:
        return int(_reads_path(tenant).read_text().strip())
    except (OSError, ValueError):
        return 0


def add_reads(n, tenant=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _reads_path(tenant).write_text(str(reads_today(tenant) + n))

