#!/usr/bin/env python3
"""Karamel heartbeat: the daily original-content cycle. Cycle 4 of the pilot.

Closes the user-zero loop. Each run: pull topics, draft each through the gated
maker (gen.py), keep only drafts that clear the critic gate, deliver them to
Adi's approval channel. He posts, edits, or skips; his edits feed the reflector
so the voice card compounds. Original-first (safety.py): no scraping, no replies
here, nothing reaches an audience without Adi.

This is the Automation block of the Karpathy five, the heartbeat. On the host it
is scheduled by launchd; each run resumes from the topic queue and the draft log,
never from zero.

Delivery goes to the tenant's configured channel (channels.py), not to whatever
this machine happens to have creds for. Records to the tenant's
original_drafts.jsonl using the same status vocabulary the reflector reads
(pending / posted_clean / posted_edited / skipped), so the learn loop closes
without touching the live reply pipeline in drafts.jsonl.

This is the entry point, so this is where a tenant is resolved. gen.py and
critic.py stay plain libraries taking a name and paths: pushing the registry
down into them would make every unit test need a tenant on disk.

CLI:
  --tenant ID   whose voice, whose channel, whose files (default adi)
  --all         run every enabled tenant in turn
  --limit N     max drafts this run per tenant (default: the tenant's setting)
  --force       run even if paused
  --print       dry run: generate and show delivery, do not send or record
  --selftest    pure-logic tests, no claude, no delivery
"""
import sys
import time

import channels
import critic
import gen
import llm
import tenants
from karamel_common import append_jsonl, is_paused, now_iso, read_jsonl
from shared import post_url

# Seeds are evergreen and voice-neutral by design: they exist so the loop always
# has something to chew on, not to be good posts. A tenant with real topics in
# their queue never reaches them.

# Evergreen dogfood seeds, used when the queue is empty so the loop always runs.
LEGACY_SEED_TOPICS = [
    {"topic": "Dieter Rams 'less but better' as an operating principle for founders",
     "register": "analytical"},
    {"topic": "why most founder LinkedIn posts read like a press release",
     "register": "analytical"},
    {"topic": "the Lakers roster math as a lesson in constraint",
     "register": "banger"},
]


MAX_TOPIC_ATTEMPTS = 2


def next_topics(limit, topic_queue, original_drafts, seeds=(), generated=None):
    """Topics to draft this run: queue entries first, then seeds, skipping any
    topic already drafted, and any topic that has failed the gate too often.

    The paths are arguments rather than module constants: dedup must be against
    THIS tenant's history. Reading a shared draft log would let one tenant's
    drafted topic silently suppress another's.

    Retiring failures is not an optimisation. original_drafts is only appended
    after a draft is delivered, so a topic that fails the critic left no trace
    and was picked again on the very next run, and the one after that. One hard
    topic at the head of the queue therefore blocked every topic behind it,
    twice a day, indefinitely: the machine looked healthy, burned two model
    calls a run, and delivered nothing. Attempts are counted from the generated
    log, which records failures with their topic, so a topic gets a second try
    (scores move between runs) and is then set aside for the ones behind it."""
    done = {(r.get("topic") or "").strip().lower() for r in read_jsonl(original_drafts)}
    if generated is not None:
        attempts = {}
        for r in read_jsonl(generated):
            key = (r.get("topic") or "").strip().lower()
            # Failures only. Counting every row retires a topic that CLEARED the
            # gate and then failed to send: channels.send is unguarded, so an
            # SMTP outage across two runs would discard the draft and the topic
            # with it, permanently, for a reason that has nothing to do with the
            # writing. A row with no verdict at all is not evidence of anything
            # and must not count either.
            if key and r.get("verdict") not in (None, "", "PASS"):
                attempts[key] = attempts.get(key, 0) + 1
        done |= {k for k, n in attempts.items() if n >= MAX_TOPIC_ATTEMPTS}
    pool = (read_jsonl(topic_queue) or []) + list(seeds)
    out, seen = [], set()
    for t in pool:
        topic = (t.get("topic") or "").strip()
        key = topic.lower()
        if not topic or key in done or key in seen:
            continue
        seen.add(key)
        out.append({"topic": topic, "register": t.get("register", "analytical")})
        if len(out) >= limit:
            break
    return out


REFILL_COUNT = 6


def used_topics(tenant, seeds=(), limit=40):
    """Everything already drafted, attempted or queued, newest first.

    Handed to the refill so it does not propose what the queue just retired,
    which would spend a model call to rebuild the exact dead end it is being
    called to escape."""
    seen = []
    for path in (tenant.original_drafts_path, tenant.generated_path,
                 tenant.topic_queue_path):
        for r in read_jsonl(path):
            t = (r.get("topic") or "").strip()
            if t and t not in seen:
                seen.append(t)
    for s in seeds:
        t = (s.get("topic") or "").strip()
        if t and t not in seen:
            seen.append(t)
    return seen[-limit:]


def parse_topics(out):
    """One topic per line, `register: topic`, tolerant of numbering and bullets.

    Register defaults rather than failing: a malformed line that still names a
    subject is worth keeping, because the alternative is an empty queue."""
    topics = []
    for line in (out or "").splitlines():
        line = line.strip().lstrip("-*0123456789. )\t")
        if not line:
            continue
        register, _, rest = line.partition(":")
        register, rest = register.strip().lower(), rest.strip()
        if rest and register in ("analytical", "banger", "personal"):
            topics.append({"topic": rest, "register": register})
        elif len(line) > 12:
            topics.append({"topic": line, "register": "analytical"})
    return topics


def refill_topics(tenant, voice, seeds=(), want=REFILL_COUNT):
    """Write fresh subjects into this tenant's queue. Returns how many.

    Grounded in the voice card, because the point is subjects this person would
    actually choose, and explicitly steered off everything already used."""
    avoid = used_topics(tenant, seeds)
    avoid_block = "\n".join(f"- {t}" for t in avoid) or "- (nothing yet)"
    prompt = (
        f"Here is a writer's voice card, between markers.\n\n"
        f"=== VOICE CARD START ===\n{voice}\n=== VOICE CARD END ===\n\n"
        f"Propose {want} subjects this person would write about on X. Their "
        f"lanes, their obsessions, the arguments they are already having.\n\n"
        f"Do NOT propose any of these, or anything that restates one:\n"
        f"{avoid_block}\n\n"
        f"Return exactly {want} lines, nothing else, each formatted:\n"
        f"register: subject\n"
        f"where register is analytical, banger or personal, and subject is a "
        f"specific angle rather than a category. Not 'AI', but the particular "
        f"claim about AI they would defend."
    )
    try:
        out = llm.complete(prompt, label="topic-refill", tenant=tenant.id)
    except Exception as e:
        print(f"[{tenant.id}] topic refill failed: {e}", file=sys.stderr)
        return 0

    known = {t.lower() for t in avoid}
    fresh = [t for t in parse_topics(out)
             if t["topic"].lower() not in known][:want]
    for t in fresh:
        append_jsonl(tenant.topic_queue_path,
                     {**t, "source": "refill", "added_at": now_iso()})
    return len(fresh)


def format_draft(register, topic, text, scores, when, verifies=()):
    """`when` is the tenant's local time, not the server's. One Mac serves
    people in several zones, and a draft stamped 6am for someone in London is
    a draft they will not trust."""
    head = f"[KARAMEL DRAFT, {register}, {when.strftime('%a %b %d %H:%M %Z')}]\n"
    if verifies:
        # First line, before the draft. A slot buried mid-paragraph gets posted
        # verbatim by someone skimming on a phone, and "[VERIFY: ...]" in a live
        # post is worse than the draft never having been sent.
        asks = "\n".join(f"  {i}. {v}" for i, v in enumerate(verifies, 1))
        head = (f"⚠️ NOT READY TO POST, {len(verifies)} blank(s) to fill first:\n"
                f"{asks}\n\n" + head)
    # Words, not symbols. This started as ✅ ✏️ ❌ from the Telegram era, where a
    # reaction is one tap. On email somebody has to type it, and an emoji picker
    # is a worse ask than a word. Both still parse; the instruction names the
    # easy one.
    #
    # It lives here rather than in a setup document because this is where it is
    # needed: at the moment of replying, months after anyone read the setup.
    # One click to a composer with the text already in it, instead of selecting
    # a paragraph out of a mail client. Copying by hand is where smart quotes,
    # lost line breaks and a trailing "(gate {...})" come from.
    #
    # One link, the twitter.com one, because that is the form that actually
    # opens the signed-in app from Gmail on a phone. See post_url: the x.com
    # spelling is not a registered universal link and lands in Gmail's own
    # browser, which has no X session. A twitter:// scheme line was tried
    # alongside it and removed: not tappable in Gmail, and redundant once the
    # https link opens the app.
    #
    # Deliberately absent when there are blanks to fill. A draft with a
    # [VERIFY: ...] slot is not postable, and handing someone a one-click post
    # button for it is handing them a way to publish a placeholder: the warning
    # at the top says stop, and a button underneath saying go would win.
    open_line = "" if verifies else f"Post it: {post_url(text)}\n\n"

    tail = (f"(gate {scores})\n\n"
            f"Reply to this email with one word:\n"
            f"  posted   you published it as written\n"
            f"  skip     you binned it\n"
            f"or  edited  followed by what you actually posted.\n"
            f"The quoted text below is ignored, so leave it.")
    if verifies:
        tail = (f"(gate {scores})\n\n"
                f"Fill the blank(s) above first, then reply with\n"
                f"  edited   followed by the finished text\n"
                f"so the posted version is what gets recorded.")
    return head + f"Topic: {topic}\n\n{text}\n\n" + open_line + tail


def run_tenant(tenant, limit=None, force=False, dry=False):
    """One tenant's daily cycle. Returns the number of drafts delivered."""
    if not tenant.enabled:
        print(f"[{tenant.id}] disabled, skipping")
        return 0
    if is_paused() and not force:
        print(f"[{tenant.id}] paused or halted, skipping")
        return 0

    card = tenant.voice_card_path
    if not card.exists():
        print(f"[{tenant.id}] no voice card at {card}, skipping", file=sys.stderr)
        return 0
    voice = card.read_text()

    # Seeds are the tenant's own, or the legacy list for the legacy owner only.
    # A new tenant with an empty queue and no seeds draws a blank rather than
    # inheriting someone else's subjects.
    seeds = tenant.seed_topics or (LEGACY_SEED_TOPICS if tenant.is_legacy else [])
    n = tenant.originals_per_day if limit is None else limit
    topics = next_topics(n, tenant.topic_queue_path, tenant.original_drafts_path,
                         seeds=seeds, generated=tenant.generated_path)
    if not topics:
        # Refill, then look again. Nothing in this system has ever written
        # topic_queue.jsonl: heartbeat reads it, tenants.py names it, and no
        # producer exists, so the seed list was the entire supply. Seeds are
        # finite and every run consumes one, so every install eventually
        # reaches this line and goes quiet forever, reporting it to a launchd
        # log nobody reads. Observed on the host: three seeds, two delivered,
        # one retired after failing twice, silent the same afternoon.
        added = refill_topics(tenant, voice, seeds=seeds)
        if added:
            print(f"[{tenant.id}] queue was empty, added {added} topic(s)")
            topics = next_topics(n, tenant.topic_queue_path,
                                 tenant.original_drafts_path, seeds=seeds,
                                 generated=tenant.generated_path)
    if not topics:
        print(f"[{tenant.id}] no fresh topics "
              f"(queue empty, {len(seeds)} seed(s) configured, refill failed)",
              file=sys.stderr)
        return 0

    delivered = 0
    for t in topics:
        r = gen.generate_gated(
            voice, t["topic"], register=t["register"], max_rounds=2,
            author=tenant.name,
            tenant=tenant.id,
            generated_path=tenant.generated_path,
            critiques_path=tenant.critiques_path,
            allow_em_dash=tenant.allow_em_dash,
            log=True,
        )
        if r["verdict"] != "PASS":
            # A bare "SKIP (gate FAIL)" is a dead end. The journey holds the
            # scores, the reason and the one fix for every round, and heartbeat
            # was throwing all of it away, which is exactly the information
            # needed to tune a card that is failing its own gate.
            print(f"[{tenant.id}] SKIP (gate {r['verdict']}) after "
                  f"{r.get('rounds', 0)} round(s): {t['topic']}")
            for j in r.get("journey", []):
                print(f"    round {j['round']}: {j['verdict']} {j.get('scores', {})}")
                if j.get("why"):
                    print(f"      why: {j['why']}")
                if j.get("fix"):
                    print(f"      fix: {j['fix']}")
                print(f"      draft: {(j.get('draft') or '')[:200]}")
            continue
        verifies = critic.verify_slots(r["final"])
        # Minted before the send, not after: it goes in the email subject, and a
        # reply keeps the subject, which is how an inbound path will match it
        # back to this row.
        draft_id = int(time.time() * 1000)
        msg = format_draft(
            t["register"], t["topic"], r["final"], r.get("scores", {}),
            tenant.now(), verifies=verifies,
        )
        flag = "ACTION NEEDED, blanks to fill" if verifies else "draft"
        subject = f"[Karamel] #{draft_id} · {flag} · {t['topic'][:60]}"
        mid = channels.send(tenant, msg, dry=dry, subject=subject)
        delivered += 1
        if dry:
            continue
        append_jsonl(tenant.original_drafts_path, {
            "draft_id": draft_id,
            "tenant": tenant.id,
            "kind": "original",
            "topic": t["topic"],
            "register": t["register"],
            "draft_text": r["final"],
            "scores": r.get("scores", {}),
            "rounds": r.get("rounds", 0),
            "status": "pending",
            # Non-empty means the text is NOT postable as-is. The approval step
            # is what resolves a slot, which is the whole reason the maker is
            # allowed to leave one instead of inventing a number.
            "needs_verify": verifies,
            # Kept under the legacy key so the reflector and poller, which both
            # match on it, keep working. It is a channel message id now, not
            # necessarily Telegram's.
            "telegram_msg_id": mid,
            "drafted_at": now_iso(),
        })
    print(f"[{tenant.id}] done: {delivered} draft(s) delivered")
    return delivered


def run(tenant_ids, limit=None, force=False, dry=False):
    """Run the cycle for each named tenant. One tenant's failure does not stop
    the others: a missing voice card or a dead channel is that customer's
    problem, not an outage for everyone on the box."""
    total, failed = 0, []
    for tid in tenant_ids:
        t = tenants.load_tenant(tid)
        if t is None:
            print(f"no such tenant: {tid}", file=sys.stderr)
            failed.append(tid)
            continue
        try:
            total += run_tenant(t, limit=limit, force=force, dry=dry)
        except Exception as e:
            print(f"[{tid}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(tid)
    print(f"total: {total} draft(s) across {len(tenant_ids) - len(failed)} tenant(s)")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def selftest():
    global read_jsonl
    import pathlib
    from datetime import datetime
    from pathlib import Path

    _real = read_jsonl
    QUEUE_A, DRAFTS_A = Path("/q/a.jsonl"), Path("/d/a.jsonl")
    QUEUE_B, DRAFTS_B = Path("/q/b.jsonl"), Path("/d/b.jsonl")

    def fake_read(path):
        if path == DRAFTS_A:
            return [{"topic": "old topic"}]
        if path == QUEUE_A:
            return [{"topic": "old topic", "register": "analytical"},
                    {"topic": "fresh topic", "register": "banger"}]
        if path == DRAFTS_B:
            return []
        if path == QUEUE_B:
            return [{"topic": "old topic", "register": "analytical"}]
        return []

    read_jsonl = fake_read
    try:
        SEEDS = [{"topic": "a seed topic", "register": "analytical"}]
        keys = [t["topic"] for t in next_topics(5, QUEUE_A, DRAFTS_A, SEEDS)]
        assert "old topic" not in keys, keys          # deduped against the draft log
        assert "fresh topic" in keys, keys
        assert next_topics(5, QUEUE_A, DRAFTS_A, SEEDS)[0]["register"] == "banger"
        assert len(next_topics(1, QUEUE_A, DRAFTS_A, SEEDS)) == 1, "limit respected"

        # A tenant with no queue and no seeds gets nothing, not someone
        # else's subjects. This is the whole point of seeds being per-tenant.
        assert next_topics(5, pathlib.Path("/nope"), DRAFTS_B, ()) == []

        # Dedup is per tenant. B has never drafted "old topic", so A having done
        # so must not suppress it for B. This is the bug that a shared draft log
        # would have caused, and it would have looked like "no fresh topics".
        keys_b = [t["topic"] for t in next_topics(5, QUEUE_B, DRAFTS_B, SEEDS)]
        assert "old topic" in keys_b, keys_b

        # A topic that keeps failing the gate is retired, and the queue moves
        # on. Without this it is picked again every run forever, because
        # original_drafts is only written on delivery: one hard topic at the
        # head of the queue starves every topic behind it, twice a day, and the
        # only visible symptom is drafts quietly not arriving.
        GEN = pathlib.Path("/gen")
        attempts = {"n": 1}

        def fake_read_gen(path):
            if path == GEN:
                return [{"topic": "old topic", "verdict": "FAIL"}] * attempts["n"]
            return fake_read(path)

        read_jsonl = fake_read_gen
        keys_c = [t["topic"] for t in next_topics(5, QUEUE_B, DRAFTS_B, SEEDS,
                                                  generated=GEN)]
        assert "old topic" in keys_c, ("one failure earns a retry", keys_c)

        attempts["n"] = MAX_TOPIC_ATTEMPTS
        keys_d = [t["topic"] for t in next_topics(5, QUEUE_B, DRAFTS_B, SEEDS,
                                                  generated=GEN)]
        assert "old topic" not in keys_d, ("should be retired", keys_d)
        assert "a seed topic" in keys_d, ("queue must advance", keys_d)

        # A topic that CLEARED the gate is never retired by attempt count, no
        # matter how many rows it has. generate_gated logs before the send, and
        # the send is unguarded, so counting passes would discard good writing
        # because the mail server was down twice. It is delivery that retires a
        # topic, via original_drafts, and delivery is the thing that failed.
        def read_passes(path):
            if path == GEN:
                return [{"topic": "old topic", "verdict": "PASS"}] * 5
            return fake_read(path)

        read_jsonl = read_passes
        keys_e = [t["topic"] for t in next_topics(5, QUEUE_B, DRAFTS_B, SEEDS,
                                                  generated=GEN)]
        assert "old topic" in keys_e, ("passes must not retire", keys_e)

        # Nor do rows with no verdict, which are evidence of nothing.
        def read_blank(path):
            if path == GEN:
                return [{"topic": "old topic"}] * 5
            return fake_read(path)

        read_jsonl = read_blank
        keys_f = [t["topic"] for t in next_topics(5, QUEUE_B, DRAFTS_B, SEEDS,
                                                  generated=GEN)]
        assert "old topic" in keys_f, ("verdictless rows must not retire", keys_f)
    finally:
        read_jsonl = _real

    when = datetime(2026, 8, 10, 9, 30)
    m = format_draft("banger", "T", "the post", {"TAKE": 9}, when)
    # The reply instruction is words rather than symbols: on email somebody has
    # to type it, and an emoji picker is a worse ask than a word.
    assert "the post" in m and "T" in m, m
    assert "posted" in m and "skip" in m and "edited" in m, m
    assert "✅" not in m, "emoji instruction is a Telegram-era leftover"
    assert "Aug 10" in m and "09:30" in m, m
    assert "NOT READY" not in m, "a clean draft must not carry the warning"

    # A link that opens the composer with the text already in it. Publishing
    # used to mean selecting a paragraph out of a mail client, which is where
    # smart quotes and lost line breaks come from.
    # twitter.com/intent/tweet, not x.com/intent/post. Verified by hand on a
    # signed-in iPhone: only the twitter.com form is a registered universal
    # link, so only it opens the app instead of a logged-out webview inside
    # Gmail. This assertion exists because the x.com spelling looks more
    # current and was changed to once already, which broke the link.
    assert "twitter.com/intent/tweet" in m, m
    assert "x.com/intent/post" not in m, \
        "the x.com spelling is not a universal link and opens a login wall"
    assert "twitter://" not in m, "the scheme link is not tappable in Gmail"
    assert "the%20post" in m, ("draft text must be url-encoded into the link", m)
    assert "in_reply_to" not in m, "an original is not a reply"
    assert m.index("the post") < m.index("Post it"), \
        "the draft comes before the button, so it is read before it is posted"

    # A draft with blanks says so BEFORE the text, not after it. Someone
    # skimming on a phone posts what they read first.
    v = format_draft("banger", "T", "the [VERIFY: seat count] post",
                     {"TAKE": 9}, when, verifies=["seat count", "the date"])
    assert v.index("NOT READY") < v.index("the [VERIFY"), "warning must precede the draft"
    assert "2 blank(s)" in v and "seat count" in v and "the date" in v, v
    # And it must not offer the one-tap approve that would post it verbatim.
    assert "✅" not in v, "a draft with blanks must not be one-tap postable"
    # Including the composer link. The warning at the top says stop; a button
    # underneath saying go would win, and what gets published is a placeholder.
    assert "intent/post" not in v and "twitter://" not in v, \
        "a draft with unfilled blanks must not be one click from publication"
    assert "edited" in v, v
    assert "✏️" not in v, "emoji instruction is a Telegram-era leftover"

    # Topic refill. Nothing has ever written topic_queue.jsonl, so the seed
    # list was the whole supply and every install went silent once it ran out.
    got = parse_topics(
        "analytical: why cap space is a narrative device\n"
        "2. banger: the roster nobody wanted\n"
        "- personal: what I got wrong about trades\n"
        "\n"
        "a bare line with no register at all\n"
    )
    assert [t["register"] for t in got] == [
        "analytical", "banger", "personal", "analytical"], got
    assert got[1]["topic"] == "the roster nobody wanted", got[1]
    assert got[3]["topic"].startswith("a bare line"), got[3]

    # Short noise is dropped rather than queued as a subject.
    assert parse_topics("ok\n---\n") == [], parse_topics("ok\n---\n")

    # A refill that proposes what the queue just retired rebuilds the dead end
    # it was called to escape, so the exclusion list is not optional.
    class _T:
        id = "t"
        original_drafts_path = pathlib.Path("/od")
        generated_path = pathlib.Path("/gen2")
        topic_queue_path = pathlib.Path("/tq")

    def read_used(path):
        if path == pathlib.Path("/od"):
            return [{"topic": "delivered one"}]
        if path == pathlib.Path("/gen2"):
            return [{"topic": "failed twice"}]
        return []

    read_jsonl = read_used
    try:
        used = used_topics(_T(), seeds=[{"topic": "a seed"}])
        assert "delivered one" in used and "failed twice" in used, used
        assert "a seed" in used, "seeds are used topics too"
    finally:
        read_jsonl = _real

    print("heartbeat selftest: all assertions passed")


def _arg(flag, default=None):
    if flag not in sys.argv:
        return default
    i = sys.argv.index(flag)
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else default


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    limit = None
    if "--limit" in sys.argv:
        limit_str = _arg("--limit")
        if not str(limit_str).isdigit():
            print(f"error: --limit must be a non-negative integer, got {limit_str!r}",
                  file=sys.stderr)
            return 2
        limit = int(limit_str)

    if "--all" in sys.argv:
        ids = [t.id for t in tenants.list_tenants()]
        if not ids:
            print("no enabled tenants registered", file=sys.stderr)
            return 1
    else:
        ids = [_arg("--tenant", tenants.LEGACY_TENANT)]

    return run(ids, limit=limit, force="--force" in sys.argv, dry="--print" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
