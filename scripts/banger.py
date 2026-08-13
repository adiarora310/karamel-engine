#!/usr/bin/env python3
"""Karamel banger generator (v1): daily, scrape current events across the 4 lanes
(pop culture, economy, investing, entrepreneurship + sport), generate 2-3
Bill-Burr-register banger tweets grounded ONLY in real scraped headlines, push
to Telegram. Curate-only: Adi publishes every word.

Runs under launchd once daily (morning). No-hallucination by design: claude
only sees headlines we actually fetched and is told to riff on those, not invent.

Flags:
  --force   skip pause/halt gate (for manual testing)
  --print   print to stdout instead of sending to Telegram
"""
import re
import sys
from datetime import datetime, timezone, timedelta

import feedparser

import llm
import tenants
from karamel_common import (
    DATA, ROOT, append_jsonl, is_paused, now_iso, now_et, read_jsonl,
    send_telegram_if_configured,
)

VOICE_CARD = ROOT / "03_voice_card.md"
BANGERS = DATA / "bangers.jsonl"
EM_DASH = re.compile("[–—]")

# RSS feeds by lane. All free, no API key.
FEEDS = {
    "tech/AI": [
        "https://hnrss.org/frontpage",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ],
    "economy/markets": [
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",  # economy
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",  # markets
    ],
    "pop culture": [
        "https://variety.com/feed/",
        "https://www.hollywoodreporter.com/feed/",
    ],
    "sport / Lakers / NBA": [
        "https://www.espn.com/espn/rss/news",
        "https://www.espn.com/espn/rss/nba/news",
    ],
    "design": [
        "https://www.dezeen.com/feed/",
        "https://www.core77.com/rss",
    ],
    "food": [
        "https://www.eater.com/rss/index.xml",
    ],
}
MAX_PER_FEED = 6
MAX_TOTAL = 28
MAX_AGE_H = 48


def fetch_headlines():
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
    for lane, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"feed failed {url}: {e}", file=sys.stderr)
                continue
            src = (feed.feed.get("title") or url)[:40]
            for e in feed.entries[:MAX_PER_FEED]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                if e.get("published_parsed"):
                    dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                items.append({"lane": lane, "title": title, "source": src})
    seen, uniq = set(), []
    for it in items:
        k = it["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq[:MAX_TOTAL]


def build_prompt(headlines, voice, n, avoid, author=None):
    hl = "\n".join(f"- [{h['lane']}] {h['title']} ({h['source']})" for h in headlines)
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\n\nALREADY POSTED TODAY (do NOT repeat these angles, headlines, or topics):\n"
            + "\n".join(f"- {a}" for a in avoid)
        )
    plural = "s" if n > 1 else ""
    pick = "the single funniest, most-tweetable angle" if n == 1 else f"the {n} funniest, most-tweetable angles across different lanes"
    who = author or tenants.default_author()
    return f"""You are writing banger tweets in {who}'s voice. His voice card follows; obey every hard line in it, especially the anti-patterns and the hard "do not" lines.

=== VOICE CARD ===
{voice}
=== END VOICE CARD ===

PERSONA FOR THESE TWEETS: channel Bill Burr doing high-IQ standup. Exasperated blue-collar logic, deadpan, punches up at hype / pretension / absurdity, the "am I the only sane person in the room" energy, undersell the insane part. Funny because it is true and said with total confidence, never try-hard.

HARD GUARDRAILS (these override everything, including the persona):
- ZERO partisan politics (US or India). No parties, politicians, elections, candidates, government policy, or culture-war tribes. Mock business / tech / markets / pop-culture / sport / startup absurdity ONLY. If a headline is political, tragic (deaths, war, disaster), or about a vulnerable group, SKIP it.
- Zero em-dashes and en-dashes. Use commas, periods, colons.
- No emoji. No hashtags. No "hot take" / "unpopular opinion". Declarative. 1 to 3 sentences, fits in one screenshot.
- Only riff on the REAL headlines listed below. Do NOT invent events, numbers, or quotes. Each tweet must be grounded in one of the listed headlines.

TODAY'S HEADLINES:
{hl}{avoid_block}

Write exactly {n} banger tweet{plural}. Pick {pick} from today's headlines. Output ONLY the following, nothing else (repeat the block {n} time{plural}):

SOURCE: <the exact headline it riffs on>
TWEET: <the tweet text>"""


def call_claude(prompt, system=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="banger")


def parse_bangers(out):
    res = []
    for block in (b.strip() for b in out.split("---")):
        if not block:
            continue
        s = re.search(r"SOURCE:\s*(.+)", block)
        t = re.search(r"TWEET:\s*(.+)", block, re.S)
        if not t:
            continue
        tweet = re.split(r"\n\s*(?:SOURCE|TWEET):", t.group(1).strip())[0].strip()
        res.append({"source": s.group(1).strip() if s else "", "tweet": tweet})
    return res


def main():
    force = "--force" in sys.argv
    do_print = "--print" in sys.argv
    count = 1  # one fresh banger per run; launchd fires this 3x/day at peak windows
    if "--count" in sys.argv:
        count = int(sys.argv[sys.argv.index("--count") + 1])

    if is_paused() and not force:
        print("paused/halted, exiting")
        return 0

    date = now_et().strftime("%Y-%m-%d")
    # same-day dedup: don't repeat headlines/angles already sent today
    prev = [r for r in read_jsonl(BANGERS) if r.get("date") == date]
    used_titles = {(r.get("source") or "").lower() for r in prev}
    avoid = [f"{(r.get('source') or '')[:70]} -> {(r.get('tweet') or '')[:90]}" for r in prev]

    headlines = [h for h in fetch_headlines() if h["title"].lower() not in used_titles]
    print(f"fetched {len(headlines)} fresh headlines (excluded {len(used_titles)} used today)", file=sys.stderr)
    if len(headlines) < 4:
        print("too few fresh headlines, skipping run")
        return 1

    voice = VOICE_CARD.read_text()
    out = call_claude(build_prompt(headlines, voice, count, avoid))
    bangers = [b for b in parse_bangers(out) if b["tweet"] and not EM_DASH.search(b["tweet"])][:count]
    if not bangers:
        print("no clean bangers produced", file=sys.stderr)
        print(out[:600], file=sys.stderr)
        return 1

    when = now_et().strftime("%-I:%M %p ET")
    for b in bangers:
        msg = f"[BANGER · {when}] post now (Bill Burr register, you publish):\n\n{b['tweet']}\n\n(re: {b['source'][:90]})"
        if do_print:
            print(msg)
        else:
            send_telegram_if_configured(msg)
        append_jsonl(BANGERS, {
            "ts": now_iso(), "date": date, "window": when,
            "tweet": b["tweet"], "source": b["source"], "status": "pending",
        })
    print(f"done: {len(bangers)} banger(s) for the {when} window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
