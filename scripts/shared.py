"""Shared helpers for karamel notifier and poller."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerr
from urllib import parse, request
from zoneinfo import ZoneInfo

HOME = Path.home()

# Where the checkout actually is, derived from this file rather than assumed.
# It was hardcoded to ~/Documents/Claude/karamel, which is true on the two
# machines this has ever run on and false on anyone else's: a clone anywhere
# else resolved every path to a directory that does not exist, and the failure
# surfaced as empty reads rather than as an error. KARAMEL_HOME overrides it for
# an install that keeps code and data apart.
PROJECT = Path(
    os.environ.get("KARAMEL_HOME") or Path(__file__).resolve().parent.parent
)
DATA = PROJECT / "data"
DRAFTS = DATA / "drafts.jsonl"
ENGAGEMENT = DATA / "engagement.jsonl"
EVAL_QUEUE = DATA / "evaluator_queue.jsonl"

def _config_dir():
    """Where credentials and counters live.

    New installs use ~/.config/karamel. An existing box has live credentials,
    pause state, counters and tenant configs under the old ~/.config/cowork, and
    moving those during a rename would strand a running system for the sake of a
    directory name.

    Prefer whichever is NON-EMPTY, not whichever merely exists. An empty new
    directory outranking a populated old one is not hypothetical: install.py
    mkdir'd it before checking its own --print flag, so a dry run orphaned a
    live config.

    The literal old name below must survive future renames. A blanket
    search-and-replace over this file already rewrote it once, leaving both
    branches pointing at the new name and silently deleting the fallback.
    """
    new = HOME / ".config" / "karamel"
    old = HOME / ".config" / ("co" + "work")   # written so a regex will not eat it
    if new.is_dir() and any(new.iterdir()):
        return new
    if old.is_dir() and any(old.iterdir()):
        return old
    return new


CONFIG_DIR = _config_dir()

# launchd label prefixes, newest first. The rename to com.karamel.* was applied
# to this repo's generated plists and never to the agents already loaded on the
# host, which as of 2026-08-12 runs nine com.cowork.* agents and zero
# com.karamel.* ones. Anything that looked for one prefix therefore lied on the
# only machine that matters: doctor reported "nothing loaded" against nine
# running agents, ./karamel stop unloaded none of them, and the updater could
# not restart the poller. Same reasoning as _config_dir() above: the old name
# keeps working until someone deliberately migrates.
AGENT_PREFIXES = ("com.karamel.", "com.cowork.")
AGENT_DIR = HOME / "Library" / "LaunchAgents"


def is_agent_line(line: str) -> bool:
    """True if a `launchctl list` row belongs to this system, either name."""
    return any(p in line for p in AGENT_PREFIXES)


def agent_plists(directory: Path | None = None) -> list[Path]:
    """Every plist of ours on this machine, under either prefix, deduplicated
    by the component name so a half-migrated box is not acted on twice."""
    d = directory or AGENT_DIR
    found: dict[str, Path] = {}
    for prefix in AGENT_PREFIXES:
        for p in sorted(d.glob(f"{prefix}*.plist")):
            short = p.name[len(prefix):-len(".plist")]
            found.setdefault(short, p)
    return list(found.values())


def agent_labels(short: str) -> list[str]:
    """Candidate launchd labels for one component, preferred name first."""
    return [f"{p}{short}" for p in AGENT_PREFIXES]
TELEGRAM_CFG = CONFIG_DIR / "telegram.json"
POLLER_STATE = CONFIG_DIR / "poller_state.json"
PAUSE_STATE = CONFIG_DIR / "pause_state.json"

ET = ZoneInfo("America/New_York")

EM_DASH_RE = re.compile(r"[—–]")
ANTI_PHRASES = [
    "here's the thing", "the truth is", "let's be honest",
    "not gonna lie", "hear me out", "buckle up", "let me cook",
    "game-changing", "10x", "insane", "absolutely", "literally",
    "founders should", "the key takeaway is", "three things to remember",
]
URL_RE = re.compile(r"https?://(?:x|twitter)\.com/[^/\s]+/status/(\d+)", re.IGNORECASE)
PAUSE_DUR_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def now_et_str(fmt: str = "%H:%M ET") -> str:
    return now_utc().astimezone(ET).strftime(fmt)


def load_creds() -> tuple[str, str]:
    with open(TELEGRAM_CFG) as f:
        cfg = json.load(f)
    return cfg["bot_token"], cfg["chat_id"]


def tg(method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    token, _ = load_creds()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    backoff = 1.0
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                if body.get("error_code") == 429:
                    retry_after = body.get("parameters", {}).get("retry_after", 5)
                    print(f"tg 429, sleeping {retry_after}s", file=sys.stderr)
                    time.sleep(retry_after)
                    continue
                if body.get("error_code") == 401:
                    print("tg 401: token invalid. aborting.", file=sys.stderr)
                    raise SystemExit(2)
                raise RuntimeError(f"tg {method} not ok: {body}")
            return body
        except urlerr.HTTPError as e:
            if e.code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            if 500 <= e.code < 600:
                if attempt < 2:
                    time.sleep(10)
                    continue
            raise
        except (urlerr.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2)
                continue
            raise
    raise RuntimeError(f"tg {method} exhausted retries")


def send_message(text: str, reply_to_message_id: int | None = None,
                 disable_web_page_preview: bool = True) -> int | None:
    _, chat_id = load_creds()
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_to_message_id is not None:
        params["reply_to_message_id"] = reply_to_message_id
        params["allow_sending_without_reply"] = True
    body = tg("sendMessage", params)
    return body.get("result", {}).get("message_id")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def has_em_dash(s: str) -> bool:
    return bool(EM_DASH_RE.search(s or ""))


def anti_pattern_hit(s: str) -> str | None:
    if has_em_dash(s):
        return "em_dash"
    low = (s or "").lower()
    for ph in ANTI_PHRASES:
        if ph in low:
            return f"phrase:{ph}"
    return None


def load_pause_state() -> dict[str, Any]:
    if not PAUSE_STATE.exists():
        return {"paused_until": None, "paused_indefinitely": False, "halt": False}
    with open(PAUSE_STATE) as f:
        return json.load(f)


def save_pause_state(state: dict[str, Any]) -> None:
    PAUSE_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAUSE_STATE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, PAUSE_STATE)
    os.chmod(PAUSE_STATE, 0o600)


def is_paused() -> tuple[bool, str]:
    """Returns (paused, reason). Notifier short-circuits when paused."""
    st = load_pause_state()
    if st.get("paused_indefinitely"):
        return True, "paused indefinitely (vacation)"
    pu = st.get("paused_until")
    if pu:
        try:
            until = datetime.fromisoformat(pu.replace("Z", "+00:00"))
        except ValueError:
            return False, ""
        if until > now_utc():
            return True, f"paused until {until.astimezone(ET).strftime('%H:%M ET')}"
    return False, ""


def is_halted() -> bool:
    return bool(load_pause_state().get("halt"))


def parse_pause_duration(token: str) -> timedelta | None:
    """Parse '2h', '30m', '24h', '1d'. Returns None if unparseable."""
    m = PAUSE_DUR_RE.match(token.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    return None


def owner_id() -> str:
    """Who owns this installation. One answer, for every caller.

    Four files each had their own copy of this and all four ended in the
    author's first name as a literal fallback, which shipped in the public
    package: on somebody else's Mac, anything run outside a launchd agent
    resolved to a tenant named after a stranger, found nothing, and reported it
    in a way that gave no hint why.

    Order matters. The env var wins because install.py writes the box owner
    into every agent's environment and that is the most specific statement of
    intent. .karamel is next, written once by the bootstrap, which is what makes
    a command typed by hand agree with the same command run by an agent. The
    placeholder is last and is not a person's name."""
    env = os.environ.get("KARAMEL_OWNER")
    if env:
        return env
    try:
        marker = json.loads((PROJECT / ".karamel").read_text()).get("owner")
        if marker:
            return marker
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "owner"


def post_url(draft_text: str, in_reply_to: str | None = None) -> str:
    """A link that opens X's composer with this text already in it.

    One helper for both kinds of draft. Replies pass in_reply_to and thread
    correctly; originals omit it and open a blank composer, which is what an
    original post needs and what it never had: the email carried the text and
    nothing else, so publishing meant selecting a paragraph out of a mail client
    and pasting it, which is where stray quote marks and lost line breaks come
    from.

    twitter.com/intent/tweet, NOT the newer x.com/intent/post spelling, and the
    difference is not cosmetic. Tested on a signed-in iPhone opening these from
    Gmail: twitter.com/intent/tweet is registered as a universal link and opens
    the X app; x.com/intent/post is not, so it loads in Gmail's in-app browser,
    which has its own cookie jar and no session, and shows a login wall to
    somebody whose app is signed in one tap away.

    This file briefly used the x.com spelling on the reasoning that a link
    bouncing through a retired domain looks broken. It turned out to be the only
    thing making the link work, so the old domain stays until Apple's
    association file says otherwise. Retest with A/B links before changing it;
    do not change it because one form looks more current."""
    url = "https://twitter.com/intent/tweet?text=" + parse.quote(
        draft_text, safe="")
    if in_reply_to:
        url += "&in_reply_to=" + parse.quote(str(in_reply_to), safe="")
    return url


def compose_url(tweet_id: str, draft_text: str) -> str:
    """Reply composer. Kept as its own name because notifier and drafter both
    call it and both store its output under a compose_url key.

    A twitter://post?message= scheme link was tried here and removed. It cannot
    carry in_reply_to, so it would post a reply as a standalone tweet, and it
    was not reported as tappable in Gmail at all: mail clients generally
    linkify http(s) and leave unknown schemes as plain text."""
    return post_url(draft_text, in_reply_to=tweet_id)


def short_id(tweet_id: str | None) -> str:
    if not tweet_id:
        return "?"
    return tweet_id[-6:]
