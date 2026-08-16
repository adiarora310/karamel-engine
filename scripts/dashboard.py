#!/usr/bin/env python3
"""A page that answers "is this thing alive", on localhost.

The install ends, the terminal goes quiet, and the next scheduled run is hours
away. Everything needed to answer that question already exists on disk, in six
different files, none of which anyone should have to know about. This puts them
on one page.

Deliberately small. Standard library only, no framework, no build step, one
route, bound to 127.0.0.1 so nothing is exposed to the network. It reads state
and never writes any: a dashboard that can change things is a dashboard that can
break things while you are looking at it.

  python3 dashboard.py                 serve on 127.0.0.1:8765
  python3 dashboard.py --port 9000
  python3 dashboard.py --once          print the page to stdout and exit
  python3 dashboard.py --selftest
"""
from __future__ import annotations

import html
import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from shared import CONFIG_DIR, is_agent_line, owner_id

DEFAULT_PORT = 8765

# A token, kept beside the other credentials. Required before this will listen
# on anything except loopback, and the check is a refusal rather than a warning:
# this page carries somebody's unpublished writing, their name, their address
# and their errors, and a warning printed at startup is read by nobody at 3am
# when a tunnel gets pointed at it.
TOKEN_FILE = CONFIG_DIR / "dashboard.json"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def load_token():
    """The shared secret, or None. Absent is fine on loopback and fatal off it."""
    if not TOKEN_FILE.exists():
        return None
    mode = TOKEN_FILE.stat().st_mode & 0o077
    if mode:
        raise SystemExit(f"{TOKEN_FILE} is readable by other users "
                         f"({oct(mode)}). Run: chmod 600 {TOKEN_FILE}")
    try:
        tok = (json.loads(TOKEN_FILE.read_text()).get("token") or "").strip()
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"{TOKEN_FILE} is unreadable: {e}")
    if tok and len(tok) < 24:
        raise SystemExit(
            f"the dashboard token is only {len(tok)} characters. Anything "
            f"reachable from a network needs a real one: "
            f"python3 -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    return tok or None


# --------------------------------------------------------------- gathering

def _owner():
    """Delegates to shared.owner_id: one resolution order for every caller.

    This was a local copy ending in a literal personal name, which shipped."""
    return owner_id()


def rows(path, limit=None):
    out = []
    try:
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def ago(ts):
    """Human gap from an ISO timestamp, or None. Times are what people actually
    check for: 'twelve hours ago' answers the question, a timestamp makes them
    do the arithmetic themselves."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = (datetime.now(t.tzinfo) - t).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} hours ago"
    return f"{secs / 86400:.0f} days ago"


def agents():
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.splitlines():
        if not is_agent_line(line):
            continue
        cols = line.split()
        if len(cols) >= 3:
            out.append({"label": cols[-1], "pid": cols[0], "exit": cols[1]})
    return out


def next_run(tz_now, hours=(9, 15)):
    """When the next draft is due, in the tenant's own timezone."""
    for h in hours:
        cand = tz_now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > tz_now:
            return cand
    return (tz_now + timedelta(days=1)).replace(
        hour=hours[0], minute=0, second=0, microsecond=0)


def recent_errors(limit=6):
    """Newest lines from the .err files, which is where every component says
    what went wrong and nobody ever looks."""
    found = []
    for p in sorted(CONFIG_DIR.glob("*.err")):
        try:
            if not p.stat().st_size:
                continue
            tail = [l for l in p.read_text(errors="replace").splitlines() if l.strip()]
            if tail:
                found.append({"file": p.name, "line": tail[-1][:200],
                              "when": ago(datetime.fromtimestamp(
                                  p.stat().st_mtime).isoformat())})
        except OSError:
            continue
    return found[:limit]


def diff_words(a, b):
    """(removed, added) word runs between a draft and what was actually posted.

    The single most useful signal for an operator: what a person changes is
    exactly what their voice card is getting wrong, and reading two paragraphs
    side by side to spot it does not scale past the first week."""
    import difflib

    aw, bw = (a or "").split(), (b or "").split()
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw).get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            removed.append(" ".join(aw[i1:i2]))
        if tag in ("replace", "insert") and j2 > j1:
            added.append(" ".join(bw[j1:j2]))
    return removed, added


def gather(full=False):
    import tenants

    owner = _owner()
    t = tenants.load_tenant(owner)
    now = t.now() if t else datetime.now()

    drafts = rows(t.original_drafts_path) if t else []
    engage = rows(t.engagement_path) if t else []
    gen = rows(t.generated_path) if t else []

    pending = [d for d in drafts if not d.get("confirmed_ts")]
    answered = [d for d in drafts if d.get("confirmed_ts")]
    counts = {}
    for e in engage:
        counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1

    passes = sum(1 for g in gen if g.get("verdict") == "PASS")

    spend = None
    try:
        import llm
        calls, usd = llm.spend_today(owner)
        cfg = llm.load_config()
        spend = {"calls": calls, "usd": usd, "provider": cfg.get("provider")}
    except Exception:
        pass

    loaded = agents()

    # The operator view. Everything, because the question it answers is "why is
    # this not working for them", and that lives in the parts a summary drops:
    # the gate scores on drafts that never shipped, and the words somebody
    # changed before posting.
    activity = None
    if full:
        edits = []
        for e in engage:
            if e.get("status") == "posted_edited" and e.get("posted_text"):
                rem, add = diff_words(e.get("draft_text"), e.get("posted_text"))
                edits.append({"topic": e.get("topic"), "ts": e.get("ts_iso"),
                              "removed": rem[:8], "added": add[:8]})
        failures = []
        for g in gen:
            if g.get("verdict") == "PASS":
                continue
            rounds = []
            for j in (g.get("journey") or []):
                rounds.append({
                    "n": j.get("round"), "scores": j.get("scores", {}),
                    "why": j.get("why", ""), "fix": j.get("fix", ""),
                    "tells": j.get("tells", []),
                })
            failures.append({"topic": g.get("topic"), "at": g.get("generated_at"),
                             "rounds": rounds})
        activity = {
            "drafts": list(reversed(drafts)),
            "edits": list(reversed(edits)),
            "failures": list(reversed(failures))[:15],
            "errors": recent_errors(limit=40),
        }

    return {
        "full": full,
        "activity": activity,
        "owner": owner,
        "name": (t.name if t else owner),
        "now": now,
        "timezone": (t.timezone if t else ""),
        "channel": ((t.channel or {}).get("type") if t else "none"),
        "address": ((t.channel or {}).get("address") if t else None),
        "card": (t.voice_card_path.name if t else None),
        "card_ok": bool(t and t.voice_card_path.exists()),
        "running": bool(loaded),
        "agents": loaded,
        "next_run": next_run(now),
        "drafts_total": len(drafts),
        "pending": pending[-5:],
        "answered": len(answered),
        "counts": counts,
        "gen_total": len(gen),
        "gen_passed": passes,
        "last_draft": (drafts[-1] if drafts else None),
        "errors": recent_errors(),
        "spend": spend,
        "config_dir": str(CONFIG_DIR),
    }


# ----------------------------------------------------------------- rendering

CSS = """
:root{--bg:#faf9f7;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e2e0dc;--ok:#1a7f4b;
--warn:#a8620a;--bad:#b3261e;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#eeece7;--dim:#9a978f;
--line:#2c2b28;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--card:#1c1b19}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .2rem;font-weight:600}
.sub{color:var(--dim);margin:0 0 1.75rem}
.status{display:flex;align-items:center;gap:.6rem;font-size:1.1rem;
font-weight:600;margin-bottom:.35rem}
.dot{width:.6rem;height:.6rem;border-radius:50%;flex:0 0 auto}
.on{background:var(--ok)}.off{background:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1.1rem 1.25rem;margin-bottom:1rem}
.card h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;
color:var(--dim);margin:0 0 .7rem;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem}
.n{font-size:1.6rem;font-weight:600;line-height:1.1}
.l{color:var(--dim);font-size:.82rem}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.4rem 0;
border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.k{color:var(--dim)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.err{color:var(--bad)}
.q{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:.8rem;font-size:.9rem;overflow-x:auto}
.foot{color:var(--dim);font-size:.82rem;margin-top:2rem;text-align:center}
code{background:var(--bg);border:1px solid var(--line);border-radius:4px;
padding:.1rem .35rem;font-size:.85em}
.ev{padding:.7rem 0;border-bottom:1px solid var(--line)}
.ev:last-child{border-bottom:0}
.cut{color:var(--bad)}
.add{color:var(--ok)}
.op{background:var(--warn);color:#000;border-radius:5px;padding:.15rem .5rem;
font-size:.72rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def render(d):
    on = d["running"]
    parts = [
        "<!-- Karamel status -->",
        f"<style>{CSS}</style>",
        '<div class="wrap">',
        f'<h1>Karamel, for {esc(d["name"])}'
        + ('  <span class="op">operator view</span>' if d.get("full") else "")
        + "</h1>",
        f'<p class="sub">{esc(d["now"].strftime("%A %d %B, %H:%M"))} '
        f'{esc(d["timezone"])}</p>',

        '<div class="card">',
        f'<div class="status"><span class="dot {"on" if on else "off"}"></span>'
        f'{"Running" if on else "Not running"}</div>',
    ]
    if on:
        parts.append(
            f'<div class="l">Next draft {esc(d["next_run"].strftime("%H:%M"))}, '
            f'{esc(len(d["agents"]))} component(s) loaded.</div>')
    else:
        parts.append('<div class="l">Nothing is scheduled. Start it with '
                     '<code>./karamel start</code>.</div>')
    parts.append("</div>")

    parts += [
        '<div class="card"><h2>So far</h2><div class="grid">',
        f'<div><div class="n">{d["drafts_total"]}</div>'
        f'<div class="l">drafts sent to you</div></div>',
        f'<div><div class="n">{d["answered"]}</div>'
        f'<div class="l">you answered</div></div>',
        f'<div><div class="n">{d["counts"].get("posted_clean",0) + d["counts"].get("posted_edited",0)}</div>'
        f'<div class="l">you posted</div></div>',
        f'<div><div class="n">{d["gen_passed"]}/{d["gen_total"]}</div>'
        f'<div class="l">cleared the critic</div></div>',
        "</div></div>",
    ]

    last = d["last_draft"]
    if last:
        parts += [
            '<div class="card"><h2>Most recent draft</h2>',
            f'<div class="l">{esc(ago(last.get("drafted_at")))} · '
            f'{esc(last.get("topic"))} · '
            f'{esc(last.get("status","pending"))}</div>',
            f'<div class="q">{esc((last.get("draft_text") or "")[:700])}</div>',
            "</div>",
        ]

    if d["pending"]:
        parts.append('<div class="card"><h2>Waiting on you</h2>')
        for p in d["pending"]:
            parts.append(
                f'<div class="row"><span>{esc(p.get("topic"))}</span>'
                f'<span class="k">{esc(ago(p.get("drafted_at")))}</span></div>')
        parts.append('<div class="l" style="margin-top:.6rem">Reply to those '
                     'emails with <code>posted</code>, <code>skip</code>, or '
                     '<code>edited</code> and your text.</div></div>')

    parts += [
        '<div class="card"><h2>Setup</h2>',
        f'<div class="row"><span class="k">Delivery</span><span>'
        f'{esc(d["address"] or d["channel"])}</span></div>',
        f'<div class="row"><span class="k">Voice card</span><span>'
        f'{esc(d["card"]) if d["card_ok"] else "<missing>"}</span></div>',
    ]
    if d["spend"]:
        s = d["spend"]
        cost = ("subscription, no per-draft cost" if s["provider"] == "cli"
                else f"${s['usd']:.2f} today")
        parts.append(f'<div class="row"><span class="k">Model</span><span>'
                     f'{esc(s["calls"])} call(s) today, {esc(cost)}</span></div>')
    parts.append("</div>")

    if d["errors"]:
        parts.append('<div class="card"><h2>Recent errors</h2>')
        for e in d["errors"]:
            parts.append(
                f'<div class="row"><span class="mono err">{esc(e["line"])}</span>'
                f'<span class="k">{esc(e["when"])}</span></div>')
        parts.append('<div class="l" style="margin-top:.6rem">Run '
                     '<code>./karamel doctor</code> for what to do about these.'
                     '</div></div>')

    act = d.get("activity")
    if act:
        parts.append('<div class="card"><h2>Gate failures, most recent first</h2>')
        if not act["failures"]:
            parts.append('<div class="l">Nothing has failed the gate.</div>')
        for f in act["failures"]:
            parts.append(f'<div class="ev"><b>{esc(f["topic"])}</b> '
                         f'<span class="k">{esc(ago(f["at"]))}</span>')
            for r in f["rounds"]:
                sc = " ".join(f"{k} {v}" for k, v in (r["scores"] or {}).items())
                parts.append(f'<div class="mono l">round {esc(r["n"])} · {esc(sc)}</div>')
                for q, t in (r["tells"] or [])[:4]:
                    parts.append(f'<div class="mono err">tell: "{esc(q)}" ({esc(t)})</div>')
                if r["why"]:
                    parts.append(f'<div class="l">{esc(r["why"])}</div>')
            parts.append("</div>")
        parts.append('<div class="l" style="margin-top:.6rem">These never '
                     'reached them. The scores say which axis blocked it, and a '
                     'tell is the exact phrase that cost the points.</div></div>')

        parts.append('<div class="card"><h2>What they changed before posting</h2>')
        if not act["edits"]:
            parts.append('<div class="l">Nothing edited yet. Every post so far '
                         'went out as written, or was skipped.</div>')
        for e in act["edits"]:
            parts.append(f'<div class="ev"><b>{esc(e["topic"])}</b> '
                         f'<span class="k">{esc(ago(e["ts"]))}</span>')
            for r in e["removed"]:
                parts.append(f'<div class="mono cut">- {esc(r[:160])}</div>')
            for a in e["added"]:
                parts.append(f'<div class="mono add">+ {esc(a[:160])}</div>')
            parts.append("</div>")
        parts.append('<div class="l" style="margin-top:.6rem">What somebody '
                     'changes is what their voice card is getting wrong. This is '
                     'the same signal the reflector reads.</div></div>')

        parts.append('<div class="card"><h2>Every draft</h2>')
        for r in act["drafts"]:
            st = r.get("status", "pending")
            parts.append(
                f'<div class="ev"><b>{esc(r.get("topic"))}</b> '
                f'<span class="k">{esc(ago(r.get("drafted_at")))} · {esc(st)}'
                f'{" · blanks: " + str(len(r.get("needs_verify") or [])) if r.get("needs_verify") else ""}'
                f'</span>'
                f'<div class="q">{esc((r.get("draft_text") or "")[:900])}</div>')
            if r.get("edited_text"):
                parts.append(f'<div class="l">they posted:</div>'
                             f'<div class="q">{esc(r["edited_text"][:900])}</div>')
            parts.append("</div>")
        parts.append("</div>")

        if act["errors"]:
            parts.append('<div class="card"><h2>Every error on the box</h2>')
            for e in act["errors"]:
                parts.append(f'<div class="row"><span class="mono err">'
                             f'{esc(e["file"])}: {esc(e["line"])}</span>'
                             f'<span class="k">{esc(e["when"])}</span></div>')
            parts.append("</div>")

    parts += [
        '<p class="foot">Karamel never posts anything. Every post is published '
        'by you, by hand.<br>This page is read-only and only reachable from '
        'this Mac.</p>',
        "</div>",
    ]
    return "\n".join(parts)


# -------------------------------------------------------------------- serving

class Handler(BaseHTTPRequestHandler):
    token = None

    def _authorised(self):
        """Constant-time comparison against ?k= or the karamel cookie.

        No token configured means loopback only, which main() has already
        enforced, so there is nothing to check."""
        if not self.token:
            return True
        import hmac
        from http.cookies import SimpleCookie
        from urllib.parse import urlparse, parse_qs

        given = parse_qs(urlparse(self.path).query).get("k", [""])[0]
        if not given:
            raw = self.headers.get("Cookie", "")
            try:
                given = SimpleCookie(raw).get("karamel", None)
                given = given.value if given else ""
            except Exception:
                given = ""
        return hmac.compare_digest(str(given), str(self.token))

    def do_GET(self):
        if not self._authorised():
            body = b"<h1>404</h1>"
            self.send_response(404)          # not 401: no hint that this exists
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        from urllib.parse import urlparse, parse_qs
        full = parse_qs(urlparse(self.path).query).get("full", ["0"])[0] == "1"
        try:
            body = render(gather(full=full)).encode()
        except Exception as e:                       # never a blank page
            body = (f"<pre>dashboard failed to gather state:\n\n"
                    f"{html.escape(repr(e))}</pre>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Refresh", "30")
        # So the token is not in every subsequent URL, and never in a referrer.
        if self.token and "k=" in self.path:
            self.send_header(
                "Set-Cookie",
                f"karamel={self.token}; Path=/; Max-Age=31536000; "
                f"HttpOnly; SameSite=Strict")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass                                          # no request log noise


def _refusal_text():
    """The message shown when a public bind has no token. Extracted so the
    selftest can assert the refusal exists without opening a socket."""
    return ("refusing to listen on <addr> with no token. This page shows "
            "drafts you have not published, your name and your email address.")


# Invented, and it has to stay invented. This file ships to everyone, so a real
# tenant's name in a demo fixture is that person's identity in every copy of the
# software. The packager refused to publish over exactly this.
DEMO = {
    "name": "Sam Rivera", "owner": "sam-rivera",
    "timezone": "America/Los_Angeles", "channel": "email",
    "address": "sam@example.com", "card": "sam-rivera.md",
    "card_ok": True, "running": True,
    "agents": [{"label": f"com.karamel.{n}"} for n in
               ("heartbeat", "inbox", "reflector", "doctor", "updater",
                "dashboard")],
    "drafts_total": 18, "answered": 15,
    "counts": {"posted_clean": 6, "posted_edited": 5, "skipped": 4},
    "gen_total": 24, "gen_passed": 18,
    "pending": [
        {"topic": "delegation and verification replacing typing speed",
         "drafted_at": None},
    ],
    "last_draft": {
        "topic": "the gap between what an AI dev tool promises and what it does",
        "drafted_at": None, "status": "pending",
        "draft_text": "A developer points an agent at a real repository, "
        "watches it rename a helper and break two callers, and closes the tab "
        "by lunchtime. That judgment was correct that morning.\n\nThe verdict "
        "sat still while everything under it moved.",
    },
    "errors": [{"file": "inbox.err", "line":
                "imaplib.IMAP4.error: AUTHENTICATIONFAILED", "when": "3 hours ago"}],
    "spend": {"calls": 11, "usd": 0.0, "provider": "cli"},
    "config_dir": "~/.config/karamel",
}


DEMO_ACTIVITY = {
    "drafts": [
        {"topic": "delegation and verification replacing typing speed",
         "drafted_at": None, "status": "pending",
         "draft_text": "The fastest developer I know types slowly.",
         "needs_verify": ["the survey figure"]},
        {"topic": "why vague instructions fail with agents",
         "drafted_at": None, "status": "posted_edited",
         "draft_text": "Vague instructions failed with human colleagues too.",
         "edited_text": "Vague instructions always failed. Agents just made it "
                        "obvious faster."},
    ],
    "edits": [{"topic": "why vague instructions fail with agents", "ts": None,
               "removed": ["failed with human colleagues too."],
               "added": ["always failed. Agents just made it obvious faster."]}],
    "failures": [{
        "topic": "the promise-to-reality gap", "at": None,
        "rounds": [{"n": 0, "scores": {"VOICE": 8, "TAKE": 9, "SPECIFIC": 7,
                                       "CLEAN": 6},
                    "why": "lands a real verdict, but two tells",
                    "fix": "none",
                    "tells": [("Here is the thing", "throat-clearing"),
                              ("significantly", "adverb crutch")]}],
    }],
    "errors": [{"file": "inbox.err",
                "line": "imaplib.IMAP4.error: AUTHENTICATIONFAILED",
                "when": "3 hours ago"}],
}


def demo(full=False):
    """Render the page with plausible data, for reviewing how it reads.

    An empty install shows zeroes everywhere, which is the least informative
    version of a page whose whole job is showing what has happened."""
    d = dict(DEMO)
    d["now"] = datetime(2026, 8, 13, 11, 20)
    d["next_run"] = next_run(d["now"])
    d["full"] = full
    d["activity"] = DEMO_ACTIVITY if full else None
    return render(d)


def selftest():
    d = {
        "name": "A Person", "owner": "p", "now": datetime(2026, 8, 13, 9, 5),
        "timezone": "America/New_York", "channel": "email",
        "address": "a@b.com", "card": "c.md", "card_ok": True,
        "running": True, "agents": [{"label": "com.karamel.heartbeat"}],
        "next_run": datetime(2026, 8, 13, 15, 0), "drafts_total": 3,
        "pending": [{"topic": "a topic", "drafted_at": None}], "answered": 2,
        "counts": {"posted_clean": 1, "posted_edited": 1}, "gen_total": 4,
        "gen_passed": 3, "last_draft": {"topic": "t", "draft_text": "x",
                                        "drafted_at": None, "status": "pending"},
        "errors": [], "spend": {"calls": 6, "usd": 0.0, "provider": "cli"},
        "config_dir": "/tmp",
    }
    page = render(d)
    assert "A Person" in page and "Running" in page
    assert "2" in page and "posted" in page
    # The subscription path must not claim a dollar cost.
    assert "$0.00" not in page and "subscription" in page

    off = dict(d, running=False, agents=[])
    assert "Not running" in render(off)
    assert "./karamel start" in render(off)

    # Everything from disk is escaped. A draft is model output and a topic can
    # come from a seed file, so neither is trusted into HTML.
    nasty = dict(d, name="<script>alert(1)</script>")
    assert "<script>alert" not in render(nasty)
    assert "&lt;script&gt;" in render(nasty)

    assert ago(None) is None
    assert ago("not a date") is None

    # 09:05 is past the morning run, so the next one is the afternoon.
    assert next_run(datetime(2026, 8, 13, 9, 5)).hour == 15
    assert next_run(datetime(2026, 8, 13, 16, 0)).day == 14
    assert next_run(datetime(2026, 8, 13, 7, 0)).hour == 9

    # The lock, and the refusal that matters more than the lock. Binding off
    # loopback without a token is the failure that puts one person's
    # unpublished writing on the internet, and it happens at the moment
    # somebody points a tunnel at a running service without reading its output.
    assert LOOPBACK == ("127.0.0.1", "::1", "localhost")
    assert "refusing to listen" in _refusal_text()

    class _H:
        token = "a" * 32
        path = "/?k=" + "a" * 32
        headers = {}
        _authorised = Handler._authorised
    assert _H._authorised(_H())
    _H.path = "/?k=wrong"
    assert not _H._authorised(_H())
    _H.path = "/"
    assert not _H._authorised(_H())
    # No token means loopback only, already enforced, so nothing to check.
    _H.token = None
    assert _H._authorised(_H())
    # Compared in constant time, so a wrong token leaks nothing by timing.
    assert "compare_digest" in inspect.getsource(Handler._authorised)

    # The operator view is opt-in and additive: the ordinary page must not
    # start showing somebody's full draft history because a flag defaulted on.
    plain = render(dict(d, full=False, activity=None))
    assert "operator view" not in plain
    assert "Every draft" not in plain

    op = render(dict(d, full=True, activity={
        "drafts": [{"topic": "t", "draft_text": "body", "status": "pending",
                    "drafted_at": None}],
        "edits": [{"topic": "t", "ts": None, "removed": ["was"], "added": ["is"]}],
        "failures": [{"topic": "t", "at": None, "rounds": [
            {"n": 0, "scores": {"CLEAN": 6}, "why": "w", "fix": "",
             "tells": [("a phrase", "adverb crutch")]}]}],
        "errors": [{"file": "x.err", "line": "boom", "when": "now"}],
    }))
    assert "operator view" in op
    assert "Every draft" in op and "body" in op
    assert "a phrase" in op and "adverb crutch" in op
    assert "boom" in op

    # The diff is the point of the view: what somebody changes is what their
    # card is getting wrong.
    rem, add = diff_words("the cat sat down", "the dog sat down")
    assert rem == ["cat"] and add == ["dog"], (rem, add)
    assert diff_words("same", "same") == ([], [])
    assert diff_words("", "added words")[1] == ["added words"]
    assert diff_words(None, None) == ([], [])

    print("dashboard selftest: all assertions passed")
    return True


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    if "--demo" in sys.argv:
        print(demo(full="--full" in sys.argv))
        return 0
    if "--once" in sys.argv:
        print(render(gather(full="--full" in sys.argv)))
        return 0

    port = DEFAULT_PORT
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    bind = "127.0.0.1"
    if "--bind" in sys.argv:
        i = sys.argv.index("--bind")
        if i + 1 < len(sys.argv):
            bind = sys.argv[i + 1]

    token = load_token()

    # Fail closed. Binding anywhere but loopback without a token would put one
    # person's unpublished drafts on a network, and this is exactly the moment
    # somebody is pointing a tunnel at it and not reading startup output.
    if bind not in LOOPBACK and not token:
        raise SystemExit(
            f"refusing to listen on {bind} with no token.\n"
            f"This page shows drafts you have not published, your name and your "
            f"email address.\n\nMake one:\n"
            f"  python3 -c \"import secrets,json,pathlib,os;"
            f"p=pathlib.Path('{TOKEN_FILE}');"
            f"p.write_text(json.dumps({{'token':secrets.token_urlsafe(32)}}));"
            f"os.chmod(p,0o600)\"\n"
        )

    Handler.token = token
    try:
        srv = HTTPServer((bind, port), Handler)
    except OSError as e:
        if e.errno == 48:
            # Almost always the launchd agent already serving this page, which
            # is the good case. Say that, rather than a traceback about sockets
            # immediately after ./karamel start printed the URL.
            print(f"Something is already serving port {port}.\n"
                  f"If that is Karamel's own agent, the page is up: "
                  f"http://127.0.0.1:{port}\n"
                  f"Check with:  launchctl list | grep dashboard")
            return 0
        raise
    where = f"http://{'127.0.0.1' if bind in LOOPBACK else bind}:{port}"
    if token:
        print(f"Karamel status: {where}/?k={token}")
        print("That link is the password. The cookie it sets lasts a year.")
    else:
        print(f"Karamel status: {where}")
        print("No token set, so this listens on loopback only.")
    print("Ctrl-C to stop. Read-only.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
