#!/usr/bin/env python3
"""Karamel reflector: the learning loop. Closes the gap between "you corrected a
draft" and "Karamel got better."

Reads drafts.jsonl (the complete record: every status) over a lookback window,
computes the metrics that say whether Karamel sounds like you, then mines the
cases where you EDITED a draft before posting (draft_text -> your posted_text).
Those edits are you correcting the voice. It finds the PATTERNS across them and
proposes surgical voice-card refinements for you to approve.

Hard rules honored:
  - Read-only on the voice card. It PROPOSES; you approve each change by hand.
  - Metrics are computed in Python (real numbers, never model-guessed).
  - claude only ever sees your edit text, and only to find patterns in it.
  - No em-dashes in any output.
  - $0: claude -p on the subscription, no paid API.

Two kinds of skip, kept separate:
  - status "skip"   = Karamel chose not to draft (targeting signal, pre-you).
  - status "skipped"= you hit the ❌ token (draft-quality signal).

The report is not emailed. The weekly digest already ends with voice-card
proposals, and two emails a week both saying "here is where your writing is
drifting" is one more than anybody reads. The analysis still runs and the
report is still written to data/reflections/, where the dashboard and anyone
debugging can read it.

Flags:
  --print        stdout instead of writing artifacts
  --force        run even if paused/halted (reflection never touches X, so safe)
  --days N       lookback window in days (default 14)
  --min-edits N  minimum edited samples before proposing voice changes (default 5)
"""
import re
import sys
from datetime import datetime, timedelta

import llm
import tenants
from karamel_common import (
    DATA, append_jsonl, has_em_dash, now_et, now_iso, now_utc, read_jsonl,
)

# Legacy fallbacks, used only when write_artifacts is called without a tenant.
# Every real path now comes off the tenant: these files are one person's
# analysis of their own unpublished writing, and a shared path would file it in
# somebody else's directory.
REFLECTIONS_DIR = DATA / "reflections"
REFLECTIONS_LOG = DATA / "reflections.jsonl"
EM_DASH = re.compile(r"[—–]")

USER_POSTED = ("posted_clean", "posted_edited")
USER_ACTIONED = ("posted_clean", "posted_edited", "skipped")


def parse_ts(s):
    """Lenient ISO -> aware datetime (UTC). Returns None if unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def row_ts(row):
    """Best available timestamp for a draft row, preferring when the user acted."""
    for key in ("confirmed_ts", "cleared_ts", "notified_ts", "drafted_at"):
        dt = parse_ts(row.get(key))
        if dt:
            return dt
    return None


def in_window(row, since):
    if since is None:
        return True
    dt = row_ts(row)
    return dt is None or dt >= since


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def compute_funnel(drafts, since):
    """Real counts + rates from the master record. Pure: no IO, no model."""
    rows = [r for r in drafts if in_window(r, since)]
    c = {
        "posted_clean": 0, "posted_edited": 0, "skipped": 0,
        "cleared": 0, "pending": 0, "system_skip": 0, "reached": 0,
    }
    for r in rows:
        st = r.get("status")
        if r.get("notified_ts"):
            c["reached"] += 1
        if st == "posted_clean":
            c["posted_clean"] += 1
        elif st == "posted_edited":
            c["posted_edited"] += 1
        elif st == "skipped":
            c["skipped"] += 1
        elif st == "cleared":
            c["cleared"] += 1
        elif st == "pending":
            c["pending"] += 1
        elif st == "skip":
            c["system_skip"] += 1

    posted = c["posted_clean"] + c["posted_edited"]
    actioned = posted + c["skipped"]
    c["posted"] = posted
    c["actioned"] = actioned
    c["send_rate"] = pct(posted, actioned)
    c["clean_rate"] = pct(c["posted_clean"], posted)
    c["edit_rate"] = pct(c["posted_edited"], posted)
    c["skip_rate"] = pct(c["skipped"], actioned)
    return c


def collect_edit_pairs(drafts, since):
    """The gold: cases where you edited our draft before posting."""
    pairs = []
    for r in drafts:
        if r.get("status") != "posted_edited" or not in_window(r, since):
            continue
        draft = (r.get("draft_text") or "").strip()
        final = (r.get("edited_text") or r.get("posted_text") or "").strip()
        if not draft or not final or draft == final:
            continue
        pairs.append({
            "original": (r.get("original_text") or "").strip()[:280],
            "draft": draft,
            "final": final,
            "lane": " + ".join(r.get("lane_fit") or []) or "?",
        })
    return pairs


def collect_targeting_skips(drafts, since, limit=20):
    """Karamel-side skips (status 'skip'): what we drafted-then-dropped. Targeting
    signal, not voice signal. We surface the reasons, not feed them to the card."""
    out = []
    for r in drafts:
        if r.get("status") != "skip" or not in_window(r, since):
            continue
        reason = (r.get("skip_reason") or "").strip()
        if reason:
            out.append(reason)
    return out[:limit]


def build_refinement_prompt(pairs, author=None):
    """The card is NOT in here. It travels as a cached system block, byte
    identical to the one the maker and the critic send, so all three share one
    cache entry instead of buying the same 20KB three times. On the cli provider
    it is folded back into the prompt anyway, so nothing is lost either way."""
    cases = []
    for i, p in enumerate(pairs, 1):
        cases.append(
            f"CASE {i} (lane: {p['lane']}):\n"
            f"  They were replying to: \"{p['original']}\"\n"
            f"  Karamel drafted:        \"{p['draft']}\"\n"
            f"  User actually posted:  \"{p['final']}\""
        )
    cases_block = "\n\n".join(cases)
    who = author or "the user"
    return f"""You are the voice-tuning analyst for Karamel. Your job is to make future drafts need less editing.

{who}'s voice card is in your system prompt. Below is a set of cases where Karamel drafted something and {who} EDITED it before posting. Each edit is them correcting the voice. Your task: find the PATTERNS across these edits and propose surgical changes to the card so the next drafts match them without editing.

EDIT CASES (draft -> what they actually posted):

{cases_block}

RULES:
- Only propose a refinement if you see the SAME kind of correction in at LEAST 2 cases. One-offs are noise; ignore them.
- Ground every observation in specific cases (cite case numbers).
- Each CHANGE must be a concrete line to add to or modify in the voice card, written so it can be pasted in. Quote it.
- Do not restate rules already in the voice card. Only propose what is missing or wrong.
- No em-dashes or en-dashes anywhere. Use commas, colons, periods.
- If the edits are idiosyncratic with no repeating pattern, say so and propose nothing.

Output EXACTLY this structure and nothing else:

READ: <one honest sentence on what the edits reveal about where Karamel is off>
REFINEMENT 1
WHAT: <the repeating pattern, citing case numbers>
CHANGE: <the exact voice-card line to add or change, in quotes>
REFINEMENT 2
WHAT: ...
CHANGE: ...
(repeat for as many real patterns as you found, or stop after READ if none)"""


def call_claude(prompt, system=None, tenant=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="reflector", tenant=tenant)


def parse_refinements(out):
    """Return (read_line, [ {what, change} ]). Tolerant of spacing."""
    out = EM_DASH.sub(", ", out or "")
    read_line = ""
    m = re.search(r"^READ:\s*(.+)$", out, re.MULTILINE)
    if m:
        read_line = m.group(1).strip()
    refinements = []
    blocks = re.split(r"\n\s*REFINEMENT\s+\d+\s*\n", "\n" + out)
    for b in blocks[1:]:
        what = re.search(r"WHAT:\s*(.+?)(?:\n[A-Z]+:|\Z)", b, re.S)
        change = re.search(r"CHANGE:\s*(.+?)(?:\n\s*REFINEMENT|\Z)", b, re.S)
        if what and change:
            refinements.append({
                "what": what.group(1).strip(),
                "change": change.group(1).strip(),
            })
    return read_line, refinements


def format_report(funnel, read_line, refinements, n_pairs, days):
    f = funnel
    L = [
        f"[KARAMEL REFLECTION · {now_et().strftime('%a %b %d, %H:%M ET')} · last {days}d]",
        "",
        "How much it sounds like you:",
        f"  Sent:    {f['posted']}  (clean {f['posted_clean']} / edited {f['posted_edited']})",
        f"  Skipped: {f['skipped']}    Cleared: {f['cleared']}    Pending: {f['pending']}",
        "",
        f"  Send rate:  {f['send_rate']:.0f}%   (of what you actioned, you posted)",
        f"  Clean rate: {f['clean_rate']:.0f}%   (of what you posted, untouched)",
        f"  Edit rate:  {f['edit_rate']:.0f}%   (of what you posted, you fixed first)",
        f"  Skip rate:  {f['skip_rate']:.0f}%",
    ]
    if f["system_skip"]:
        L.append(f"  (Karamel self-skipped {f['system_skip']} before they reached you, targeting filter)")
    L.append("")

    if f["actioned"] == 0:
        L.append("No engagement signal yet. Once you reply to drafts (✅ / ✏️ <edit> / ❌),")
        L.append("I will have your corrections to learn from.")
        return "\n".join(L)

    if not refinements:
        if n_pairs < 1:
            L.append("No edited drafts yet, so no voice corrections to mine.")
            L.append("Clean rate above is the number to watch.")
        else:
            L.append(f"{n_pairs} edited draft(s) so far. Not enough repeating pattern to")
            L.append("propose a voice change yet. I will keep watching.")
        return "\n".join(L)

    if read_line:
        L.append(f"Read: {read_line}")
        L.append("")
    L.append(f"Proposed voice-card refinements (from {n_pairs} of your edits).")
    L.append("Reply with the numbers you approve; I apply them by hand to 03_voice_card.md.")
    L.append("")
    for i, r in enumerate(refinements, 1):
        L.append(f"{i}. {r['what']}")
        L.append(f"   ADD: {r['change']}")
        L.append("")
    return "\n".join(L).rstrip()


def write_artifacts(report, funnel, refinements, tenant=None):
    """Where one person's analysis of their own writing lands.

    Per tenant, not shared. These files describe how a named individual edits
    their own drafts, so a shared path would put one person's weekly report,
    quoting their unpublished writing, in another person's directory."""
    out_dir = tenant.reflections_dir if tenant else REFLECTIONS_DIR
    log = tenant.reflections_log if tenant else REFLECTIONS_LOG
    out_dir.mkdir(parents=True, exist_ok=True)
    date = (tenant.now() if tenant else now_et()).strftime("%Y-%m-%d")
    (out_dir / f"{date}.md").write_text(report + "\n")
    append_jsonl(log, {
        "ts": now_iso(),
        "date": date,
        "tenant": tenant.id if tenant else None,
        "funnel": funnel,
        "n_refinements": len(refinements),
        "refinements": refinements,
    })


def run_tenant(tenant, days=14, min_edits=5, do_print=False):
    """One person's reflection cycle. Returns the funnel, or None if skipped."""
    if not tenant.enabled:
        print(f"[{tenant.id}] disabled, skipping")
        return None
    card = tenant.voice_card_path
    if not card.exists():
        print(f"[{tenant.id}] no voice card at {card}, skipping", file=sys.stderr)
        return None

    since = now_utc() - timedelta(days=days)
    drafts = read_jsonl(tenant.drafts_path) + read_jsonl(tenant.original_drafts_path)
    funnel = compute_funnel(drafts, since)
    pairs = collect_edit_pairs(drafts, since)

    read_line, refinements = "", []
    if len(pairs) >= min_edits:
        try:
            out = call_claude(
                build_refinement_prompt(pairs, author=tenant.name),
                system=card.read_text(), tenant=tenant.id,
            )
            read_line, refinements = parse_refinements(out)
        except Exception as e:
            print(f"[{tenant.id}] refinement step failed (metrics still "
                  f"reported): {e}", file=sys.stderr)

    report = format_report(funnel, read_line, refinements, len(pairs), days)
    # The em-dash scrub is the tenant's rule, not the house's. A tenant whose own
    # prose uses them was having their own report rewritten.
    if not tenant.allow_em_dash and has_em_dash(report):
        report = EM_DASH.sub(", ", report)

    if do_print:
        print(f"\n===== {tenant.name} ({tenant.id}) =====")
        print(report)
    else:
        # No longer emailed. The weekly digest already ends with a section of
        # voice-card proposals, and two emails a week both saying "here is what
        # your writing is drifting toward" is one more than anybody reads.
        #
        # The analysis itself still runs, because it is the loop that makes the
        # card sharpen against what somebody actually publishes, and the report
        # is still written to disk where the dashboard and a person debugging
        # can read it. Only the delivery is gone.
        write_artifacts(report, funnel, refinements, tenant=tenant)

    print(
        f"[{tenant.id}] done: actioned={funnel['actioned']} "
        f"clean_rate={funnel['clean_rate']:.0f}% edits={len(pairs)} "
        f"refinements={len(refinements)}",
        file=sys.stderr,
    )
    return funnel


def selftest():
    import tempfile
    from pathlib import Path

    # The voice card must NOT be in the prompt. It rides as a cached system
    # block, byte identical to what the maker and the critic send, so all three
    # share one cache entry rather than buying the same 20KB three times.
    pairs = [{"lane": "x", "original": "o", "draft": "d", "final": "f"}]
    prompt = build_refinement_prompt(pairs, author="Jane Doe")
    assert "CURRENT VOICE CARD" not in prompt, "the card leaked back into the prompt"
    assert "Jane Doe" in prompt, "the analyst must know whose voice this is"
    assert "CASE 1" in prompt

    # Whose report this is decides where it lands. These files quote one
    # person's unpublished writing back at them.
    t_legacy = tenants.legacy_tenant()
    t_other = tenants.Tenant({"id": "someone", "name": "Someone"})
    assert t_other.reflections_dir != t_legacy.reflections_dir
    assert t_other.reflections_log != t_legacy.reflections_log

    with tempfile.TemporaryDirectory() as d:
        class _T:
            id, name = "tmp", "Tmp"
            reflections_dir = Path(d) / "refl"
            reflections_log = Path(d) / "refl.jsonl"
            def now(self):
                return now_et()
        write_artifacts("a report", {"actioned": 1}, [], tenant=_T())
        written = list((Path(d) / "refl").glob("*.md"))
        assert len(written) == 1, written
        assert written[0].read_text().startswith("a report")
        rows = read_jsonl(Path(d) / "refl.jsonl")
        assert rows and rows[0]["tenant"] == "tmp", rows

    # A tenant with no card is skipped rather than crashing mid-report, and a
    # disabled one is not processed at all.
    class _NoCard:
        id, name, enabled, allow_em_dash = "nc", "No Card", True, False
        voice_card_path = Path("/definitely/not/here.md")
    assert run_tenant(_NoCard(), do_print=True) is None

    class _Off:
        id, name, enabled = "off", "Off", False
    assert run_tenant(_Off(), do_print=True) is None

    print("reflector selftest: all assertions passed")
    return True


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    do_print = "--print" in sys.argv
    days = 14
    min_edits = 5
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    if "--min-edits" in sys.argv:
        min_edits = int(sys.argv[sys.argv.index("--min-edits") + 1])

    if "--all" in sys.argv:
        ids = [t.id for t in tenants.list_tenants()]
        if not ids:
            ids = [tenants.LEGACY_TENANT]
    elif "--tenant" in sys.argv:
        ids = [sys.argv[sys.argv.index("--tenant") + 1]]
    else:
        ids = [tenants.LEGACY_TENANT]

    rc = 0
    for tid in ids:
        t = tenants.load_tenant(tid)
        if t is None:
            print(f"no such tenant: {tid}", file=sys.stderr)
            rc = 1
            continue
        run_tenant(t, days=days, min_edits=min_edits, do_print=do_print)
    return rc


if __name__ == "__main__":
    sys.exit(main())
