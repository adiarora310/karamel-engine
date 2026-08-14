#!/usr/bin/env python3
"""Put the status page on a URL both the person and the operator can open.

The page runs on 127.0.0.1, which answers "is this working" for whoever is sat
at that Mac and nobody else. Supporting an install you cannot see means being
able to look at it, and asking somebody to paste terminal output is what a whole
evening of this already proved does not scale.

A Cloudflare quick tunnel needs no account, no DNS and no login: cloudflared
connects outward and prints a URL. Nothing is opened on the machine and no port
is forwarded.

The URL is random and changes on restart, which is why this emails it rather
than expecting anyone to copy it down. The page's own token still applies, so a
guessed URL is not a way in.

  python3 share.py            start the tunnel, email the URL, keep running
  python3 share.py --print    just print the URL and exit
  python3 share.py --selftest
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading

from shared import CONFIG_DIR

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DEFAULT_PORT = 8765


def find_cloudflared():
    import shutil

    found = shutil.which("cloudflared")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        import pathlib
        if pathlib.Path(cand).exists():
            return cand
    raise SystemExit(
        "cloudflared is not installed. Run:  brew install cloudflared"
    )


def extract_url(line):
    """The tunnel URL out of one line of cloudflared output, or None.

    Parsed rather than guessed: the URL is random per run, which is the entire
    reason this emails it instead of expecting somebody to have written it
    down."""
    m = URL_RE.search(line or "")
    return m.group(0) if m else None


def token_suffix():
    """?k=... if the page has a token, so the emailed link works as sent."""
    import json

    try:
        tok = json.loads((CONFIG_DIR / "dashboard.json").read_text()).get("token")
    except Exception:
        return ""
    return f"/?k={tok}" if tok else ""


def announce(url, extra=None):
    """Email the link to the tenant, and to anyone in dashboard.json's
    `share_with`. That list is how an operator gets the same view without
    anybody reading a terminal to them."""
    import json

    import channels
    import tenants

    t = tenants.load_tenant(tenants.LEGACY_TENANT)
    if t is None:
        print(f"no tenant; the URL is {url}")
        return
    body = (
        f"Karamel status page for {t.name}:\n\n  {url}\n\n"
        "This link changes whenever the tunnel restarts, so use the newest "
        "email rather than a bookmark.\n\nIt is read-only. Nothing on that "
        "page can change anything."
    )
    try:
        channels.send(t, body, subject="[Karamel] your status page")
        print(f"emailed the link to {(t.channel or {}).get('address', t.id)}")
    except Exception as e:
        print(f"could not email the link: {e}", file=sys.stderr)

    try:
        also = json.loads((CONFIG_DIR / "dashboard.json").read_text()).get(
            "share_with") or []
    except Exception:
        also = []
    for addr in also:
        try:
            class _T:
                id, name = t.id, t.name
                channel = {"type": "email", "address": addr}
            channels.send(_T(), body, subject=f"[Karamel] {t.name}'s status page")
            print(f"emailed the link to {addr}")
        except Exception as e:
            print(f"could not email {addr}: {e}", file=sys.stderr)


def run(port=DEFAULT_PORT, announce_it=True):
    cmd = [find_cloudflared(), "tunnel", "--url", f"http://127.0.0.1:{port}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    sent = threading.Event()
    try:
        for line in proc.stdout:
            url = extract_url(line)
            if url and not sent.is_set():
                sent.set()
                full = url + token_suffix()
                print(f"\nStatus page: {full}\n")
                if announce_it:
                    announce(full)
                else:
                    return 0
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


def selftest():
    line = ("2026-08-14T12:00:00Z INF |  https://brave-fox-runs-here."
            "trycloudflare.com  |")
    assert extract_url(line) == "https://brave-fox-runs-here.trycloudflare.com"
    assert extract_url("nothing here") is None
    assert extract_url("") is None
    assert extract_url(None) is None
    # Not fooled by a lookalike host: this only ever reports a real quick tunnel.
    assert extract_url("https://evil.trycloudflare.com.attacker.net") == \
        "https://evil.trycloudflare.com"
    print("share selftest: all assertions passed")
    return True


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    return run(port=port, announce_it="--print" not in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
