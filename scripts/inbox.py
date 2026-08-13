#!/usr/bin/env python3
"""Karamel inbox: the email half of the approval loop.

poller_daemon.py reads Telegram, so a Telegram tenant confirms a draft with the
check, the pencil or the cross and reflector.py learns from it. Email was
send-only, which made an email tenant a customer of the half of the product that
does not compound: they received drafts and their approvals went nowhere.

This closes it. One IMAP pass per run, same shape as poller.main().

FOUR THINGS THAT MATTER

1. Quoted text is stripped before any token detection. The draft email we send
   ENDS with the literal line "reply with the check to post, pencil plus your
   edit, or cross to skip.  ✅ ✏️ ❌". Almost every mail client quotes the
   original beneath the reply, so parsing a whole message body would read our
   own instructions back as the user's answer and mark every reply as posted,
   including the ones that said no. See strip_quoted().

2. Routing is by From address, matched against a tenant's configured channel.
   An email from an address no tenant owns is ignored, never guessed at.

3. Correlation is In-Reply-To first, then the "#<draft_id>" the subject carries.
   Reply-All, forwards and clients that rewrite Message-IDs all keep the
   subject, so the fallback is the one that usually works.

4. The token vocabulary is imported from poller, not reimplemented. Two parsers
   for the same three symbols would drift, and the drift would be silent.

CLI:
  --catch-up  scan existing mail instead of starting from now (first run only)
  --once        one IMAP pass, then exit (what launchd runs)
  --dry-run     read and report, write nothing, mark nothing seen
  --selftest    pure-logic tests, no network
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header, make_header

import poller
import tenants
from channels import load_email_creds
from shared import CONFIG_DIR, append_jsonl, now_iso, read_jsonl, write_jsonl_atomic

INBOX_STATE = CONFIG_DIR / "inbox_state.json"

DRAFT_ID_RE = re.compile(r"#(\d{10,})")

# Lines that begin the quoted original. Everything from the first hit is theirs,
# not ours, and must not be parsed for tokens.
QUOTE_MARKERS = (
    re.compile(r"^\s*>"),
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*From:\s.+@", re.IGNORECASE),
    re.compile(r"^\s*\[KARAMEL DRAFT", re.IGNORECASE),
    re.compile(r"^\s*⚠️ NOT READY TO POST", re.IGNORECASE),
)
SIGNATURE = re.compile(r"^--\s*$")


def strip_quoted(body):
    """The reply text only, with the quoted original and signature removed.

    This is the correctness crux of the whole module. Our draft email ends with
    an instruction line containing all three tokens, so any parse that sees the
    quoted original will find a check mark in every reply, including the ones
    that skipped."""
    out = []
    for line in (body or "").splitlines():
        if any(m.search(line) for m in QUOTE_MARKERS) or SIGNATURE.match(line):
            break
        out.append(line)
    return "\n".join(out).strip()


def header(msg, name):
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def sender_address(msg):
    """Bare address from a From header, lowercased."""
    _, addr = email.utils.parseaddr(header(msg, "From"))
    return (addr or "").strip().lower()


def draft_id_from_subject(subject):
    m = DRAFT_ID_RE.search(subject or "")
    return int(m.group(1)) if m else None


def body_text(msg):
    """The text/plain part, preferring it over any HTML alternative."""
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and "attachment" not in str(
            part.get("Content-Disposition") or ""
        ):
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def tenant_for_sender(addr):
    """The tenant who owns this address, or None. Never guesses."""
    if not addr:
        return None
    for t in tenants.list_tenants(include_disabled=True):
        ch = t.channel or {}
        if ch.get("type") == "email" and (ch.get("address") or "").lower() == addr:
            return t
    return None


def locate_draft(tenant, in_reply_to, draft_id):
    """(path, rows, index) for the draft this reply answers, or (None, None, None).

    In-Reply-To first because it is exact; the subject's draft id second because
    it survives clients that rewrite Message-IDs, and forwards."""
    for path in (tenant.original_drafts_path, tenant.drafts_path):
        rows = read_jsonl(path)
        if in_reply_to:
            for i, r in enumerate(rows):
                if r.get("telegram_msg_id") == in_reply_to:
                    return path, rows, i
        if draft_id is not None:
            for i, r in enumerate(rows):
                if r.get("draft_id") == draft_id:
                    return path, rows, i
    return None, None, None


def apply_reply(tenant, msg, dry=False):
    """Process one message. Returns a one-line outcome for the log."""
    addr = sender_address(msg)
    if tenant is None:
        return f"ignored: {addr or '(no from)'} matches no tenant"

    subject = header(msg, "Subject")
    reply_text = strip_quoted(body_text(msg))
    status, edited_text, reply_url = poller.parse_reply_tokens(reply_text)
    if status is None:
        return (f"[{tenant.id}] unrecognised reply to {subject[:50]!r}: "
                f"{reply_text[:60]!r}")

    path, rows, i = locate_draft(
        tenant, header(msg, "In-Reply-To").strip() or None,
        draft_id_from_subject(subject),
    )
    if i is None:
        return f"[{tenant.id}] no matching draft for {subject[:60]!r}"

    draft = rows[i]
    if draft.get("confirmed_ts"):
        return (f"[{tenant.id}] draft #{draft.get('draft_id')} already "
                f"{draft.get('status')}, ignoring duplicate reply")

    # A draft with unfilled blanks cannot be confirmed as posted-clean: the
    # placeholder would have gone out verbatim. Treat it as an edit only if they
    # actually supplied text.
    if draft.get("needs_verify") and status == "posted_clean":
        return (f"[{tenant.id}] draft #{draft.get('draft_id')} still has "
                f"{len(draft['needs_verify'])} blank(s); reply with the pencil "
                f"and the finished text, not the check")

    if dry:
        return f"[{tenant.id}] would mark #{draft.get('draft_id')} {status}"

    draft["status"] = status
    draft["confirmed_ts"] = now_iso()
    draft["confirmed_by"] = "email_inbox"
    if edited_text:
        draft["edited_text"] = edited_text
    if reply_url:
        draft["reply_url"] = reply_url
    write_jsonl_atomic(path, rows)

    if status in ("posted_clean", "posted_edited"):
        append_jsonl(tenant.engagement_path, {
            "ts_iso": now_iso(),
            "draft_id": draft.get("draft_id"),
            "tenant": tenant.id,
            "kind": draft.get("kind", "original"),
            "status": status,
            "topic": draft.get("topic"),
            "draft_text": draft.get("draft_text"),
            "posted_text": edited_text or draft.get("draft_text"),
            "reply_url": reply_url,
        })
    return f"[{tenant.id}] #{draft.get('draft_id')} -> {status}"


def load_state():
    """Saved position, or {} when there is none.

    Returned {"last_uid": 0} for a missing file, which made "never run before"
    indistinguishable from "run before, and the mailbox was empty". The
    first-run guard tests for the absence of that key, so it could never fire:
    it was dead code from the moment it was written, and only the subject filter
    stopped a second full-mailbox scan."""
    if not INBOX_STATE.exists():
        return {}
    try:
        got = json.loads(INBOX_STATE.read_text())
        return got if isinstance(got, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    INBOX_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INBOX_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, INBOX_STATE)
    os.chmod(INBOX_STATE, 0o600)


# Only ever fetch mail that is a reply to one of ours. Our subject carries this
# marker and a reply keeps it, so this is both the correctness filter and the
# privacy boundary: without it the search matched every message in the mailbox
# and this opened bank alerts, medical mail and years of personal
# correspondence to read the word "posted".
SUBJECT_MARKER = "[Karamel]"


def search_uids(im, last):
    """UIDs of our own threads newer than the watermark. Narrow by subject at
    the server, not after fetching: fetching first means downloading a whole
    mailbox to discard it."""
    typ, data = im.uid("search", None, "UID", f"{last + 1}:*",
                       "SUBJECT", f'"{SUBJECT_MARKER}"')
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")
    return [u for u in (data[0] or b"").split() if int(u) > last]


def current_high_uid(im):
    """The newest UID in the mailbox right now, or 0 if it is empty."""
    typ, data = im.uid("search", None, "ALL")
    if typ != "OK":
        return 0
    uids = [int(u) for u in (data[0] or b"").split()]
    return max(uids) if uids else 0


def run_once(dry=False, catch_up=False):
    cfg = load_email_creds()
    host = cfg.get("imap_host") or cfg["smtp_host"].replace("smtp.", "imap.", 1)
    port = int(cfg.get("imap_port", 993))
    state = load_state()
    first_run = "last_uid" not in state
    last = int(state.get("last_uid", 0))

    with imaplib.IMAP4_SSL(host, port, timeout=30) as im:
        im.login(cfg["username"], cfg["password"])
        im.select("INBOX")

        # A watermark of zero means "every message this account has ever
        # received". On a real mailbox that is tens of thousands of messages,
        # fetched one at a time, and every one of them printed. Observed live:
        # it walked years of personal mail before anyone could stop it.
        #
        # So a first run marks the current position and reads nothing. Replies
        # to drafts that do not exist yet cannot be waiting.
        if first_run and not catch_up:
            high = current_high_uid(im)
            if not dry:
                state["last_uid"] = high
                save_state(state)
            print(f"first run: starting from now (uid {high}). Nothing before "
                  f"this is read, because a reply to a draft we never sent "
                  f"cannot be waiting.\nPass --catch-up to scan existing mail "
                  f"instead.")
            return 0

        try:
            uids = search_uids(im, last)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        if not uids:
            print("no new mail")
            return 0

        highest, handled, ignored = last, [], 0
        for uid in uids:
            typ, raw = im.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            t = tenant_for_sender(sender_address(msg))
            line = apply_reply(t, msg, dry=dry)
            # One line per unmatched message turns a stuck mailbox into
            # thousands of lines of noise with the real result buried in it.
            if line.startswith("ignored:"):
                ignored += 1
            else:
                handled.append(line)
            highest = max(highest, int(uid))
        for line in handled:
            print(line)
        if ignored:
            print(f"({ignored} message(s) matched the subject but no tenant)")
        if not dry:
            state["last_uid"] = highest
            save_state(state)
        print(f"processed {len(uids)} message(s), last_uid={highest}"
              f"{' (dry run, not saved)' if dry else ''}")
    return 0


# ------------------------------- tests ---------------------------------------

def selftest():

    # The first run must not read the whole mailbox. Observed live on the host:
    # a watermark of zero meant "every message this account ever received", so
    # it walked years of bank alerts and personal mail, one printed line each,
    # looking for the word "posted".
    # load_state must distinguish "never run" from "run, mailbox was empty".
    # It returned {"last_uid": 0} for a missing file, so the first-run guard
    # tested for a key that was always present and never fired once.
    import tempfile as _tf
    from pathlib import Path as _Path
    _real_state = globals()["INBOX_STATE"]
    try:
        with _tf.TemporaryDirectory() as d:
            globals()["INBOX_STATE"] = _Path(d) / "inbox_state.json"
            assert load_state() == {}, "missing state must be empty, not zeroed"
            assert "last_uid" not in load_state()
            globals()["INBOX_STATE"].write_text('{"last_uid": 0}')
            assert load_state() == {"last_uid": 0}
            assert "last_uid" in load_state()      # run before, empty mailbox
            globals()["INBOX_STATE"].write_text("not json at all")
            assert load_state() == {}
            globals()["INBOX_STATE"].write_text('["a", "list"]')
            assert load_state() == {}
    finally:
        globals()["INBOX_STATE"] = _real_state

    assert SUBJECT_MARKER == "[Karamel]"

    # The search is narrowed at the server. Fetching first and filtering after
    # means downloading a whole mailbox in order to discard it, and it means
    # opening mail that has nothing to do with this system.
    class _FakeIM:
        def __init__(self): self.args = None
        def uid(self, *a):
            self.args = a
            return "OK", [b"7 8 9"]
    im = _FakeIM()
    assert search_uids(im, 6) == [b"7", b"8", b"9"]
    assert "SUBJECT" in im.args and '"[Karamel]"' in im.args, im.args
    assert "7:*" in im.args, im.args
    # Anything at or below the watermark is dropped even if the server returns it.
    im2 = _FakeIM()
    assert search_uids(im2, 8) == [b"9"]

    class _EmptyIM:
        def uid(self, *a): return "OK", [b""]
    assert current_high_uid(_EmptyIM()) == 0
    class _FullIM:
        def uid(self, *a): return "OK", [b"3 11 7"]
    assert current_high_uid(_FullIM()) == 11
    # THE ONE THAT MATTERS. A real reply quoting our draft email, whose
    # instruction line contains all three tokens. Parsing the whole body reads
    # our own instructions as their answer.
    quoted = (
        "❌ not this one, too close to the last post\n"
        "\n"
        "On Mon, 10 Aug 2026 at 09:30, Karamel <k@example.com> wrote:\n"
        "> [KARAMEL DRAFT, analytical, Mon Aug 10 09:30 BST]\n"
        "> Topic: something\n"
        ">\n"
        "> the draft text\n"
        ">\n"
        "> (gate {}) reply with the check to post, pencil plus your edit, "
        "or cross to skip.  ✅ ✏️ ❌\n"
    )
    clean = strip_quoted(quoted)
    assert "✅" not in clean, "our own instruction line leaked into the reply"
    assert clean.startswith("❌"), clean
    status, _, _ = poller.parse_reply_tokens(clean)
    assert status == "skipped", f"a skip was read as {status}"

    # Same message parsed naively is the bug this guards against.
    naive, _, _ = poller.parse_reply_tokens(quoted)
    assert naive != "skipped", "if this passes, strip_quoted is not doing anything"

    # Outlook-style and underscore dividers.
    assert strip_quoted("yes\n-----Original Message-----\n✅") == "yes"
    assert strip_quoted("yes\n______\n✅") == "yes"
    assert strip_quoted("yes\n\n-- \nSent from my phone ✅") == "yes"
    assert strip_quoted("From: a@b.com\n✅") == ""

    # An edit survives stripping, with its text.
    edit = "✏️ the tool is the point, not the model\n\n> old draft"
    assert strip_quoted(edit) == "✏️ the tool is the point, not the model"
    st, txt, _ = poller.parse_reply_tokens(strip_quoted(edit))
    assert st == "posted_edited" and "the tool is the point" in txt, (st, txt)

    # A real client sends multipart/alternative; we must take text/plain and
    # ignore the HTML twin, which carries the same quoted instructions.
    from email.message import EmailMessage as _EM
    multi = _EM()
    multi["From"] = "jane@example.com"
    multi["Subject"] = "Re: [Karamel] #1786327098445 x"
    multi.set_content("✏️ sharper version here\n\n> ✅ ✏️ ❌")
    multi.add_alternative("<p>&#9997; sharper version here</p><blockquote>"
                          "&#9989; &#9997; &#10060;</blockquote>", subtype="html")
    got = strip_quoted(body_text(multi))
    assert got == "✏️ sharper version here", repr(got)
    assert "✅" not in got

    # Draft id from the subject, including Re: and Fwd: prefixes.
    assert draft_id_from_subject("[Karamel] #1786327098445 · draft · x") == 1786327098445
    assert draft_id_from_subject("Re: [Karamel] #1786327098445 · x") == 1786327098445
    assert draft_id_from_subject("no id here") is None
    assert draft_id_from_subject("") is None
    assert draft_id_from_subject("#123") is None          # too short to be one

    # Routing never guesses.
    class T:
        def __init__(self, tid, addr):
            self.id, self.channel = tid, {"type": "email", "address": addr}
    real = tenants.list_tenants
    tenants.list_tenants = lambda include_disabled=False: [
        T("jane", "Jane@Example.com"), T("sam", "sam@example.com")]
    try:
        assert tenant_for_sender("jane@example.com").id == "jane"   # case-insensitive
        assert tenant_for_sender("sam@example.com").id == "sam"
        assert tenant_for_sender("stranger@example.com") is None
        assert tenant_for_sender("") is None
        assert tenant_for_sender(None) is None
    finally:
        tenants.list_tenants = real

    # A draft with unfilled blanks refuses a bare check mark.
    class FakeT:
        id = "t"
        original_drafts_path = drafts_path = engagement_path = None
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "x@y.com"
    msg["Subject"] = "[Karamel] #1786327098445 x"
    msg.set_content("✅\n")
    _real_locate = globals()["locate_draft"]
    globals()["locate_draft"] = lambda t, r, d: (
        "p", [{"draft_id": 1786327098445, "needs_verify": ["the number"]}], 0)
    try:
        out = apply_reply(FakeT(), msg, dry=True)
        assert "blank" in out and "pencil" in out, out
    finally:
        globals()["locate_draft"] = _real_locate

    print("inbox selftest: all assertions passed")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    if "--once" in sys.argv or "--dry-run" in sys.argv:
        try:
            return run_once(dry="--dry-run" in sys.argv,
                            catch_up="--catch-up" in sys.argv)
        except Exception as e:
            print(f"inbox run failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    print(__doc__.strip().split("CLI:")[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
