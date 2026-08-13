#!/usr/bin/env python3
"""Karamel content-prompter (analytical / VC-credibility register).

Surfaces 1-2 cross-domain thread/post IDEAS in Adi's analytical register,
grounded in what the target-100 are actually posting (candidates.jsonl) + the
current news cycle. Each idea = format + the cross-domain wedge + a hook line +
a 3-5 beat outline. Adi writes the body and publishes. This is the credibility
layer (vs banger.py = reach layer). Substack essays are NOT in scope (Adi's hand).

Runs Tue/Wed/Thu morning, before the 9-11am ET VC posting window.
Flags: --force, --print, --count N (default 2)
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
IDEAS = DATA / "content_ideas.jsonl"
CANDIDATES = DATA / "candidates.jsonl"
EM_DASH = re.compile("[–—]")

# Analytical register leans investing/econ/business/tech, plus pop culture for the wedge.
FEEDS = {
    "tech/AI": ["https://hnrss.org/frontpage", "https://techcrunch.com/feed/"],
    "economy/markets": [
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    ],
    "pop culture": ["https://variety.com/feed/"],
    "design": ["https://www.dezeen.com/feed/", "https://www.core77.com/rss"],
}
MAX_PER_FEED = 6
MAX_HEADLINES = 22
MAX_AGE_H = 48


def fetch_headlines():
    items, seen = [], set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
    for lane, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception:
                continue
            src = (feed.feed.get("title") or url)[:40]
            for e in feed.entries[:MAX_PER_FEED]:
                title = (e.get("title") or "").strip()
                if not title or title.lower() in seen:
                    continue
                if e.get("published_parsed"):
                    dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                seen.add(title.lower())
                items.append({"lane": lane, "title": title, "source": src})
    return items[:MAX_HEADLINES]


def recent_candidates(n=15):
    """What the target-100 posted recently — the bullseye's live discourse."""
    rows = read_jsonl(CANDIDATES)
    out = []
    for r in rows[-n:]:
        txt = (r.get("text") or "").replace("\n", " ").strip()
        if txt:
            out.append(f"@{r.get('author_handle','?')}: {txt[:200]}")
    return out


def build_prompt(headlines, candidates, voice, n, author=None):
    hl = "\n".join(f"- [{h['lane']}] {h['title']}" for h in headlines)
    cand = "\n".join(f"- {c}" for c in candidates) if candidates else "(none captured recently)"
    who = author or tenants.default_author()
    return f"""You are generating ORIGINAL post ideas in {who}'s ANALYTICAL register. His voice card follows; obey every hard line, especially the anti-patterns and the hard "do not" lines.

=== VOICE CARD ===
{voice}
=== END VOICE CARD ===

This is the CREDIBILITY layer, not jokes. Analytical register: cross-domain pattern recognition, a specific framework, at least one specific number/name/example, 12-18 word sentences. The bar: someone in the target VC list reads it, screenshots it, and replies or DMs. Each idea MUST pull across two lanes (pop culture, economy, investing, entrepreneurship) into a non-obvious frame. That cross-domain wedge is the whole point.

HARD GUARDRAILS:
- Zero partisan politics (US or India). Zero em-dashes/en-dashes. No emoji, no "hot take", no "founders should", no AI-connective-tissue ("here's the thing", "the truth is").
- Ground every idea in a REAL item below (a target-100 post or a headline). Do NOT invent events, numbers, or quotes.
- Do NOT write the full thread. Give the idea, the hook, and the outline. Adi writes the body in his own voice.

WHAT THE TARGET-100 VCs ARE POSTING NOW:
{cand}

TODAY'S HEADLINES:
{hl}

Generate exactly {n} post ideas. Output ONLY this, repeated {n} times:

FORMAT: single post | thread | long-form
WEDGE: <lane A> x <lane B>: <the non-obvious connection in one line, no dashes>
HOOK: <the opening line, in voice, a fact or scene, never a question>
OUTLINE:
- <beat 1>
- <beat 2>
- <beat 3>
CLOSE: <a forward-looking last line>
---"""


def call_claude(prompt, system=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="content-prompter")


def parse_ideas(out):
    ideas = []
    for block in (b.strip() for b in out.split("---")):
        if "HOOK:" not in block:
            continue
        ideas.append(block)
    return ideas


def main():
    force = "--force" in sys.argv
    do_print = "--print" in sys.argv
    count = 2
    if "--count" in sys.argv:
        count = int(sys.argv[sys.argv.index("--count") + 1])

    if is_paused() and not force:
        print("paused/halted, exiting")
        return 0

    headlines = fetch_headlines()
    cands = recent_candidates()
    print(f"fetched {len(headlines)} headlines, {len(cands)} candidate posts", file=sys.stderr)
    if len(headlines) + len(cands) < 5:
        print("too little signal, skipping run")
        return 1

    voice = VOICE_CARD.read_text()
    out = call_claude(build_prompt(headlines, cands, voice, count))
    # sanitize any stray em/en dashes (scaffold + hook must be dash-free) rather than drop
    ideas = [EM_DASH.sub(", ", b) for b in parse_ideas(out)][:count]
    if not ideas:
        print("no clean ideas produced", file=sys.stderr)
        print(out[:600], file=sys.stderr)
        return 1

    date = now_et().strftime("%Y-%m-%d")
    if do_print:
        print(f"[THREAD IDEAS {date}] {len(ideas)} analytical ideas")
        for idea in ideas:
            print("\n" + idea + "\n" + "-" * 40)
    else:
        send_telegram_if_configured(f"[THREAD IDEAS {date}] {len(ideas)} analytical, cross-domain. You write + publish.")
        for idea in ideas:
            send_telegram_if_configured(idea)

    for idea in ideas:
        append_jsonl(IDEAS, {"ts": now_iso(), "date": date, "idea": idea, "status": "surfaced"})
    print(f"done: {len(ideas)} ideas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
