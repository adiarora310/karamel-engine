"""Karamel poller: the Telegram receive side.

Not scheduled on an email install, and kept because inbox.py imports the token
vocabulary from here. Two parsers for the same three answers would drift, and
the drift would be silent.

Long-polls Telegram getUpdates, routes each message to:
  - draft-reply handler (reply_to_message matches a known telegram_msg_id)
  - command handler (text starts with /)
  - fallback help

State: ~/.config/karamel/poller_state.json (last_update_id).
Pause state: ~/.config/karamel/pause_state.json (paused_until, paused_indefinitely, halt).

Each run is one pass: one long-poll cycle with timeout=20, processes the batch, exits.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import tenants
from safety import MAX_REPLIES_PER_DAY, record_reply, reply_count_today
from shared import (
    DATA, DRAFTS, ENGAGEMENT, EVAL_QUEUE, ET, POLLER_STATE,
    URL_RE, append_jsonl, is_halted, is_paused, load_pause_state,
    now_et_str, now_iso, now_utc, parse_pause_duration, read_jsonl,
    save_pause_state, send_message, tg, write_jsonl_atomic,
)

LONG_POLL_TIMEOUT = 20

# Whose budget and files this bills. Reads the owner of this installation
# rather than a name compiled in: on a self-hosted copy the operator is not
# called "adi", and hardcoding it billed their replies to a tenant that does not
# exist on their machine while reading paths that are not theirs.
TENANT = tenants.LEGACY_TENANT

# Cycle 5: original posts from the heartbeat live here. Approvals resolve across
# both this and the reply log, so ties get flipped in whichever file holds them.
ORIGINAL_DRAFTS = DATA / "original_drafts.jsonl"

# Token detection on draft replies
RE_CHECK = re.compile(r"(✅|\bposted\b|\bp\b)", re.IGNORECASE)
RE_EDIT  = re.compile(r"(✏️|\bedited\b|\be\b)", re.IGNORECASE)
RE_SKIP  = re.compile(r"(❌|\bskip\b|\bskipped\b|\bs\b)", re.IGNORECASE)


def load_poller_state() -> dict:
    if not POLLER_STATE.exists():
        return {"last_update_id": 0}
    with open(POLLER_STATE) as f:
        return json.load(f)


def save_poller_state(state: dict) -> None:
    POLLER_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLLER_STATE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, POLLER_STATE)
    os.chmod(POLLER_STATE, 0o600)


def find_draft_by_msg_id(rows: list[dict], msg_id: int) -> int | None:
    for i, r in enumerate(rows):
        if r.get("telegram_msg_id") == msg_id:
            return i
        if r.get("notifier_message_id") == msg_id and r.get("status") in ("pending",):
            # legacy bundle drafts won't have telegram_msg_id; allow matching by notifier_message_id only if unambiguous
            pass
    return None


def find_draft_across(msg_id: int):
    """Find a draft by telegram_msg_id across the reply log and the original-post
    log. Returns (path, rows, index) or (None, None, None). DRAFTS is checked
    first, so reply-draft resolution is unchanged; original posts are found next."""
    for path in (DRAFTS, ORIGINAL_DRAFTS):
        rows = read_jsonl(path)
        i = find_draft_by_msg_id(rows, msg_id)
        if i is not None:
            return path, rows, i
    return None, None, None


def evaluator_due_at() -> str:
    return (now_utc() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluator_due_et_str() -> str:
    return (now_utc() + timedelta(hours=24)).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")


def queue_evaluator(draft: dict) -> str:
    """Append evaluator job to evaluator_queue.jsonl. Returns due ET string."""
    due_iso = evaluator_due_at()
    append_jsonl(EVAL_QUEUE, {
        "queued_ts": now_iso(),
        "draft_id": draft.get("draft_id"),
        "tweet_id": draft.get("tweet_id"),
        "reply_url": draft.get("reply_url"),
        "scheduled_for": due_iso,
        "status": "queued",
    })
    return evaluator_due_et_str()


def parse_reply_tokens(text: str) -> tuple[str | None, str | None, str | None]:
    """Returns (status, edited_text, reply_url).

    status ∈ {posted_clean, posted_edited, skipped} or None.
    edited_text is the final posted text when status == posted_edited.
    reply_url extracted from URL_RE if present.
    """
    text = text or ""

    # extract URL
    url_match = URL_RE.search(text)
    reply_url = url_match.group(0) if url_match else None
    # remove URL from token-detect text
    clean = URL_RE.sub("", text).strip()

    edit_m = RE_EDIT.search(clean)
    if edit_m:
        # Edit text is everything after the edit token
        after = clean[edit_m.end():].strip()
        if after:
            return "posted_edited", after, reply_url
        # if no follow-up text, treat as malformed → still log status with no edit text
        return "posted_edited", None, reply_url

    if RE_SKIP.search(clean):
        return "skipped", None, reply_url
    if RE_CHECK.search(clean):
        return "posted_clean", None, reply_url
    return None, None, reply_url


def handle_draft_reply(message: dict, replied_msg_id: int) -> None:
    path, rows, i = find_draft_across(replied_msg_id)
    if i is None:
        send_message(
            "Couldn't find the draft this is replying to. It may have been from "
            "before §12 went live (bundled batch). Reply to drafts sent after the "
            "switch and confirmations will work.",
            reply_to_message_id=message["message_id"],
        )
        return

    text = message.get("text", "")
    status, edited_text, reply_url = parse_reply_tokens(text)
    if status is None:
        send_message(
            "Reply not recognized. Use ✅ posted / ✏️ <edit> / ❌ skip. "
            "Optionally include the X URL.",
            reply_to_message_id=message["message_id"],
        )
        return

    draft = rows[i]

    # Special-case: §12 LIVE handshake message. No engagement/queue write; bespoke ack.
    if draft.get("_live_confirm"):
        draft["status"] = "live_confirmed"
        draft["confirmed_ts"] = now_iso()
        write_jsonl_atomic(path, rows)
        send_message(
            "✅ Round-trip confirmed. §12 is LIVE.\n\n"
            "From here on:\n"
            "  • Notifier sends drafts one-per-message — reply ✅ / ✏️ <edit> / ❌\n"
            "  • /status anytime · /pause Xh · /resume · /help · /halt\n"
            "  • This Mac runs autonomously. You operate from your phone.",
            reply_to_message_id=message["message_id"],
        )
        return

    draft["status"] = status
    draft["confirmed_ts"] = now_iso()
    draft["confirmed_by"] = "telegram_poller"
    if edited_text:
        draft["edited_text"] = edited_text
    if reply_url:
        draft["reply_url"] = reply_url

    # append to engagement.jsonl if posted
    eval_eta = ""
    budget_note = ""
    # Engagement + evaluator apply to REPLY drafts only (they need tweet_id). An
    # original post from the heartbeat captures status + edited_text and stops here.
    if status in ("posted_clean", "posted_edited") and draft.get("tweet_id"):
        # This is the only moment we learn a reply actually went out: Adi sends
        # it by hand, then confirms here. So this is where the daily counter is
        # incremented. Skips do not count, originals do not count, and the
        # notifier reads this counter back before pushing the next batch.
        n = record_reply(TENANT)
        budget_note = f" · {n}/{MAX_REPLIES_PER_DAY} replies today"
        if n >= MAX_REPLIES_PER_DAY:
            budget_note += " (cap reached, no more drafts until tomorrow)"
        eng = {
            "ts_iso": now_iso(),
            "draft_id": draft.get("draft_id"),
            "tweet_id": draft.get("tweet_id"),
            "handle": draft.get("handle"),
            "author": draft.get("author"),
            "tier": draft.get("tier"),
            "status": status,
            "original_text": draft.get("original_text"),
            "draft_text": draft.get("draft_text"),
            "posted_text": edited_text or draft.get("draft_text"),
            "reply_url": reply_url,
            "lane_fit": draft.get("lane_fit", []),
        }
        append_jsonl(ENGAGEMENT, eng)
        eval_eta = queue_evaluator(draft)

    write_jsonl_atomic(path, rows)

    did = draft.get("draft_id") or "?"
    sched = f" · evaluator scheduled {eval_eta}" if eval_eta else ""
    if status == "posted_clean":
        ack = f"✅ logged for draft #{did} · posted clean{sched}{budget_note}"
    elif status == "posted_edited":
        ack = f"✏️ logged for draft #{did} · posted edited{sched}{budget_note}"
        if edited_text:
            ack += f"\nedited text saved ({len(edited_text)} chars)"
    else:
        ack = f"❌ logged for draft #{did} · skipped"
    if reply_url and status != "skipped":
        ack += "\nreply URL captured"
    send_message(ack, reply_to_message_id=message["message_id"])


def render_status() -> str:
    paused, reason = is_paused()
    halted = is_halted()
    drafts = read_jsonl(DRAFTS)
    pending = sum(1 for r in drafts if r.get("status") == "pending")
    pending_unnotified = sum(
        1 for r in drafts
        if r.get("status") == "pending" and not r.get("notified_ts")
    )
    awaiting_conf = sum(
        1 for r in drafts
        if r.get("status") == "pending" and r.get("telegram_msg_id")
    )
    posted_today = sum(
        1 for r in drafts
        if r.get("status") in ("posted_clean", "posted_edited")
        and (r.get("confirmed_ts") or "").startswith(
            now_utc().astimezone(ET).strftime("%Y-%m-%d")
        )
    )
    notif_count = 0
    today_file = DATA / f"notifier_count_{now_utc().astimezone(ET).strftime('%Y-%m-%d')}.txt"
    if today_file.exists():
        try:
            notif_count = int(today_file.read_text().strip() or "0")
        except ValueError:
            pass

    lines = [
        f"📊 Karamel status · {now_et_str('%a %H:%M ET')}",
        "",
        f"Notifier: {'⏸ ' + reason if paused else '▶ running'}",
        f"X-touch halt: {'🛑 YES' if halted else 'no'}",
        f"Drafts pending: {pending} (unnotified: {pending_unnotified}, awaiting confirm: {awaiting_conf})",
        f"Posted today: {posted_today}",
        f"Notifier sends today: {notif_count}/50",
        f"Reply budget: {reply_count_today(TENANT)}/{MAX_REPLIES_PER_DAY} used",
    ]
    return "\n".join(lines)


HELP_TEXT = (
    "Karamel commands:\n"
    "  ✅ / ✏️ <text> / ❌  → reply to a draft to log it (posted/edited/skipped)\n"
    "  /status — current state\n"
    "  /pause 2h | /pause 30m | /pause 24h — halt notifier\n"
    "  /pause vacation — halt notifier indefinitely\n"
    "  /resume — re-enable notifier (also clears halt)\n"
    "  /halt — emergency stop ALL X-touching components (listener, evaluator)\n"
    "  /clear  wipe all pending drafts so nothing is left to action\n"
    "  /reflect — metrics (clean/edit/skip rate) + proposed voice refinements\n"
    "  /help — this message"
)


def handle_command(message: dict) -> None:
    text = (message.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@", 1)[0]  # strip @botname if Telegram added it
    arg = parts[1].strip() if len(parts) > 1 else ""
    reply_to = message["message_id"]

    if cmd == "/help":
        send_message(HELP_TEXT, reply_to_message_id=reply_to)
        return

    if cmd == "/status":
        send_message(render_status(), reply_to_message_id=reply_to)
        return

    if cmd == "/halt":
        st = load_pause_state()
        st["halt"] = True
        st["halt_set_ts"] = now_iso()
        save_pause_state(st)
        send_message(
            "🛑 HALT. All X reads stopped (listener, evaluator). "
            "Notifier still sends queued drafts. /resume to restart everything.",
            reply_to_message_id=reply_to,
        )
        return

    if cmd == "/resume":
        save_pause_state({
            "paused_until": None,
            "paused_indefinitely": False,
            "halt": False,
            "resumed_ts": now_iso(),
        })
        send_message("▶ resumed. Next batch on schedule.", reply_to_message_id=reply_to)
        return

    if cmd == "/pause":
        if not arg:
            send_message(
                "Usage: /pause 2h | /pause 30m | /pause 24h | /pause vacation",
                reply_to_message_id=reply_to,
            )
            return
        if arg.lower() == "vacation":
            save_pause_state({
                "paused_until": None,
                "paused_indefinitely": True,
                "halt": load_pause_state().get("halt", False),
                "set_at": now_iso(),
                "set_by_command": text,
            })
            send_message(
                "⏸ paused indefinitely. /resume to restart.",
                reply_to_message_id=reply_to,
            )
            return
        delta = parse_pause_duration(arg)
        if delta is None:
            send_message(
                "Couldn't parse duration. Try 30m, 2h, 24h, or vacation.",
                reply_to_message_id=reply_to,
            )
            return
        until = now_utc() + delta
        save_pause_state({
            "paused_until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paused_indefinitely": False,
            "halt": load_pause_state().get("halt", False),
            "set_at": now_iso(),
            "set_by_command": text,
        })
        send_message(
            f"⏸ paused until {until.astimezone(ET).strftime('%H:%M ET (%Y-%m-%d)')}",
            reply_to_message_id=reply_to,
        )
        return

    if cmd in ("/clear", "/cleardrafts"):
        rows = read_jsonl(DRAFTS)
        n = 0
        for r in rows:
            if r.get("status") == "pending":
                r["status"] = "cleared"
                r["cleared_ts"] = now_iso()
                r["confirmed_by"] = "bulk_clear"
                n += 1
        if n:
            write_jsonl_atomic(DRAFTS, rows)
        send_message(
            f"🧹 Cleared {n} pending draft(s). Nothing left pending."
            if n else "Nothing pending to clear.",
            reply_to_message_id=reply_to,
        )
        return

    if cmd in ("/reflect", "/metrics"):
        script = Path(__file__).resolve().parent / "reflector.py"
        try:
            subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(script.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            send_message(
                "🔍 Reflecting on your recent drafts. Metrics and any voice "
                "refinements land here in a moment.",
                reply_to_message_id=reply_to,
            )
        except Exception as e:
            send_message(
                f"Couldn't start reflection: {e}", reply_to_message_id=reply_to
            )
        return

    # Unknown command
    send_message(
        f"Unknown command: {cmd}\n\n" + HELP_TEXT,
        reply_to_message_id=reply_to,
    )


def handle_fallback(message: dict) -> None:
    send_message(
        "Reply to a specific draft message to log it (✅ / ✏️ / ❌). "
        "Or use /help for commands.",
        reply_to_message_id=message["message_id"],
    )


def route_message(message: dict) -> None:
    text = (message.get("text") or "").strip()
    rtm = message.get("reply_to_message")
    rtm_id = rtm.get("message_id") if rtm else None

    if rtm_id is not None:
        # Is it a reply to a known draft (reply log or original-post log)?
        _p, _r, idx = find_draft_across(rtm_id)
        if idx is not None:
            handle_draft_reply(message, rtm_id)
            return
        # reply to something else (e.g., a previous bot ack). Fall through.

    if text.startswith("/"):
        handle_command(message)
        return

    handle_fallback(message)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--no-poll":
        # used by tests: synthesize updates manually
        print("noop (--no-poll)")
        return 0

    state = load_poller_state()
    last = int(state.get("last_update_id") or 0)
    try:
        body = tg("getUpdates", {
            "offset": last + 1,
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": ["message"],
        }, timeout=LONG_POLL_TIMEOUT + 10)
    except SystemExit:
        raise
    except Exception as e:
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return 0

    updates = body.get("result", [])
    if not updates:
        return 0

    _, chat_id = (None, None)
    try:
        from shared import load_creds
        _, chat_id = load_creds()
        chat_id = int(chat_id)
    except Exception:
        chat_id = None

    max_uid = last
    for upd in updates:
        uid = upd.get("update_id")
        if uid is not None and uid > max_uid:
            max_uid = uid
        msg = upd.get("message")
        if not msg:
            continue
        # only process messages from the configured chat
        if chat_id is not None:
            mchat = (msg.get("chat") or {}).get("id")
            if mchat != chat_id:
                continue
        try:
            route_message(msg)
        except Exception as e:
            print(f"route_message error on update {uid}: {e}", file=sys.stderr)
            # don't crash the whole batch; advance update_id anyway

    state["last_update_id"] = max_uid
    state["last_poll_ts"] = now_iso()
    save_poller_state(state)
    print(f"processed {len(updates)} updates, last_update_id={max_uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
