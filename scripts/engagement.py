#!/usr/bin/env python3
"""Engagement tracking for posted X drafts: likes, replies, reposts, followers.

Khoa, 2026-10-19: "start tracking how posted drafts perform (likes, replies,
reposts, new followers) per account, so we can see what's working". He starts
from zero followers.

THE RULES IT KEEPS
- Nothing posts. This only reads what the person already posted themselves.
- No password. Two sources, neither needs one:
    1. Public tweet metrics. X's embed endpoint (cdn.syndication.twimg.com)
       returns likes and replies for any public tweet ID with no login.
    2. The account's public profile, read through the Chrome session already
       signed in on this machine (the CDP port the old reader used), for
       tenants with engagement_source "browser". That adds reposts, views and
       the follower count, and finds posted drafts nobody pasted a link for.
       It reads a public page any logged-in visitor could see; the person whose
       account it is never hands over a password.
- Ongoing behaviour only. A tweet is tracked when it is a Karamel draft that
  was posted after it was delivered: matched by the link in their "Posted"
  reply, or by text against drafts delivered in the last 14 days. Nothing is
  learned from the account's history before Karamel.
- X only. LinkedIn metrics are the LinkedIn lane's job and come from
  LinkedIn's official API; this file never touches LinkedIn.

FILES, per tenant data dir
  engagement_metrics.jsonl  one snapshot per tracked tweet per run
  followers.jsonl           one follower count per run (browser source only)

CLI
  --tenant ID | --all       which accounts
  --report                  print what is working, per account
  --selftest                pure checks, no network
"""
from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import tenants
from shared import append_jsonl, now_iso, read_jsonl

STATUS_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{6,25})")
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=en&token={token}"
MATCH_RATIO = 0.80          # posted text vs delivered draft, after normalising
MATCH_WINDOW_DAYS = 14
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Karamel engagement"}


def metrics_path(t):
    return t.data_dir / "engagement_metrics.jsonl"


def followers_path(t):
    return t.data_dir / "followers.jsonl"


# ---------------------------------------------------------------- public source

def syndication_token(tweet_id):
    """The token X's own embed widget computes: (id / 1e15) * pi in base 36,
    with zeros and the point removed. Without it the endpoint 404s."""
    x = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole, frac = int(x), x - int(x)
    s = ""
    while True:
        s = digits[whole % 36] + s
        whole //= 36
        if whole == 0:
            break
    f = ""
    for _ in range(12):
        frac *= 36
        d = int(frac)
        f += digits[d]
        frac -= d
    return re.sub(r"(0+|\.)", "", s + "." + f)


def public_metrics(tweet_id, timeout=15):
    """{likes, replies, text, handle, created_at} for a public tweet, or None."""
    url = SYNDICATION.format(id=tweet_id, token=syndication_token(tweet_id))
    try:
        req = urllib.request.Request(url, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception:
        return None
    if not d or "id_str" not in d:
        return None
    return {
        "likes": d.get("favorite_count"),
        "replies": d.get("conversation_count"),
        "text": d.get("text") or "",
        "handle": (d.get("user") or {}).get("screen_name"),
        "created_at": d.get("created_at"),
    }


# --------------------------------------------------------------- browser source

PROFILE_JS = r"""
() => {
  const out = {followers: null, posts: []};
  const f = [...document.querySelectorAll('a[href$="/verified_followers"], a[href$="/followers"]')]
            .map(a => a.innerText).find(t => /follower/i.test(t));
  if (f) out.followers = f;
  document.querySelectorAll('article[data-testid="tweet"]').forEach(a => {
    const link = a.querySelector('a[href*="/status/"]');
    const t = a.querySelector('div[data-testid="tweetText"]');
    const time = a.querySelector('time');
    const g = a.querySelector('div[role="group"]');
    if (!link) return;
    const m = link.getAttribute('href').match(/\/([^\/]+)\/status\/(\d+)/);
    if (!m) return;
    out.posts.push({handle: m[1], id: m[2], text: t ? t.innerText : '',
                    at: time ? time.getAttribute('datetime') : null,
                    metrics: g ? g.getAttribute('aria-label') : ''});
  });
  return out;
}
"""


def parse_count(s):
    """'1,234' / '1.2K' / '3M' -> int."""
    if s is None:
        return None
    m = re.search(r"([\d.,]+)\s*([KkMm]?)", str(s))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1))


def parse_group_label(label):
    """X's action-bar aria-label, e.g. '3 replies, 5 reposts, 40 likes, 1234 views'."""
    out = {}
    # X leaves a zero out of the label entirely ("1 reply, 1 like, 45 views"
    # has no reposts), so on a post that has a label, absent means 0. Only a
    # missing label means unknown.
    absent = 0 if (label or "").strip() else None
    for key, word in (("replies", "repl"), ("reposts", "repost"),
                      ("likes", "like"), ("views", "view")):
        m = re.search(r"([\d,\.]+[KkMm]?)\s+" + word, label or "", re.IGNORECASE)
        out[key] = parse_count(m.group(1)) if m else absent
    return out


def read_profile(handle, cdp_port=9222):
    """Followers and recent posts from the public profile, through the signed-in
    Chrome on this machine. None when that browser or playwright is absent."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded",
                          timeout=30000)
                page.wait_for_selector('a[href$="/followers"], a[href$="/verified_followers"]',
                                       timeout=15000)
                page.wait_for_timeout(2500)
                data = page.evaluate(PROFILE_JS)
            finally:
                page.close()
    except Exception:
        return None
    data["followers"] = parse_count(data.get("followers"))
    for post in data.get("posts") or []:
        post.update(parse_group_label(post.pop("metrics", "")))
    return data


# --------------------------------------------------------------------- matching

def normalise(text):
    t = re.sub(r"https?://\S+", "", text or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def delivered_drafts(t, days=MATCH_WINDOW_DAYS):
    """Karamel drafts delivered to this person recently, newest last."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in read_jsonl(t.original_drafts_path):
        ts = r.get("drafted_at") or ""
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= since and r.get("draft_text"):
            out.append(r)
    return out


def match_draft(post_text, drafts):
    """The delivered draft this post came from, or None. Texts are compared
    after links and spacing are removed; an edited post still matches when
    most of it survived, because the edit is what we want to learn from."""
    best, score = None, 0.0
    p = normalise(post_text)
    for d in drafts:
        for candidate in (d.get("edited_text"), d.get("draft_text")):
            if not candidate:
                continue
            r = SequenceMatcher(None, p, normalise(candidate)).ratio()
            if r > score:
                best, score = d, r
    return best if score >= MATCH_RATIO else None


def tracked_ids(t):
    """{tweet_id: draft_id} for every posted draft whose link we know."""
    ids = {}
    for r in read_jsonl(t.original_drafts_path):
        for field in ("reply_url", "posted_url"):
            m = STATUS_RE.search(r.get(field) or "")
            if m:
                ids[m.group(2)] = r.get("draft_id")
        if r.get("tweet_id"):
            ids[str(r["tweet_id"])] = r.get("draft_id")
    return ids


# ------------------------------------------------------------------------ run

def run_tenant(t, browser=None):
    """One collection pass. Returns a one-line summary."""
    known = tracked_ids(t)
    use_browser = t.engagement_source == "browser" if browser is None else browser
    profile = None
    if use_browser and t.handle:
        profile = read_profile(t.handle, getattr(t, "cdp_port", 9222))
        if profile and profile.get("followers") is not None:
            append_jsonl(followers_path(t), {"ts": now_iso(), "tenant": t.id,
                                             "followers": profile["followers"]})
        drafts = delivered_drafts(t)
        for post in (profile or {}).get("posts") or []:
            if post.get("handle", "").lower() != t.handle.lower() or post["id"] in known:
                continue
            d = match_draft(post.get("text"), drafts)
            if d:
                known[post["id"]] = d.get("draft_id")
    snaps = 0
    by_id = {p["id"]: p for p in (profile or {}).get("posts") or []}
    for tweet_id, draft_id in known.items():
        pub = public_metrics(tweet_id) or {}
        br = by_id.get(tweet_id, {})
        if not pub and not br:
            continue
        append_jsonl(metrics_path(t), {
            "ts": now_iso(), "tenant": t.id, "tweet_id": tweet_id,
            "draft_id": draft_id,
            "likes": br.get("likes") if br.get("likes") is not None else pub.get("likes"),
            "replies": br.get("replies") if br.get("replies") is not None else pub.get("replies"),
            "reposts": br.get("reposts"), "views": br.get("views"),
            "source": "browser+public" if br else "public",
        })
        snaps += 1
    f = profile.get("followers") if profile else None
    return (f"[{t.id}] tracked {snaps} posted draft(s)"
            + (f", {f} followers" if f is not None else "")
            + ("" if profile or not use_browser else " (browser unavailable; links only)"))


def latest_by_tweet(rows):
    out = {}
    for r in rows:
        out[r["tweet_id"]] = r
    return out


def report(t):
    """What is working for this account, from the latest snapshot of each post."""
    drafts = {r.get("draft_id"): r for r in read_jsonl(t.original_drafts_path)}
    latest = latest_by_tweet(read_jsonl(metrics_path(t)))
    lines = [f"{t.name} (@{t.handle or '?'})"]
    fol = read_jsonl(followers_path(t))
    if fol:
        first, last = fol[0]["followers"], fol[-1]["followers"]
        lines.append(f"  followers: {last} (+{last - first} since tracking began)")
    else:
        lines.append("  followers: not tracked yet (needs the browser source)")
    if not latest:
        lines.append("  no posted drafts tracked yet. Reply 'Posted' with the post's "
                     "link to start.")
        return "\n".join(lines)
    ranked = sorted(latest.values(), key=lambda r: -((r.get("likes") or 0)
                    + 2 * (r.get("replies") or 0) + 2 * (r.get("reposts") or 0)))
    lines.append(f"  {len(ranked)} posted draft(s), best first:")
    for r in ranked[:10]:
        d = drafts.get(r.get("draft_id")) or {}
        text = (d.get("edited_text") or d.get("draft_text") or "")[:70]
        lines.append(f"   {r.get('likes') or 0:>4} likes {r.get('replies') or 0:>3} replies "
                     f"{r.get('reposts') if r.get('reposts') is not None else '-':>3} reposts"
                     f"  [{d.get('lane') or d.get('register') or '?'}] {text}")
    return "\n".join(lines)


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv and \
        sys.argv.index(flag) + 1 < len(sys.argv) else default


def selftest():
    assert syndication_token("20") == syndication_token(20)
    assert re.fullmatch(r"[0-9a-z]+", syndication_token("1585341984679469056"))
    assert parse_count("1,234") == 1234 and parse_count("1.2K") == 1200
    assert parse_count("3M") == 3000000 and parse_count(None) is None
    g = parse_group_label("3 replies, 5 reposts, 40 likes, 1,234 views")
    assert g == {"replies": 3, "reposts": 5, "likes": 40, "views": 1234}, g
    assert parse_group_label("")["likes"] is None
    assert parse_group_label("1 reply, 1 like, 45 views")["reposts"] == 0
    m = STATUS_RE.search("Posted https://x.com/KhoaMaNYC/status/1234567890123?s=20")
    assert m and m.group(1) == "KhoaMaNYC" and m.group(2) == "1234567890123"
    assert STATUS_RE.search("https://twitter.com/a/status/987654321")
    drafts = [{"draft_id": 1, "draft_text": "LOIs do not count as customers. Signed contracts do."},
              {"draft_id": 2, "draft_text": "Quantum founders are back in my inbox."}]
    assert match_draft("LOIs do not count as customers. Signed contracts do. https://t.co/x",
                       drafts)["draft_id"] == 1
    assert match_draft("LOIs don't count as customers. Signed contracts do!",
                       drafts)["draft_id"] == 1, "a light edit still matches"
    assert match_draft("Something he wrote himself, unrelated.", drafts) is None
    print("engagement selftest: all assertions passed")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    if "--all" in sys.argv:
        targets = tenants.list_tenants()
    else:
        t = tenants.load_tenant(_arg("--tenant", tenants.LEGACY_TENANT))
        targets = [t] if t else []
    if not targets:
        print("no such tenant", file=sys.stderr)
        return 2
    for t in targets:
        print(report(t) if "--report" in sys.argv else run_tenant(t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
