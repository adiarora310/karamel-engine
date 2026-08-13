#!/usr/bin/env python3
"""Karamel weekly digester: Sunday 6pm ET. Aggregate the last 7 days of the
reply loop (engagement.jsonl + drafts.jsonl) and the content engine
(bangers.jsonl, content_ideas.jsonl). Produce a digest markdown + a Telegram
summary, and propose voice-card refinements (Adi confirms, never auto-applied).

No X access (reads local logs only), so it runs regardless of /halt.
Flags: --print (stdout only, no file/Telegram), --days N (default 7)
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta

from karamel_common import DATA, ROOT, now_et, now_iso, read_jsonl, send_telegram_if_configured
from shared import DRAFTS, ENGAGEMENT

BANGERS = DATA / "bangers.jsonl"
IDEAS = DATA / "content_ideas.jsonl"
DIGEST_DIR = DATA / "weekly_digests"


def parse_ts(row):
    for k in ("ts_iso", "ts", "drafted_at", "confirmed_ts", "queued_ts"):
        v = row.get(k)
        if v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def within(rows, since):
    out = []
    for r in rows:
        t = parse_ts(r)
        if t and t >= since:
            out.append(r)
    return out


def score(e):
    m = e.get("metrics_24h") or {}
    if m.get("deleted"):
        return -1
    return (m.get("likes") or 0) + 3 * (m.get("replies") or 0) + 2 * (m.get("reposts") or 0)


def main():
    do_print = "--print" in sys.argv
    days = 7
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    since = datetime.now(timezone.utc) - timedelta(days=days)

    drafts = within(read_jsonl(DRAFTS), since)
    eng = within(read_jsonl(ENGAGEMENT), since)
    bangers = within(read_jsonl(BANGERS), since)
    ideas = within(read_jsonl(IDEAS), since)

    posted = [d for d in drafts if d.get("status") in ("posted_clean", "posted_edited")]
    edited = [d for d in drafts if d.get("status") == "posted_edited"]
    skipped = [d for d in drafts if d.get("status") in ("skip", "skipped")]
    evaluated = [e for e in eng if e.get("metrics_24h") and not (e["metrics_24h"] or {}).get("deleted")]
    top = sorted(evaluated, key=score, reverse=True)[:5]

    date = now_et().strftime("%Y-%m-%d")
    L = []
    L.append(f"# Karamel weekly digest, {date}")
    L.append(f"\nWindow: last {days} days. Generated {now_iso()}.\n")
    L.append("## Reply loop")
    L.append(f"- Drafts surfaced: {len(drafts)}")
    L.append(f"- Posted: {len(posted)} ({len(edited)} edited)")
    L.append(f"- Skipped: {len(skipped)}")
    L.append(f"- Posted replies with 24h metrics: {len(evaluated)}")

    if top:
        L.append("\n### Top landings")
        for e in top:
            m = e["metrics_24h"]
            L.append(f"- @{e.get('handle','?')} · {m.get('likes',0)}L / {m.get('replies',0)}R / {m.get('reposts',0)}RT")
            L.append(f"  \"{(e.get('posted_text') or e.get('draft_text') or '')[:160]}\"")

    if skipped:
        L.append("\n### Skip reasons (pattern check)")
        reasons = {}
        for d in skipped:
            r = (d.get("skip_reason") or "unspecified")[:60]
            reasons[r] = reasons.get(r, 0) + 1
        for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
            L.append(f"- {c}x  {r}")

    L.append("\n## Content engine")
    L.append(f"- Bangers surfaced: {len(bangers)}")
    L.append(f"- Analytical thread ideas surfaced: {len(ideas)}")

    # Voice-card refinement proposals (heuristic; Adi confirms, never auto-applied)
    L.append("\n## Voice-card refinement proposals (Adi confirms; nothing auto-applied)")
    proposals = []
    if posted and edited and len(edited) / max(len(posted), 1) > 0.5:
        proposals.append("You edited >50% of posted drafts. The drafter voice is drifting; worth a heavy-edit sample review to retune the voice card.")
    if evaluated and top:
        proposals.append(f"Top landing pattern: lane-fit was {top[0].get('lane_fit')}. If this repeats 3+ weeks, bias the drafter toward it.")
    if not evaluated:
        proposals.append("No posted-reply metrics yet this week. Once you post + confirm with the X URL, the evaluator will start filling this in.")
    if not proposals:
        proposals.append("No strong patterns yet. Need more weeks of data.")
    for p in proposals:
        L.append(f"- {p}")

    digest = "\n".join(L)

    if do_print:
        print(digest)
        return 0

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{date}.md"
    path.write_text(digest)

    summary = (
        f"📊 Weekly digest {date}\n"
        f"Replies: {len(posted)} posted ({len(edited)} edited), {len(skipped)} skipped, "
        f"{len(evaluated)} with metrics\n"
        f"Content: {len(bangers)} bangers, {len(ideas)} thread ideas\n"
    )
    if top:
        m = top[0]["metrics_24h"]
        summary += f"Top: @{top[0].get('handle','?')} {m.get('likes',0)}L/{m.get('replies',0)}R\n"
    summary += f"Full digest written to data/weekly_digests/{date}.md"
    send_telegram_if_configured(summary)

    # best-effort: commit the digest so the fork chat can read it from the repo
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", str(path)], check=False, capture_output=True, timeout=20)
        subprocess.run(
            ["git", "-C", str(ROOT), "-c", "user.name=Karamel", "-c", "user.email=karamel@localhost",
             "commit", "-m", f"weekly digest {date}"],
            check=False, capture_output=True, timeout=20,
        )
        subprocess.run(["git", "-C", str(ROOT), "push"], check=False, capture_output=True, timeout=30)
    except Exception as e:
        print(f"digest git push best-effort failed: {e}", file=sys.stderr)

    print(f"done: digest written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
