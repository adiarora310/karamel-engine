#!/usr/bin/env python3
"""Delivery channels: how a draft reaches the person who has to approve it.

heartbeat.py used to call shared.send_message directly and fall back to stdout
when Telegram creds were missing. That was correct for one user whose channel
was known at import time, and wrong the moment a second tenant exists: the
channel is a property of the person, not of the machine.

Every backend returns a message id or None. None means "delivered, no id to
correlate" (stdout) OR "not delivered" (a send that failed) - callers that need
to distinguish should check the return of send() against the channel type. The
approval loop only stores the id so a reply can be matched back to a draft, so a
missing id degrades reply-matching, not delivery.

Adding a backend: write send_<type>(tenant, text) -> id or None, register it in
BACKENDS, and add the type to tenants.VALID_CHANNELS so configs validate.
"""
from __future__ import annotations

import json
import sys

from shared import CONFIG_DIR

EMAIL_CFG = CONFIG_DIR / "email.json"

def send_telegram(tenant, text):
    """The existing path. Creds are per-machine today (one bot, one config), so
    chat_id is what makes it per-tenant. A second Telegram tenant works only
    because the bot can hold many chats; it is not isolation, and a per-tenant
    bot token belongs here when that matters."""
    from shared import send_message

    chat_id = tenant.channel.get("chat_id")
    try:
        return send_message(text, chat_id=chat_id) if chat_id else send_message(text)
    except TypeError:
        # shared.send_message predates multi-chat; it targets the configured
        # chat. Sending anyway would deliver one tenant's draft to another's
        # phone, so refuse rather than misroute.
        raise RuntimeError(
            f"shared.send_message does not accept chat_id, so tenant "
            f"{tenant.id!r} cannot be routed to chat {chat_id!r} safely"
        )


def send_stdout(tenant, text):
    """No channel configured. Used on a machine without creds and by --print."""
    print(f"\n--- [{tenant.id}] would deliver ---\n{text}\n")
    return None


def load_email_creds():
    """SMTP config from ~/.config/karamel/email.json, same shape and handling as
    telegram.json. Raises with the missing key named rather than a KeyError."""
    if not EMAIL_CFG.exists():
        raise RuntimeError(
            f"{EMAIL_CFG} missing. Needs: smtp_host, smtp_port, username, "
            "password, from_address. Use an app-specific password, never the "
            "account password."
        )
    mode = EMAIL_CFG.stat().st_mode & 0o077
    if mode:
        # It holds a password. Refuse rather than warn: a readable credential
        # file on a box that will hold several people's data is not a nit.
        raise RuntimeError(
            f"{EMAIL_CFG} is group/world readable ({oct(mode)}). "
            f"Run: chmod 600 {EMAIL_CFG}"
        )
    cfg = json.loads(EMAIL_CFG.read_text())
    missing = [k for k in ("smtp_host", "smtp_port", "username", "password",
                           "from_address") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"{EMAIL_CFG} missing key(s): {', '.join(missing)}")
    cfg["password"] = normalise_app_password(cfg["smtp_host"], cfg["password"])
    return cfg


def normalise_app_password(host, password):
    """Strip the display spaces out of a Google app password.

    Google shows a 16-character app password as four groups of four, and that
    is what people paste, because it is what is on the screen. Gmail's SMTP then
    answers 535 Username and Password not accepted, which reads as a wrong
    password rather than a formatting artefact. Confirmed on the host: sixteen
    lowercase letters, correct, rejected purely for the spaces.

    Narrow on purpose. Only Google hosts, and only when removing whitespace
    leaves exactly the 16 letters an app password is made of. A password is not
    something to silently rewrite on a guess, and plenty of real ones contain
    spaces."""
    pw = str(password or "")
    if not str(host or "").lower().endswith(("gmail.com", "googlemail.com")):
        return pw.strip()
    bare = "".join(pw.split())
    if len(bare) == 16 and bare.isalpha() and bare.islower():
        return bare
    return pw.strip()


def auth_failure_hint(cfg, err):
    """What a 535 from the mail server usually means, in words.

    A raw SMTPAuthenticationError says "Username and Password not accepted" and
    a URL, which is true and useless. For Gmail there are only a few real
    causes, and the shape of the stored password distinguishes most of them
    without anyone having to read the secret aloud."""
    pw = str(cfg.get("password") or "")
    bare = pw.replace(" ", "")
    lines = [f"the mail server rejected {cfg.get('username')!r}: {err}"]
    if cfg.get("smtp_host", "").endswith("gmail.com"):
        if len(bare) != 16:
            lines.append(
                f"That password is {len(bare)} characters with spaces removed. A "
                f"Gmail app password is exactly 16. This looks like the normal "
                f"account password, which Gmail always refuses for SMTP."
            )
        elif " " in pw:
            lines.append(
                "It is 16 characters but stored with spaces. Google displays "
                "them in four groups for readability; try it with the spaces "
                "removed."
            )
        else:
            lines.append(
                "The shape is right for an app password, so the likeliest cause "
                "is that it belongs to a DIFFERENT Google account. "
                "myaccount.google.com/apppasswords creates one under whichever "
                "account is default, which is not always the one you are "
                "configuring. Check the avatar top-right says "
                f"{cfg.get('username')}, delete any old entry, and make a fresh "
                "one. Confirmed cause on this system, 2026-08-12."
            )
        lines.append("App passwords need 2-Step Verification switched on first.")
    return "\n  ".join(lines)


def send_email(tenant, text, subject=None):
    """Deliver one draft by email. Returns the Message-ID.

    The Message-ID and the draft id in the subject are how inbox.py matches a
    reply back to its draft: a mail client puts both in the reply (In-Reply-To,
    and "Re: ..." keeps the subject). It tries the Message-ID first because it
    is exact, then the subject id because that survives clients which rewrite
    Message-IDs, and forwards.

    So BOTH must keep travelling. Dropping the id from the subject to make it
    tidier would break every reply from a client that rewrites Message-IDs, and
    it would break silently: the mail arrives, parses, matches nothing, and the
    approval is lost with no error anywhere.
    """
    import smtplib
    from email.message import EmailMessage
    from email.utils import make_msgid

    cfg = load_email_creds()
    to = (tenant.channel or {}).get("address")
    if not to:
        raise ValueError(f"tenant {tenant.id!r} has an email channel with no address")

    msg = EmailMessage()
    msg["Subject"] = subject or "[Karamel] a draft for you"
    msg["From"] = f"{cfg.get('from_name', 'Karamel')} <{cfg['from_address']}>"
    msg["To"] = to
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    mid = make_msgid(domain=cfg["from_address"].split("@")[-1])
    msg["Message-ID"] = mid
    msg.set_content(text)

    port = int(cfg["smtp_port"])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=30) as smtp:
                smtp.login(cfg["username"], cfg["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg["smtp_host"], port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(cfg["username"], cfg["password"])
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(auth_failure_hint(cfg, e)) from e
    if to.lower() == (cfg.get("from_address") or "").lower():
        mark_unread(cfg, mid)
    return mid


def mark_unread(cfg, message_id, attempts=4, delay=1.5):
    """Clear \\Seen on a message we just sent to ourselves. Best effort.

    Karamel sends from the person's address to the same address, and Gmail
    treats a message whose From matches the account as one the account sent:
    it files it in the inbox already read. No unread badge, no notification,
    nothing bold in the list. Measured on a live account: 43 Karamel messages
    in INBOX, 0 unread, including one delivered four minutes earlier.

    So the product looked broken for a whole evening while working perfectly.
    Every layer reported success, because every layer had succeeded, and the
    person kept saying they had received nothing. They had; it just never
    announced itself.

    Never fatal, and never raised: the draft is already delivered by the time
    this runs, and a flag is not worth losing a send over. Only runs when
    sender and recipient are the same mailbox, which is the only case that has
    the problem."""
    import imaplib
    import time

    host, port = cfg.get("imap_host"), int(cfg.get("imap_port") or 993)
    if not host:
        return False
    for attempt in range(attempts):
        # Delivery is not instant, so a search immediately after the send finds
        # nothing. Sleep first, then look.
        time.sleep(delay)
        try:
            M = imaplib.IMAP4_SSL(host, port, timeout=20)
            try:
                M.login(cfg["username"], cfg["password"])
                M.select("INBOX")
                st, data = M.search(None, "HEADER", "Message-ID", message_id)
                ids = data[0].split() if st == "OK" and data and data[0] else []
                if not ids:
                    continue
                for i in ids:
                    M.store(i, "-FLAGS", "(\\Seen)")
                return True
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
        except Exception as e:
            if attempt == attempts - 1:
                print(f"could not mark {message_id} unread: {e}", file=sys.stderr)
    return False


BACKENDS = {
    "telegram": send_telegram,
    "email": send_email,
    "none": send_stdout,
}


def send(tenant, text, dry=False, subject=None):
    """Deliver one message to a tenant's channel. Returns a message id or None.

    dry=True always routes to stdout regardless of configured channel, so a dry
    run can never reach a real person.

    Both channels are complete round trips now. poller_daemon reads Telegram;
    inbox.py reads IMAP and calls the same parse_reply_tokens, so a check, a
    pencil or a cross means the same thing whichever way it arrives, lands in
    the same draft row, and appends the same engagement record that reflector.py
    learns from. Email was send-only until inbox.py existed, and a tenant on a
    send-only channel gets drafts forever without their voice card ever
    sharpening, which is the product quietly not working.
    """
    if dry:
        return send_stdout(tenant, text)
    kind = (tenant.channel or {}).get("type", "none")
    backend = BACKENDS.get(kind)
    if backend is None:
        raise ValueError(f"unknown channel type {kind!r} for tenant {tenant.id!r}")
    if kind == "email":
        return backend(tenant, text, subject=subject)
    return backend(tenant, text)


def selftest():

    # Google app passwords, pasted as displayed. Four groups of four is what is
    # on the screen, so it is what people paste, and Gmail answers 535 for it.
    # Observed on the host: sixteen correct lowercase letters, refused for the
    # spaces alone.
    assert normalise_app_password("smtp.gmail.com", "abcd efgh ijkl mnop") \
        == "abcdefghijklmnop"
    assert normalise_app_password("smtp.gmail.com", "abcdefghijklmnop") \
        == "abcdefghijklmnop"
    # Narrow on purpose: a real password is not something to rewrite on a guess.
    assert normalise_app_password("smtp.gmail.com", "my real pass word") \
        == "my real pass word"
    assert normalise_app_password("smtp.fastmail.com", "a b c d") == "a b c d"
    assert normalise_app_password("smtp.gmail.com", "  padded  ") == "padded"
    assert normalise_app_password("smtp.gmail.com", None) == ""
    # 16 characters but with a digit is not an app password shape.
    assert normalise_app_password("smtp.gmail.com", "abcd efgh ijkl mn0p") \
        == "abcd efgh ijkl mn0p"
    class FakeTenant:
        def __init__(self, channel):
            self.id, self.channel = "fake", channel

    # dry never touches a real backend, whatever the config says
    assert send(FakeTenant({"type": "telegram", "chat_id": "1"}), "x", dry=True) is None
    assert send(FakeTenant({"type": "email", "address": "a@b"}), "x", dry=True) is None

    # no channel configured degrades to stdout rather than raising
    assert send(FakeTenant({"type": "none"}), "x") is None
    assert send(FakeTenant({}), "x") is None

    # email fails loudly on a missing config rather than silently dropping a
    # draft. The intent is unchanged from when this backend did not exist: a
    # send that cannot happen must never look like a send that did.
    try:
        send(FakeTenant({"type": "email", "address": "a@b"}), "x")
        raise AssertionError("email must not claim to have delivered")
    except RuntimeError as e:
        assert "email.json" in str(e)

    # an unknown type is a config error, not a silent no-op
    try:
        send(FakeTenant({"type": "carrier-pigeon"}), "x")
        raise AssertionError("unknown channel should raise")
    except ValueError as e:
        assert "carrier-pigeon" in str(e)

    assert set(BACKENDS) == {"telegram", "email", "none"}

    # --- email, without ever opening a socket ---------------------------------
    import json as _json, os as _os, pathlib as _pl, tempfile as _tf
    global EMAIL_CFG
    _real_cfg = EMAIL_CFG

    tmp = _pl.Path(_tf.mkdtemp())
    try:
        # missing config names what it needs, rather than KeyError-ing at send time
        EMAIL_CFG = tmp / "absent.json"
        try:
            load_email_creds(); raise AssertionError("missing config must raise")
        except RuntimeError as e:
            assert "smtp_host" in str(e) and "app-specific" in str(e)

        good = {"smtp_host": "smtp.example.com", "smtp_port": 587,
                "username": "u", "password": "p", "from_address": "a@example.com"}

        # a world-readable password file is refused, not warned about
        loose = tmp / "loose.json"
        loose.write_text(_json.dumps(good)); _os.chmod(loose, 0o644)
        EMAIL_CFG = loose
        try:
            load_email_creds(); raise AssertionError("loose perms must raise")
        except RuntimeError as e:
            assert "readable" in str(e) and "chmod 600" in str(e)

        # each missing key is named
        for drop in ("smtp_host", "password", "from_address"):
            partial = dict(good); partial.pop(drop)
            f = tmp / f"p_{drop}.json"
            f.write_text(_json.dumps(partial)); _os.chmod(f, 0o600)
            EMAIL_CFG = f
            try:
                load_email_creds(); raise AssertionError(f"{drop} must be required")
            except RuntimeError as e:
                assert drop in str(e), (drop, str(e))

        ok = tmp / "ok.json"
        ok.write_text(_json.dumps(good)); _os.chmod(ok, 0o600)
        EMAIL_CFG = ok
        assert load_email_creds()["smtp_host"] == "smtp.example.com"

        # an email tenant with no address fails before any connection attempt
        try:
            send_email(FakeTenant({"type": "email"}), "x")
            raise AssertionError("no address must raise")
        except ValueError as e:
            assert "no address" in str(e)

        # dry never reaches SMTP even with a valid config and address
        assert send(FakeTenant({"type": "email", "address": "a@b.com"}),
                    "x", dry=True) is None
    finally:
        EMAIL_CFG = _real_cfg

    print("channels selftest: all assertions passed")


def _main():
    import sys

    if "--selftest" in sys.argv:
        selftest()
        return 0

    if "--check" in sys.argv:
        try:
            cfg = load_email_creds()
        except RuntimeError as e:
            print(f"email config: NOT USABLE\n  {e}", file=sys.stderr)
            return 1
        print(f"email config: ok  host={cfg['smtp_host']}:{cfg['smtp_port']} "
              f"from={cfg['from_address']}")
        return 0

    if "--send-test" in sys.argv:
        # Deliberately separate from the pipeline. SMTP fails in dull ways (a
        # wrong port, an app password that was never generated, a host that
        # wants SSL not STARTTLS) and finding that out through a heartbeat run
        # means a real draft is the test message.
        i = sys.argv.index("--send-test")
        if i + 1 >= len(sys.argv):
            print("usage: channels.py --send-test you@example.com", file=sys.stderr)
            return 2
        addr = sys.argv[i + 1]

        class _T:
            id = "smtp-test"
            channel = {"type": "email", "address": addr}

        try:
            mid = send_email(_T(), "If you are reading this, Karamel can send "
                                   "email. Nothing else about this message matters.",
                             subject="[Karamel] SMTP test")
        except Exception as e:
            print(f"send FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"sent to {addr}  message-id={mid}")
        return 0

    print(__doc__.strip())
    print("\n  --check                  validate email.json without sending")
    print("  --send-test ADDRESS      send one real test email")
    print("  --selftest               offline tests, never opens a socket")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
