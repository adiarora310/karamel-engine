"""Karamel notifier: sends the reply drafts, one email each.

Pre-flight: git pull, files exist, skip-window, pause state, daily cap.
Find pending+un-notified drafts, sort by age, take 12, re-run anti-pattern filter.
One message per draft, capturing the delivery id into drafts.jsonl.

Flags: --force (skip the posting window, for manual testing; the halt, the
daily cap and the safety gate are still enforced)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from karamel_common import in_posting_window
import tenants
from safety import MAX_REPLIES_PER_DAY, reply_allowed, reply_count_today
from shared import (
    DATA, DRAFTS, ET, PROJECT, TELEGRAM_CFG,
    anti_pattern_hit, compose_url, has_em_dash, is_paused,
    now_iso, now_utc, read_jsonl, short_id,
    write_jsonl_atomic,
)

MAX_BATCH = 12
DAILY_CAP = 50   # Messages a day. NOT a reply cap: see below.

# This file pushes reply drafts (DRAFTS = drafts.jsonl). Originals go out via
# heartbeat.py from original_drafts.jsonl and are not subject to the reply cap.
# Whose budget and files this bills. Reads the owner of this installation
# rather than a name compiled in: on a self-hosted copy the operator is not
# called "adi", and hardcoding it billed their replies to a tenant that does not
# exist on their machine while reading paths that are not theirs.
TENANT = tenants.LEGACY_TENANT


def skip_window_reason(dt=None, tenant=None) -> str | None:
    """Why the notifier should stay quiet, or None to send. `dt` is an ET-aware
    datetime, defaulting to now; it exists so the agreement with
    in_posting_window can be tested at a chosen time rather than only at the
    one the test happens to run.

    The decision is delegated to karamel_common.in_posting_window so the two
    cannot drift. They had drifted, and in the direction that produces work
    nothing else is awake to produce: this function's Sunday branch tested only
    `h < 16` and had no upper bound, while in_posting_window caps Sunday at 9pm.
    So every Sunday between 9pm and midnight the notifier pushed drafts that the
    listener and drafter were gated off from making. Observed live on
    2026-08-09 at 22:25 ET during the cycle-4 reactivation, with the listener
    logging "outside posting window, exiting" while the notifier sent a batch.

    in_posting_window's docstring had claimed "Matches notifier.
    skip_window_reason" since v2.1. It was not true. It is now, by construction.
    This function only names the reason; it no longer decides.
    """
    et = dt or now_utc().astimezone(ET)
    # The tenant reaches in_posting_window so an always_on record is awake here
    # too. Delegating the decision is the whole point of this function: the two
    # drifted once already and the notifier pushed drafts on a Sunday night that
    # nothing else was awake to have made.
    if in_posting_window(et, tenant=tenant):
        return None
    weekday = et.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:
        return "Saturday quiet day"
    if weekday == 6:
        return "Sunday outside 4pm-9pm ET"
    return "weekday outside 7am-9pm ET"


def daily_counter_path() -> Path:
    return DATA / f"notifier_count_{now_utc().astimezone(ET).strftime('%Y-%m-%d')}.txt"


def read_counter() -> int:
    p = daily_counter_path()
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip() or "0")
    except ValueError:
        return 0


def write_counter(n: int) -> None:
    p = daily_counter_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n))


def git_pull() -> None:
    try:
        res = subprocess.run(
            ["git", "-C", str(PROJECT), "pull", "--ff-only"],
            check=False, capture_output=True, timeout=30, text=True,
        )
    except subprocess.TimeoutExpired:
        print("git pull timed out, continuing with local copy", file=sys.stderr)
        return
    if res.returncode != 0:
        # --ff-only refuses the moment the branch diverges, i.e. whenever this
        # box holds a commit origin lacks. Deploys then freeze with no other
        # symptom, so this is the only place that failure is visible.
        detail = " ".join((res.stderr or res.stdout or "").split())[:300]
        print(
            f"git pull --ff-only failed (rc={res.returncode}), "
            f"continuing with local copy: {detail or 'no output'}",
            file=sys.stderr,
        )


def lane_label(draft) -> str:
    """One of the voice card's four lanes, for the subject line.

    lane_fit is free text from the model and reads like
    "single-lane economy (media) with an entrepreneurship/distribution angle",
    which is a useful note and an unreadable subject. This maps it back onto
    the four canonical lanes by scanning for their names, first match winning,
    so the reply subject says the same kind of thing an original's does."""
    import heartbeat

    blob = " ".join(draft.get("lane_fit") or []).lower()
    for lane in heartbeat.LANES:
        if lane.lower().replace(" ", "") in blob.replace("-", "").replace(" ", ""):
            return lane
    # Nothing recognisable: ask, rather than mislabel. Costs one short call and
    # only on a draft already worth sending.
    return heartbeat.classify_lane(
        draft.get("original_text") or "", draft.get("draft_text") or "")


def format_draft(draft: dict) -> str:
    """The same shape as an original draft, plus the post being answered.

    Tier, age, likes, lane-fit and the batch counter are gone. They are
    operator diagnostics, every one of them is on the status page, and in an
    email they were the first two lines a person read: a reply is judged on
    what it answers and what it says.

    The post being answered stays, because that is the one thing you cannot
    judge the reply without, and opening X to find it defeats the point."""
    handle = draft.get("handle") or "@" + str(draft.get("author_handle") or "?")
    original = (draft.get("original_text") or "").strip()
    draft_text = (draft.get("draft_text") or "").strip()
    # Threaded, via in_reply_to. An original opens a blank composer; a reply
    # that posts as a standalone tweet is a different thing entirely.
    url = compose_url(draft["tweet_id"], draft_text)
    did = draft.get("draft_id") or short_id(draft.get("tweet_id"))
    return (
        f"Summary: Reply to {handle}\n\n"
        f"They posted: \"{original}\"\n\n"
        # Labelled, unlike an original. The post being answered can run several
        # paragraphs, so without this the reply reads as a continuation of the
        # quote rather than as the thing being approved.
        f"Here's your reply:\n\n"
        f"{draft_text}\n\n"
        f"Click on the link to post it: {url}\n\n"
        f"Reply to this email with one word or emoji:\n"
        f"Posted or ✅: You published it as written.\n"
        f"Skip or ❌: You binned it.\n"
        f"Edited or ✏️: Followed by what you actually posted.\n\n"
        f"Draft #{did}"
    )


def _tenant():
    """Whose replies these are. The reply half predates the registry and sent
    straight to the one configured Telegram chat, which is fine for one person
    and silently wrong for two."""
    try:
        import tenants
        return tenants.load_tenant(tenants.LEGACY_TENANT)
    except Exception:
        return None


def _deliver(tenant, text, subject=None):
    """Send through the tenant's own channel, or return None.

    This used to call send_message directly, which is Telegram. An email-only
    install therefore drafted replies and had nowhere to put them: the files
    accumulated, the person saw nothing, and no error said so. Same failure the
    original-content path had before it moved to channels."""
    try:
        import channels
        return channels.send(tenant, text, subject=subject)
    except Exception as e:
        print(f"send failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    if not DRAFTS.exists():
        # Not an error. The file appears the first time the drafter writes a
        # reply, so on a fresh install it is simply absent, and every twenty
        # minutes this wrote "drafts.jsonl missing" to stderr and exited 1.
        # The watchdog reads new stderr, found no failing check to attach it to,
        # and mailed a person "something doesn't look right" whose only content
        # was a log line meaning nothing has happened yet.
        print("no reply drafts yet")
        return 0
    tenant = _tenant()
    kind = ((tenant.channel if tenant else None) or {}).get("type", "none")
    if kind == "none":
        print("no delivery channel configured for this tenant", file=sys.stderr)
        return 1
    if kind == "telegram" and not TELEGRAM_CFG.exists():
        print("telegram.json missing", file=sys.stderr)
        return 1

    # --force skips the posting window and nothing else, matching what the flag
    # already means in listener.py and drafter.py. Without it the reply half was
    # untestable for two days a week: on a Saturday the window is closed all
    # day, so a draft could be scraped and written and then sat on with no way
    # to see it arrive short of waiting for Monday.
    #
    # The halt, the daily cap and the safety gate below are NOT skipped. A
    # manual test that can push past a tripwire is not a test, it is the thing
    # the tripwire exists to stop.
    force = "--force" in sys.argv
    skip = skip_window_reason(tenant=tenant)
    if skip and not force:
        print(f"skip window: {skip}")
        return 0
    if skip and force:
        print(f"window closed ({skip}), forced")

    paused, reason = is_paused()
    if paused:
        print(f"notifier paused: {reason}")
        return 0

    used = read_counter()
    if used >= DAILY_CAP:
        print(f"{used}/{DAILY_CAP} daily cap reached")
        return 0

    # The reply gate. Until cycle 4 nothing in this tree called it, so the
    # 10/day cap in safety.py was decorative and DAILY_CAP=50 above was the only
    # real limit, five times looser and counting the wrong thing (messages
    # pushed, not replies sent). Checked here so a closed gate costs one log
    # line instead of a batch. Fails closed on an unreadable counter.
    gate_ok, gate_why = reply_allowed(TENANT)
    if not gate_ok:
        print(f"reply gate closed for tenant '{TENANT}': {gate_why}")
        return 0

    git_pull()

    rows = read_jsonl(DRAFTS)
    # collect pending+un-notified
    pending_idx = [
        i for i, r in enumerate(rows)
        if r.get("status") == "pending" and not r.get("notified_ts")
    ]
    if not pending_idx:
        print("no pending drafts to push")
        return 0

    # sort by age ascending (freshest first)
    pending_idx.sort(key=lambda i: rows[i].get("age_minutes") or 99999)

    # remaining capacity: message budget, and the reply budget it now respects.
    # Pushing 12 drafts when only 2 more replies can be sent today is not a
    # neutral act, it is 10 invitations to blow the cap by hand.
    remaining = DAILY_CAP - used
    replies_left = MAX_REPLIES_PER_DAY - reply_count_today(TENANT)
    batch_cap = min(MAX_BATCH, remaining, replies_left)
    print(f"budget: {replies_left} replies left today, pushing at most {batch_cap}")
    pending_idx = pending_idx[: batch_cap * 2]  # extra room for filter rejects

    # anti-pattern filter — reject in place, exclude from batch
    accepted: list[int] = []
    for i in pending_idx:
        if len(accepted) >= batch_cap:
            break
        text = rows[i].get("draft_text") or ""
        hit = anti_pattern_hit(text)
        if hit:
            rows[i]["status"] = "skip"
            rows[i]["skip_reason"] = f"notifier_filter_tripped: {hit}"
            print(f"row {i}: filter tripped ({hit}), marked skip")
            continue
        accepted.append(i)

    if not accepted:
        # persist any filter-tripped marks
        write_jsonl_atomic(DRAFTS, rows)
        print("no pending drafts to push (all filter-tripped)")
        return 0

    n = len(accepted)

    # No batch header. It was one line, "[BATCH 09:14 ET . 2 drafts . 0
    # Tier-1]", and it made sense on Telegram where a batch lands as a burst in
    # one chat and a marker separates it from conversation. In a mailbox it is
    # a near-empty message that costs an unread and carries nothing to act on.
    #
    # It also doubled as a reachability probe: if the header failed to send,
    # this returned before attempting the drafts. That is not lost. Each
    # per-draft send is checked on its own below, and a channel that is down
    # fails the first one, which is the same signal one message later.

    # per-draft sends
    sent_count = 0
    for k, i in enumerate(accepted, start=1):
        draft = rows[i]
        # assign draft_id if missing. Drafter-written rows already carry a
        # millisecond timestamp; this only fires for a row from somewhere else,
        # and a short id here cannot be recovered from the body of a reply,
        # because DRAFT_ID_RE wants ten digits or more.
        if not draft.get("draft_id"):
            draft["draft_id"] = short_id(draft.get("tweet_id"))
        text = format_draft(draft)
        if has_em_dash(text):
            print(f"ABORT row {i}: em-dash in formatted message", file=sys.stderr)
            continue
        # The lane, not the handle and not the id. Same shape as an original,
        # and the id now rides on the last line of the body instead.
        msg_id = _deliver(tenant, text,
                          subject=f"[Karamel] Your reply is ready to post! "
                                  f"{lane_label(draft)}")
        if msg_id is None:
            print(f"row {i}: send failed", file=sys.stderr)
            continue
        # mark notified in-place
        draft["notified_ts"] = now_iso()
        draft["notifier_message_id"] = msg_id  # legacy field
        # Legacy key name. It holds whatever id the channel returned, which is
        # a Message-ID on email; inbox.locate_draft and reflector both match on
        # it, so renaming it means migrating every row already on disk.
        draft["telegram_msg_id"] = msg_id
        draft["compose_url"] = compose_url(draft["tweet_id"], draft.get("draft_text", ""))
        sent_count += 1

    # persist
    write_jsonl_atomic(DRAFTS, rows)
    write_counter(used + sent_count)
    print(f"sent {sent_count}/{n} drafts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
