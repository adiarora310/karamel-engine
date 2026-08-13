#!/usr/bin/env python3
"""Karamel drafter v2.1: for each candidate without a draft, call `claude -p`
(text-only, subscription auth, $0 marginal) with voice card v1.2 as context.
Appends to data/drafts.jsonl with status=pending for the notifier to push.

Runs under launchd every 20 min. Window gate + pause check. Em-dash scrub
with one retry. Politics/sensitive scrub is delegated to the voice card's
hard "do not" lines: the model returns SKIP for those.

Flags: --force (skip window gate), --limit N (max candidates this run)
"""
import re
import sys
import time
from datetime import datetime, timezone

import llm
from karamel_common import (
    DATA, ROOT, append_jsonl, compose_url, in_posting_window, is_paused,
    now_iso, read_jsonl,
)
import tenants
from safety import MAX_REPLIES_PER_DAY, reply_allowed, reply_count_today

CANDIDATES = DATA / "candidates.jsonl"
DRAFTS = DATA / "drafts.jsonl"
VOICE_CARD = ROOT / "03_voice_card.md"
EM_DASH = re.compile("[–—]")
MAX_CANDIDATE_AGE_BEFORE_DRAFT_MIN = 90  # stale candidates aren't worth drafting
# Whose budget and files this bills. Reads the owner of this installation
# rather than a name compiled in: on a self-hosted copy the operator is not
# called "adi", and hardcoding it billed their replies to a tenant that does not
# exist on their machine while reading paths that are not theirs.
TENANT = tenants.LEGACY_TENANT


def effective_age_minutes(cand):
    """How old the post is NOW, not at discovery. cand['age_minutes'] is frozen
    at the moment the listener saw it, so a candidate that sat in the file for a
    day still reports its discovery-time age. Returns None if undeterminable."""
    base = cand.get("age_minutes")
    if base is None:
        return None
    try:
        seen = datetime.fromisoformat(
            (cand.get("discovered_at") or "").replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        return base
    waited = (datetime.now(timezone.utc) - seen).total_seconds() // 60
    return int(base + max(0, waited))


def split_lanes(lane_str):
    """Model returns e.g. 'pop-culture x investing' or 'single-lane investing'.
    notifier.format_draft does ' + '.join(lane_fit), so return a list."""
    if not lane_str:
        return []
    parts = re.split(r"\s*(?:x|×|\+|,)\s*", lane_str.strip(), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def build_prompt(cand, voice, retry=False, author=None):
    retry_note = (
        "\nPREVIOUS ATTEMPT WAS REJECTED for containing an em-dash or "
        "en-dash. Use commas, colons, or periods instead.\n"
        if retry
        else ""
    )
    who = author or tenants.default_author()
    return f"""You are drafting an X reply in {who}'s voice. His full voice card follows between the markers. Obey every rule in it, especially the anti-patterns and hard "do not" lines.

=== VOICE CARD START ===
{voice}
=== VOICE CARD END ===

Original post by @{cand['author_handle']} (Tier {cand['tier']}, {cand['age_minutes']} min old, {cand['likes_at_discovery']} likes at discovery):
\"\"\"{cand['text']}\"\"\"

Draft ONE reply per the voice card. 1 to 3 sentences. Add a fact, a counter, or a specific observation. Pick the register per the voice card's "When to use which" rules.

If the post violates a hard "do not" line (partisan politics, etc.) or you cannot add something genuinely specific, return exactly one line: SKIP: <one-line reason>

Otherwise return exactly three lines, nothing else:
LANE_FIT: <e.g. pop-culture x investing, or single-lane investing>
REASONING: <one line on why this adds value>
DRAFT: <the reply text>{retry_note}"""


def call_claude(prompt, system=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="drafter")


def parse_response(out):
    """Return (lane_fit, reasoning, draft) or (None, None, skip_reason)."""
    if out.lstrip().upper().startswith("SKIP:"):
        return None, None, out.lstrip()[5:].strip()
    fields = {}
    for key in ("LANE_FIT", "REASONING", "DRAFT"):
        m = re.search(rf"^{key}:\s*(.+)$", out, re.MULTILINE)
        if m:
            fields[key] = m.group(1).strip()
    if "DRAFT" not in fields:
        return None, None, f"unparseable response: {out[:160]}"
    return (
        fields.get("LANE_FIT", "unknown"),
        fields.get("REASONING", ""),
        fields["DRAFT"],
    )


def main():
    force = "--force" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    if is_paused():
        print("paused/halted, exiting")
        return 0
    if not force and not in_posting_window():
        print("outside posting window, exiting")
        return 0

    # Same gate the listener and notifier consult. Drafting past a closed gate
    # only builds a backlog that can never be pushed, at one claude -p call each.
    gate_ok, gate_why = reply_allowed(TENANT)
    if not gate_ok:
        print(f"reply gate closed for tenant '{TENANT}': {gate_why}")
        return 0

    drafted_ids = {r["tweet_id"] for r in read_jsonl(DRAFTS)}
    pending = [
        c for c in read_jsonl(CANDIDATES) if c["tweet_id"] not in drafted_ids
    ]

    # Staleness. MAX_CANDIDATE_AGE_BEFORE_DRAFT_MIN has been declared since v2.1
    # and never read, so nothing stopped the drafter writing a reply to a post
    # from days ago. Replying that late is both useless and conspicuous, and it
    # is exactly what a restart after a long gap produces: candidates.jsonl
    # still holds rows the listener found before it was retired.
    fresh, stale = [], 0
    for c in pending:
        age = effective_age_minutes(c)
        if age is not None and age > MAX_CANDIDATE_AGE_BEFORE_DRAFT_MIN:
            stale += 1
            continue
        fresh.append(c)
    if stale:
        print(f"skipped {stale} stale candidate(s) over "
              f"{MAX_CANDIDATE_AGE_BEFORE_DRAFT_MIN}m old")
    pending = fresh

    # Never draft more than can actually be sent today.
    replies_left = MAX_REPLIES_PER_DAY - reply_count_today(TENANT)
    if limit:
        replies_left = min(replies_left, limit)
    if len(pending) > replies_left:
        print(f"trimming {len(pending)} candidates to {replies_left} (reply budget)")
        pending = pending[:replies_left]

    if not pending:
        print("no undrafted candidates")
        return 0

    voice = VOICE_CARD.read_text()
    drafted, skipped = 0, 0

    for cand in pending:
        out = call_claude(build_prompt(cand, voice))
        lane, reasoning, draft = parse_response(out)

        if lane is None:  # skip: parse_response put the reason in slot 3
            append_jsonl(
                DRAFTS,
                {
                    "draft_id": int(time.time() * 1000),
                    "tweet_id": cand["tweet_id"],
                    "status": "skip",
                    "skip_reason": draft,
                    "candidate": cand,
                    "drafted_at": now_iso(),
                },
            )
            skipped += 1
            print(f"SKIP @{cand['author_handle']}: {draft}")
            continue

        # em-dash scrub, one retry
        if EM_DASH.search(draft):
            out = call_claude(build_prompt(cand, voice, retry=True))
            lane, reasoning, draft = parse_response(out)
            if lane is None or EM_DASH.search(draft or ""):
                append_jsonl(
                    DRAFTS,
                    {
                        "draft_id": int(time.time() * 1000),
                        "tweet_id": cand["tweet_id"],
                        "status": "skip",
                        "skip_reason": "em-dash after retry",
                        "candidate": cand,
                        "drafted_at": now_iso(),
                    },
                )
                skipped += 1
                continue

        append_jsonl(
            DRAFTS,
            {
                "draft_id": int(time.time() * 1000),
                "tweet_id": cand["tweet_id"],
                # notifier.format_draft reads these exact keys:
                "handle": "@" + cand["author_handle"].lstrip("@"),
                "author": cand.get("author_name", cand["author_handle"]),
                "tier": cand["tier"],
                "age_minutes": cand.get("age_minutes"),
                "current_likes": cand.get("likes_at_discovery"),
                "original_text": cand["text"][:280],
                "draft_text": draft,
                "lane_fit": split_lanes(lane),  # list, notifier does " + ".join
                # kept for the read side / digester:
                "author_handle": cand["author_handle"],
                "reasoning": reasoning,
                "compose_url": compose_url(cand["tweet_id"], draft),
                "status": "pending",
                "telegram_msg_id": None,
                "drafted_at": now_iso(),
            },
        )
        drafted += 1
        print(f"DRAFT @{cand['author_handle']} [{lane}]: {draft}")

    print(f"done: {drafted} drafted, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
