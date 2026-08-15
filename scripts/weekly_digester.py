#!/usr/bin/env python3
"""Karamel weekly digest. Aggregate the last 7 days of original posts and the
reply loop, deliver it as plain text, and propose voice-card refinements that
the person confirms and nothing auto-applies.

No X access (reads local logs only), so it runs regardless of /halt.

Fires daily and delivers weekly, gated on when it last delivered rather than on
the day of the week. The plist asks for 18:00; if the Mac is asleep at 18:00 on
a Sunday, launchd runs the missed job on wake, and a weekday check would then
look at Monday and skip the week entirely. A watermark cannot lose a week that
way, only move it later.

Flags:
  --all            every registered tenant
  --tenant ID      one tenant
  --print          stdout only: no file, no delivery, no watermark
  --force          deliver even if the last one was less than 7 days ago
  --days N         window to aggregate (default 7)
"""
import sys
from datetime import datetime, timezone, timedelta

import channels
import tenants
from karamel_common import now_et, now_iso, read_jsonl

EVERY_DAYS = 7


def parse_ts(row):
    # generated_at is the key generated.jsonl uses, and it was missing from this
    # list when the original-post section was added, so every row from that file
    # fell out of the window and the digest reported "Cleared the critic: 0/0"
    # on a week with five drafts. A row whose timestamp cannot be read is
    # dropped by within(), which makes a missing key here look like no activity
    # rather than like a bug.
    for k in ("ts_iso", "ts", "drafted_at", "generated_at", "confirmed_ts",
              "queued_ts", "added_at"):
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


def _arg(flag, default=None, cast=str):
    """Value after `flag`, or the default when it is absent, last, or unusable.

    Bounds-checked because the bare sys.argv[index+1] form crashes with an
    IndexError when the flag is typed without its value, and under launchd that
    arrives as a traceback in a .err file rather than a message anyone can act
    on. critic.py grew the same helper after the same mistake shipped there."""
    if flag not in sys.argv:
        return default
    i = sys.argv.index(flag) + 1
    if i >= len(sys.argv):
        return default
    try:
        return cast(sys.argv[i])
    except (TypeError, ValueError):
        return default


def plural(n, suffix="s"):
    """Accepts a count or anything with a length. "1 draft", "2 drafts"."""
    n = n if isinstance(n, int) else len(n)
    return "" if n == 1 else suffix


def digest_dir(t):
    return t.data_dir / "weekly_digests"


def watermark(t):
    return digest_dir(t) / ".last_delivered"


def due(t, force=False):
    """Whether a digest is owed. Never-delivered counts as due."""
    if force:
        return True, "forced"
    p = watermark(t)
    if not p.exists():
        return True, "never delivered"
    try:
        last = datetime.fromisoformat(p.read_text().strip().replace("Z", "+00:00"))
        # Inside the guard, not after it. A timestamp with no offset parses
        # perfectly and then raises TypeError on the subtraction below, which is
        # not a ValueError and so escaped as an unhandled crash: the agent exits
        # non-zero and no digest is ever delivered again. A watermark nobody can
        # interpret means due, however it fails to be interpretable.
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last).days
    except (ValueError, OSError, TypeError):
        return True, "unreadable watermark"
    if age >= EVERY_DAYS:
        return True, f"{age} days since the last one"
    return False, f"delivered {age} day(s) ago, next in {EVERY_DAYS - age}"


def run_tenant(t, days=7, do_print=False, force=False):
    if not do_print:
        owed, why = due(t, force=force)
        if not owed:
            print(f"[{t.id}] skipped: {why}")
            return 0

    since = datetime.now(timezone.utc) - timedelta(days=days)

    drafts = within(read_jsonl(t.drafts_path), since)
    eng = within(read_jsonl(t.engagement_path), since)
    posted = [d for d in drafts if d.get("status") in ("posted_clean", "posted_edited")]
    edited = [d for d in drafts if d.get("status") == "posted_edited"]
    skipped = [d for d in drafts if d.get("status") in ("skip", "skipped")]
    evaluated = [e for e in eng if e.get("metrics_24h") and not (e["metrics_24h"] or {}).get("deleted")]
    top = sorted(evaluated, key=score, reverse=True)[:5]

    # The original-post half. Reply mining is off unless somebody deliberately
    # turns it on, so on a default install every number below this line is zero
    # and a digest built only from the reply loop is a weekly email that says
    # nothing happened during a week that produced fourteen drafts.
    originals = within(read_jsonl(t.original_drafts_path), since)
    o_answered = [d for d in originals if d.get("confirmed_ts")]
    o_posted = [d for d in originals
                if d.get("status") in ("posted_clean", "posted_edited")]
    gen = within(read_jsonl(t.generated_path), since)
    gen_pass = [g for g in gen if g.get("verdict") == "PASS"]

    date = now_et().strftime("%Y-%m-%d")
    span = f"{(datetime.now(timezone.utc) - timedelta(days=days)).strftime('%-d %B')} to {now_et().strftime('%-d %B')}"

    # Plain text, in the same voice as the drafts. This used to be markdown,
    # which nothing renders in a plain-text email, so a reader got literal #
    # and ## characters down the left margin.
    #
    # The Content engine section is gone. It counted bangers.jsonl and
    # content_ideas.jsonl, which are written by banger.py and
    # content_prompter.py, and neither is in AGENTS_SPEC. Nothing schedules
    # them, so the section reported zero every week for everyone: a line that
    # can only ever say nothing happened teaches a reader to skim past the
    # section that can.
    L = []
    L.append(f"Summary: Your week, {span}")
    L.append("")
    L.append("Original posts")
    if originals:
        L.append(f"You got {len(originals)} draft{plural(originals)}. "
                 f"You answered {len(o_answered)} and posted {len(o_posted)}.")
    else:
        L.append("No drafts reached you this week.")
    if gen:
        L.append(f"Karamel wrote {len(gen)} and kept {len(gen_pass)}. "
                 f"The rest failed its own gate before you saw them.")

    L.append("")
    L.append("Replies")
    if drafts:
        L.append(f"It drafted {len(drafts)}. You posted {len(posted)}, "
                 f"edited {len(edited)} of those, and skipped {len(skipped)}.")
    else:
        L.append("No replies drafted this week.")
    if evaluated:
        L.append(f"{len(evaluated)} posted repl{'y' if len(evaluated) == 1 else 'ies'} "
                 f"came back with 24 hour numbers.")

    if top:
        L.append("")
        L.append("What landed best")
        for e in top:
            m = e["metrics_24h"]
            L.append(f"@{e.get('handle','?')}, {m.get('likes',0)} likes, "
                     f"{m.get('replies',0)} replies, {m.get('reposts',0)} reposts")
            L.append(f'"{(e.get("posted_text") or e.get("draft_text") or "")[:160]}"')

    if skipped:
        reasons = {}
        for d in skipped:
            r = (d.get("skip_reason") or "unspecified").strip()
            reasons[r] = reasons.get(r, 0) + 1
        L.append("")
        L.append("Why things were skipped")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
            L.append(f"{c}x  {r[:120]}")

    # Heuristic, and never auto-applied: the person confirms. A voice card that
    # edits itself from one week of edits drifts fast.
    proposals = []
    if posted and edited and len(edited) / max(len(posted), 1) > 0.5:
        proposals.append(
            "You edited more than half of what you posted, so the voice is "
            "drifting. Worth reading a few of your edits side by side with the "
            "drafts to see what the card is getting wrong.")
    if evaluated and top:
        proposals.append(
            f"Your best reply this week sat in {top[0].get('lane_fit')}. "
            f"If that repeats for three weeks it is worth leaning into.")
    if not evaluated:
        proposals.append(
            "No numbers on posted replies yet. They start arriving once you "
            "post one and reply to the email with the X link.")
    if not proposals:
        proposals.append("No clear patterns yet. It needs more weeks.")

    L.append("")
    L.append(f"What Karamel noticed, for you to confirm. Nothing here is "
             f"applied on its own.")
    for prop in proposals:
        L.append(prop)

    digest = "\n".join(L)

    if do_print:
        print(digest)
        return 0

    d = digest_dir(t)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{date}.md"
    path.write_text(digest)

    # The whole digest, not a teaser pointing at a file on a Mac they may not be
    # sitting at. It is a page of text once a week.
    subject = "[Karamel] Your week in review!"
    try:
        channels.send(t, digest, subject=subject)
    except Exception as e:
        # The digest is already on disk, so a delivery failure must not look
        # like a successful week. Leave the watermark alone: not writing it is
        # what makes the next run retry instead of skipping seven days.
        print(f"[{t.id}] digest written to {path} but delivery failed: {e}",
              file=sys.stderr)
        return 1

    watermark(t).write_text(now_iso())
    print(f"[{t.id}] digest delivered and written to {path}")
    return 0


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    do_print = "--print" in sys.argv
    force = "--force" in sys.argv
    days = _arg("--days", 7, cast=int)

    if "--all" in sys.argv:
        ids = [t.id for t in tenants.list_tenants()]
        if not ids:
            ids = [tenants.LEGACY_TENANT]
    elif "--tenant" in sys.argv:
        one = _arg("--tenant", None)
        if one is None:
            print("--tenant needs an id", file=sys.stderr)
            return 2
        ids = [one]
    else:
        ids = [tenants.LEGACY_TENANT]

    rc = 0
    for tid in ids:
        t = tenants.load_tenant(tid)
        if t is None:
            print(f"no such tenant: {tid}", file=sys.stderr)
            rc = 1
            continue
        rc = run_tenant(t, days=days, do_print=do_print, force=force) or rc
    return rc


def selftest():
    import tempfile, types
    from pathlib import Path

    # due() is the whole weekly gate, and the failure that matters is a missed
    # week rather than an early one: a Mac asleep at 18:00 must still deliver on
    # wake. Never-delivered and unreadable both have to read as due, because the
    # alternative is a component that silently never runs -- which is the exact
    # bug this file is being fixed for.
    with tempfile.TemporaryDirectory() as d:
        t = types.SimpleNamespace(id="x", data_dir=Path(d))
        owed, why = due(t)
        assert owed and "never" in why, (owed, why)

        digest_dir(t).mkdir(parents=True, exist_ok=True)
        watermark(t).write_text(
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        owed, why = due(t)
        assert not owed, why
        assert due(t, force=True)[0], "--force must override the watermark"

        watermark(t).write_text(
            (datetime.now(timezone.utc) - timedelta(days=9)).isoformat())
        assert due(t)[0], "nine days must be due"

        watermark(t).write_text("not a timestamp")
        owed, why = due(t)
        assert owed and "unreadable" in why, (owed, why)

        # A naive timestamp parses fine and then blows up on the subtraction
        # with TypeError, which is not a ValueError. Before this was folded into
        # the guard it escaped as an unhandled crash and no digest was ever
        # delivered again.
        watermark(t).write_text("2026-01-01T00:00:00")
        owed, _ = due(t)
        assert owed, "a naive watermark must not raise"

    # Every file this digest reads must have its timestamp key understood.
    # within() drops a row it cannot date, so a key missing from parse_ts reads
    # as "nothing happened" rather than as a bug: the original-post section
    # shipped reporting 0/0 on a week with five drafts because generated.jsonl
    # stamps generated_at and this list did not know it.
    now = datetime.now(timezone.utc).isoformat()
    for key in ("ts_iso", "drafted_at", "generated_at", "confirmed_ts",
                "queued_ts", "added_at"):
        assert parse_ts({key: now}) is not None, key
    assert parse_ts({"no_timestamp_here": now}) is None
    assert parse_ts({"generated_at": "not a date"}) is None

    # Flags typed without their value fall back instead of raising IndexError.
    _real_argv = sys.argv
    try:
        sys.argv = ["weekly_digester.py", "--days"]
        assert _arg("--days", 7, cast=int) == 7, "missing value -> default"
        sys.argv = ["weekly_digester.py", "--days", "notanumber"]
        assert _arg("--days", 7, cast=int) == 7, "uncastable -> default"
        sys.argv = ["weekly_digester.py", "--days", "14"]
        assert _arg("--days", 7, cast=int) == 14
        sys.argv = ["weekly_digester.py", "--tenant"]
        assert _arg("--tenant", None) is None, "missing id must not crash"
    finally:
        sys.argv = _real_argv

    # It runs daily and delivers weekly only while nothing re-introduces a
    # weekday check, which would drop any week whose Sunday the Mac slept
    # through. Matched against calls, not prose, so the comment explaining the
    # decision does not trip the check that enforces it.
    # Tokens are assembled rather than written out, so this check does not match
    # itself. Spelled literally, the assertion is the only thing that fails it.
    src = Path(__file__).read_text()
    day_calls = (".week" + "day()", "iso" + "week" + "day")
    assert not any(c in src for c in day_calls), \
        "gate on the watermark, not the day of the week"
    # A git tail here creates local commits in the user's clone, and updater.py
    # is fast-forward-only and refuses a dirty tree: the first digest delivered
    # on someone else's Mac would silently end their auto-updates forever.
    # Checked against the module namespace rather than the source, because a
    # source scan for these names matches the assertion that scans for them.
    # Neither import is reachable here, so no shell-out is either.
    assert "subprocess" not in globals(), \
        "the digester must not shell out; a git tail here ends auto-updates"
    assert "os" not in globals(), "no os either, for the same reason"
    print("weekly_digester selftest: all assertions passed")


if __name__ == "__main__":
    sys.exit(main())
