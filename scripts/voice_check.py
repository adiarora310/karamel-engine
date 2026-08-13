#!/usr/bin/env python3
"""Karamel voice check: the "does this sound like me" gut-check.

Takes a generated voice card and drafts a few ORIGINAL posts from it (not
replies), then shows them next to the person's real lines so a human can eyeball
voice match before anything goes live. This is the concierge demo: hand a founder
three posts in their own voice next to three they actually wrote.

It exercises the original-first mechanic on purpose. Original posts are the safe
distribution surface post X-label; this is what the engine should lean on.

Zero-hallucination discipline (Adi's hard rule): the model is told NOT to invent
specifics. Where the voice calls for a fresh stat, date, or quote, it writes a
[VERIFY: what to check] slot instead of a plausible-looking fabrication. The
output is a voice/structure demo, not publish-ready copy, and is labeled as such.

Same $0 plumbing as the rest: claude -p on the subscription, CLAUDE* stripped,
cwd=/tmp. Em-dash scrub. Writes a record to data/voice_cards/<slug>.check.md.

Flags:
  --card PATH      a generated voice card (required)
  --samples PATH   intake JSON, to show the person's real lines side by side
  --n N            how many posts to draft (default 3)
  --print          stdout only, do not write the record file
"""
import json
import re
import sys
from pathlib import Path

import llm
from karamel_common import DATA, has_em_dash, now_et, now_iso

OUT_DIR = DATA / "voice_cards"
EM_DASH = re.compile(r"\s*[—–]\s*")
POST_DELIM = "===POST==="


def scrub(text):
    return EM_DASH.sub(", ", text or "")


def real_lines(samples_path, limit):
    if not samples_path:
        return []
    try:
        data = json.loads(Path(samples_path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [s.strip() for s in (data.get("samples") or []) if str(s).strip()][:limit]


def build_check_prompt(card, n):
    return f"""You draft ORIGINAL posts (not replies) in a person's voice, using only their voice card. The point is to test voice match, so the structure, register, and rhythm must be theirs, exactly as the card describes.

=== VOICE CARD ===
{card}
=== END VOICE CARD ===

Draft {n} original posts this person could publish. Rules:

1. MATCH THE CARD. Use their structure, their register mix, their openers, their sentence length, their obsessions. If the card says "fact then payoff," every post has that shape.
2. ZERO FABRICATION. Do not invent specific statistics, dates, dollar figures, or quotes. This is the hard rule. Where the voice calls for a fresh specific, write a slot like [VERIFY: the exact stat to confirm] instead of a made-up number. Evergreen, widely-known facts are fine. A plausible-looking invented number is NOT.
3. ORIGINAL, not reactive. These are standalone posts, not replies to anyone.
4. NO em-dashes or en-dashes anywhere. Use commas, colons, semicolons, periods.
5. Vary the registers across the {n} posts per the card's mix.

Output EXACTLY this for each post and nothing else:

{POST_DELIM}
REGISTER: <banger | analytical | single-lane analytical>
TEXT: <the post text, no line containing {POST_DELIM}>"""


def call_claude(prompt, system=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="voice-check")


def parse_posts(out):
    posts = []
    for block in out.split(POST_DELIM):
        block = block.strip()
        if not block:
            continue
        reg = re.search(r"REGISTER:\s*(.+)", block)
        txt = re.search(r"TEXT:\s*(.+)", block, re.S)
        if txt:
            posts.append({
                "register": (reg.group(1).strip() if reg else "?"),
                "text": scrub(txt.group(1).strip()),
            })
    return posts


def render(posts, reals, slug):
    L = [
        f"[VOICE CHECK · {slug} · {now_et().strftime('%a %b %d, %H:%M ET')}]",
        "Voice and structure demo. NOT publish-ready: confirm every [VERIFY] slot.",
        "",
    ]
    if reals:
        L.append("YOUR REAL LINES (the bar):")
        for s in reals:
            L.append(f"  · {s}")
        L.append("")
    L.append(f"DRAFTED IN YOUR VOICE ({len(posts)}):")
    L.append("")
    for i, p in enumerate(posts, 1):
        L.append(f"{i}. [{p['register']}]")
        L.append(f"   {p['text']}")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def main():
    if "--card" not in sys.argv:
        print("usage: voice_check.py --card PATH [--samples PATH] [--n N] [--print]",
              file=sys.stderr)
        return 2
    card_path = Path(sys.argv[sys.argv.index("--card") + 1])
    samples_path = (sys.argv[sys.argv.index("--samples") + 1]
                    if "--samples" in sys.argv else None)
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 3
    do_print = "--print" in sys.argv

    if not card_path.exists():
        print(f"voice check error: no such card: {card_path}", file=sys.stderr)
        return 2

    card = card_path.read_text()
    posts = parse_posts(call_claude(build_check_prompt(card, n)))
    if not posts:
        print("voice check error: model returned no parseable posts", file=sys.stderr)
        return 1

    slug = card_path.stem
    report = render(posts, real_lines(samples_path, n), slug)
    if has_em_dash(report):
        report = scrub(report)

    print(report)
    if not do_print:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        rec = OUT_DIR / f"{slug}.check.md"
        rec.write_text(report + f"\n<!-- voice_check {now_iso()} -->\n")
        print(f"(record: {rec})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
