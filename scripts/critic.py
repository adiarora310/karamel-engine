#!/usr/bin/env python3
"""Karamel critic: the verifier gate. Cycle 1 of the concierge-pilot build loop.

This is the missing Karpathy block. Until now the drafter (the maker) shipped its
own first-pass output with no gate but an em-dash scrub. The critic (the checker)
is a SEPARATE sub-agent with different, skeptical instructions. The maker never
grades its own homework. A draft ships only if it clears this gate.

It encodes the flat-voice fix diagnosed this session: a draft that states true
facts with no point of view FAILS. The TAKE axis is mandatory. Correct-but-
characterless is exactly what this gate exists to reject.

Two functions matter:
  grade(draft, voice, context) -> verdict     the gate. One claude call.
  refine(regenerate, grade_fn, max_rounds)     generate, critique, refine loop.
                                               The voice-quality mechanism.

Both the drafter pattern (claude -p on the subscription, $0, CLAUDE* env stripped,
cwd=/tmp) and the house rules (no em-dashes; metrics computed in Python, never
model-guessed) are honored. The em-dash verdict is deterministic: a draft with an
em-dash FAILS regardless of what the model says.

CLI:
  --draft "text" [--context "post"]   grade one draft, print the verdict
  --demo                              grade a known-good and a known-flat line
  --selftest                          run the pure-logic unit tests, no claude
"""
import re
import subprocess
import sys

import llm
import tenants

from karamel_common import DATA, ROOT, has_em_dash, now_iso, append_jsonl

VERIFY_RE = re.compile(r"\[VERIFY:\s*([^\]]*)\]", re.IGNORECASE)
VOICE_CARD = ROOT / "03_voice_card.md"
CRITIQUES = DATA / "critiques.jsonl"

# Used only when a caller does not name one. Entry points resolve a tenant and
# pass tenant.name; this keeps the bare CLI (critic.py --demo) working without
# making every library call require a registry lookup. Resolved at call time
# rather than compiled in: as a literal it shipped one person's name to
# everybody who received a copy.

# PASS bar. TAKE is mandatory and high: a draft with no point of view does not ship.
THRESHOLDS = {"VOICE": 7, "TAKE": 7, "SPECIFIC": 6, "CLEAN": 8}
AXES = ("VOICE", "TAKE", "SPECIFIC", "CLEAN")


# CLEAN-axis tells distilled from the stop-slop skill (~/.agents/skills/stop-slop/SKILL.md).
# Universal tells apply to every register. The long-form-only tells are relaxed for the
# banger register, where a quotable one-liner or a short fragment is the intended voice.
STOP_SLOP_UNIVERSAL = [
    "adverb crutches, especially -ly adverbs doing vague work",
    "passive voice, name the actor and make them the subject",
    "Wh-starter rhetorical openers (a sentence opening with which, what, or how as a setup)",
    "throat-clearing (here's what, here's the thing, this is why)",
    "not-X-it's-Y contrasts, state Y directly",
    "meta-joiners (the rest of this, let me explain, worth noting)",
    "vague declaratives (the implications are significant, the reasons are structural)",
    "inanimate things doing human verbs (the decision emerges, the complaint becomes a fix)",
]
STOP_SLOP_LONGFORM_ONLY = [
    "pull-quote bait, a line engineered to sound quotable reads as AI",
    "dramatic fragmentation for effect, vary sentence length instead",
]


def clean_axis(register):
    """The CLEAN-axis rule list and register note, grounded in stop-slop."""
    checks = list(STOP_SLOP_UNIVERSAL)
    if register != "banger":
        checks += STOP_SLOP_LONGFORM_ONLY
    rules = "\n".join(f"    - {c}" for c in checks)
    note = ("Banger register: a quotable one-liner or a short fragment is the intended "
            "voice, not a flaw. Do not dock CLEAN for those."
            if register == "banger"
            else "Long-form or analytical register: reward varied rhythm and full sentences.")
    return rules, note


def build_critique_prompt(voice, draft, context=None, register="auto", author=None):
    """`author` is the name the grader is told it works for. It was the literal
    a single hardcoded name until the registry existed, which meant every tenant's
    drafts were graded against a stranger: the voice card would say one person
    and the instruction would name another. Callers pass tenant.name."""
    who = author or tenants.default_author()
    ctx = (f'It is a reply to this post:\n"""{context}"""'
           if context else "It is an original post (no reply context).")
    clean_rules, clean_note = clean_axis(register)
    return f"""You are the strictest editor {who} has. Your only job is to catch drafts that do not sound like them, or that fall flat, before they ever reach them. You did not write this draft. Be skeptical. Default to FAIL if it is generic, flat, or could have been written by anyone.

Their voice card is in your system prompt. Grade against it.
{ctx}
Register for this draft: {register}. {clean_note}

DRAFT TO GRADE:
\"\"\"{draft}\"\"\"

ON [VERIFY: ...] SLOTS. The maker is forbidden from inventing a statistic, date, or quote, and is instructed to write [VERIFY: what to confirm] where a fresh specific belongs. A slot is that rule working, not a hedge. Grade the draft as if each slot will be filled with a real, correct specific before it is posted, because a human fills it. Do NOT dock SPECIFIC or VOICE merely for the presence of a slot.
Two things still fail. A draft whose central claim IS the slot, so that nothing survives if the number turns out unremarkable, is weak: dock TAKE. And a draft that is vague everywhere OTHER than its slots is vague: grade that normally.

Score each axis 1 to 10:
- VOICE: matches the card's texture, register, openers, and rules.
- TAKE: lands a point of view, an angle, or a verdict. A draft that recounts true facts with no angle scores LOW here. This is the most important axis. "This happened, then this happened" is a fail.
- SPECIFIC: contains at least one concrete number, name, or exact detail.

CLEANLINESS IS NOT SCORED BY YOU. You report evidence and the score is computed from it. List every AI tell you find, and for each one QUOTE THE OFFENDING TEXT VERBATIM from the draft. A tell you cannot quote does not count and will be discarded, so do not report an impression. Quote the shortest span that contains the problem.
The tells to look for:
{clean_rules}
    - em-dashes or en-dashes (also enforced deterministically)

Report them on the TELLS line as: "exact quote" (name of tell); "exact quote" (name of tell)
Write TELLS: none if the draft is clean. Reporting nothing when the draft is clean is the correct answer, not a failure to look.

Output EXACTLY these seven lines and nothing else:
VOICE: <1-10>
TAKE: <1-10>
SPECIFIC: <1-10>
TELLS: <none, or the quoted list described above>
VERDICT: PASS or FAIL
WHY: <one line, the single most important reason for the verdict>
FIX: <if FAIL, the one change that would most improve it; if PASS, write none>"""


def call_claude(prompt, system=None, tenant=None, label="", effort=None):
    """One model call. Named for its history; it no longer shells out.

    `system` carries the voice card and nothing else. Keeping it to the card
    alone means the maker and the critic send byte-identical system text and
    therefore share ONE cache entry: the card is written once per hour and read
    at about a tenth the price by every call after it. Putting each role's
    instructions in here instead would split that into two entries and halve
    the saving, which is why the roles live in the user prompt."""
    return llm.complete(prompt, system=system, tenant=tenant, label=label,
                        effort=effort)


# A reported tell: "the exact offending text" (name of the tell).
TELL_RE = re.compile(r'["“]([^"“”]{2,})["”]\s*\(([^)]{2,})\)')
_NO_TELLS = {"", "none", "n/a", "na", "-", "nothing", "no tells", "clean"}


def _norm(s):
    """Whitespace- and case-insensitive form, for comparing a quote to a draft.
    A model that re-wraps a quoted line across a newline is still quoting it."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def parse_tells(out):
    """The (quote, tell) pairs the critic claims to have found, unverified.

    Returns None, distinct from [], when the response carries no TELLS line at
    all. Empty means the grader looked and found nothing; None means we never
    got an answer, and those must not be scored the same way."""
    m = re.search(
        r"^TELLS:\s*(.*?)(?=^(?:VERDICT|WHY|FIX|VOICE|TAKE|SPECIFIC|CLEAN):|\Z)",
        out or "", re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    body = m.group(1).strip()
    if _norm(body).rstrip(".") in _NO_TELLS:
        return []
    return [(q.strip(), t.strip()) for q, t in TELL_RE.findall(body)]


def verified_tells(draft, tells):
    """Only the tells whose quote actually appears in the draft.

    This is the whole point of quoting. A grader that docks for a tell it cannot
    point at is docking on an impression, and an impression is exactly what put
    CLEAN at 6 on twelve consecutive rounds while the same grader wrote
    FIX: none. Unquotable claims are dropped rather than argued with."""
    hay = _norm(draft)
    return [(q, t) for q, t in (tells or []) if _norm(q) and _norm(q) in hay]


def clean_score(draft, tells, model_clean=0):
    """CLEAN, computed rather than felt. Each verified tell costs 2 points.

    Ten is a clean draft, eight survives the bar with one blemish, two tells
    fail. When the grader gave us no TELLS line we cannot compute anything, so
    we fall back to whatever it said and then to 0: a malformed response must
    not read as a perfect score."""
    if tells is None:
        return int(model_clean or 0)
    return max(1, 10 - 2 * len(verified_tells(draft, tells)))


def parse_verdict(out, draft=None):
    """Pure. Parse the seven-line critic response into a verdict dict.

    CLEAN is derived from the quoted tells when the draft is supplied. Callers
    that only have the text (the tests, the CLI) still get the model's own
    number, so this stays usable without a draft in hand."""
    scores = {}
    for axis in AXES:
        m = re.search(rf"^{axis}:\s*(\d+)", out, re.MULTILINE)
        scores[axis] = int(m.group(1)) if m else 0
    tells = parse_tells(out)
    verified = verified_tells(draft, tells) if draft is not None else []
    if draft is not None:
        scores["CLEAN"] = clean_score(draft, tells, model_clean=scores.get("CLEAN", 0))
    vm = re.search(r"^VERDICT:\s*(PASS|FAIL)", out, re.MULTILINE | re.IGNORECASE)
    why = re.search(r"^WHY:\s*(.+)$", out, re.MULTILINE)
    fix = re.search(r"^FIX:\s*(.+)$", out, re.MULTILINE)
    model_verdict = (vm.group(1).upper() if vm else "FAIL")
    return {
        "scores": scores,
        "model_verdict": model_verdict,
        "tells": verified,
        "tells_claimed": len(tells) if tells is not None else None,
        "why": (why.group(1).strip() if why else ""),
        "fix": (fix.group(1).strip() if fix else ""),
    }


def verify_slots(draft):
    """The [VERIFY: ...] asks in a draft, in order. Empty list if none."""
    return [m.strip() for m in VERIFY_RE.findall(draft or "")]


def decide(scores, draft, allow_em_dash=False):
    """Pure. The gate decision, computed in Python not trusted to the model.
    Every axis must clear its threshold. Returns (verdict, override_or_empty).

    The em-dash veto is per-tenant, not universal. It is Adi's rule and the
    highest-precision AI tell in his card, so it stays on by default, but a
    tenant whose own prose uses em dashes would otherwise have every draft that
    sounds like them rejected here before the model's verdict was even read."""
    if not allow_em_dash and has_em_dash(draft):
        return "FAIL", "em-dash present (tenant rule, deterministic)"
    below = [a for a in AXES if scores.get(a, 0) < THRESHOLDS[a]]
    if below:
        return "FAIL", f"below bar on {', '.join(below)}"
    return "PASS", ""


def grade(draft, voice, context=None, register="auto", log=False, author=None,
          critiques_path=None, allow_em_dash=False, tenant=None,
          effort=None):
    """The gate. One claude call, then a deterministic Python decision. On a
    critic-call failure returns verdict ERROR (never PASS), so an infrastructure
    fault is never read as a clean content FAIL or PASS.

    `author` names whose editor this is; `critiques_path` is where the log goes.
    Both default to the legacy single-tenant values so existing callers and the
    CLI keep working; heartbeat passes the tenant's."""
    log_path = critiques_path or CRITIQUES
    try:
        out = call_claude(
            build_critique_prompt(voice, draft, context, register, author=author),
            system=voice, label="critic", tenant=tenant, effort=effort,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        result = {
            "verdict": "ERROR", "scores": {}, "why": f"critic call failed: {e}",
            "fix": "", "override": "", "model_verdict": "ERROR",
            "draft": draft, "graded_at": now_iso(),
        }
        if log:
            append_jsonl(log_path, result)
        return result
    parsed = parse_verdict(out, draft=draft)
    verdict, override = decide(parsed["scores"], draft, allow_em_dash=allow_em_dash)
    result = {
        "verdict": verdict,
        "scores": parsed["scores"],
        "why": parsed["why"],
        "fix": parsed["fix"],
        "override": override,
        "model_verdict": parsed["model_verdict"],
        # The evidence behind CLEAN, and how much of it survived checking. When
        # these disagree loudly (claimed 4, verified 1) the grader is guessing,
        # and that is worth seeing rather than averaging away.
        "tells": parsed["tells"],
        "tells_claimed": parsed["tells_claimed"],
        "draft": draft,
        "graded_at": now_iso(),
    }
    if log:
        append_jsonl(log_path, result)
    return result


def refine(regenerate, grade_fn, max_rounds=2):
    """The generate, critique, refine loop. The voice-quality mechanism.

    regenerate(feedback) -> draft   (feedback is None on the first draft, else the
                                     critic's FIX line for the previous attempt)
    grade_fn(draft) -> verdict dict  (inject grade bound to a voice + context)

    Returns (draft, verdict, rounds_used). Only ships a PASS if one was reached;
    otherwise returns the last attempt with its FAIL verdict, so nothing flat
    slips through claiming success.
    """
    max_rounds = max(0, max_rounds)
    draft = regenerate(None)
    best = None  # (rank, draft, verdict, rounds)
    for rounds in range(max_rounds + 1):
        verdict = grade_fn(draft)
        if verdict["verdict"] == "PASS":
            return draft, verdict, rounds
        rank = _rank(verdict)
        if best is None or rank > best[0]:
            best = (rank, draft, verdict, rounds)
        if rounds == max_rounds:
            # Return the BEST attempt, not the last. Refinement is not monotonic:
            # a round that fixes the named axis can lose ground on another, and
            # returning the final draft threw away a strictly better earlier one.
            # `rounds` still reports attempts made, not which one won, so the
            # caller can tell effort from outcome.
            return best[1], best[2], rounds
        draft = regenerate(_feedback(verdict))


def _rank(verdict):
    """Order two failing attempts. Fewest axes below bar wins; total breaks ties."""
    scores = verdict.get("scores") or {}
    below = sum(1 for a in AXES if scores.get(a, 0) < THRESHOLDS[a])
    return (-below, sum(scores.get(a, 0) for a in AXES))


_NO_FIX = {"", "none", "n/a", "na", "-", "nothing", "no change"}


def _feedback(verdict):
    """What to tell the maker next round.

    The model writes FIX: none whenever IT would have passed the draft, which
    happens often, because decide() can still fail on a score threshold the
    model did not treat as disqualifying. The old code passed that straight
    through, so the maker was instructed to fix "none" and predictably came back
    worse. The failing axes are known deterministically, so say them."""
    parts = []
    fix = (verdict.get("fix") or "").strip()
    if fix.lower().rstrip(".") not in _NO_FIX:
        parts.append(fix)
    # The exact phrases that cost CLEAN points, quoted. "CLEAN scored 6, needs 8"
    # is not an instruction anyone can act on, which is why round 1 so often came
    # back no better than round 0. These are removable strings.
    tells = verdict.get("tells") or []
    if tells:
        listed = "; ".join(f'"{q}" ({t})' for q, t in tells[:6])
        parts.append(f"Remove these exact phrases and rewrite around them: {listed}.")
        # Ban the construction, not only the sentence carrying it. Observed on a
        # real run: three rounds, CLEAN 6 every time, two fresh tells each round
        # and both of them "not-X-it's-Y" again. The maker was obeying exactly
        # what it was told, deleting the quoted sentence and rebuilding the same
        # pattern a paragraph away, so the score never moved. A quoted phrase is
        # evidence of a habit; removing the evidence is not removing the habit.
        kinds = []
        for _, kind in tells[:6]:
            kind = (kind or "").strip()
            if kind and kind not in kinds:
                kinds.append(kind)
        if kinds:
            parts.append(
                "Do not write another instance of these patterns anywhere in "
                "the draft: " + "; ".join(kinds) + ". Deleting the sentence and "
                "rebuilding the same construction elsewhere is the failure "
                "this is catching.")
    scores = verdict.get("scores") or {}
    below = [f"{a} scored {scores.get(a, 0)}, needs {THRESHOLDS[a]}"
             for a in AXES if scores.get(a, 0) < THRESHOLDS[a] and not (a == "CLEAN" and tells)]
    if below:
        parts.append("Below the bar: " + "; ".join(below) + ".")
    why = (verdict.get("why") or "").strip()
    if why and not parts:
        parts.append(why)
    return " ".join(parts) or "Sharpen it: land a clearer verdict."


# ----------------------------- CLI + tests -----------------------------------

GOOD = ("the known-good line's $19 smoothie has higher margin than any item at Whole Foods. "
        "Status-pricing in groceries is just getting started.")
FLAT = ("the known-good line sells a smoothie for $19. It is one of the most expensive "
        "smoothies in Los Angeles, and the store has many locations.")


def selftest():
    global call_claude
    ok = True

    pass_out = ("VOICE: 9\nTAKE: 9\nSPECIFIC: 9\nCLEAN: 9\n"
                "VERDICT: PASS\nWHY: sharp take with a number\nFIX: none")
    p = parse_verdict(pass_out)
    assert p["scores"] == {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 9}, p
    assert decide(p["scores"], GOOD) == ("PASS", ""), decide(p["scores"], GOOD)

    flat_out = ("VOICE: 6\nTAKE: 3\nSPECIFIC: 7\nCLEAN: 8\n"
                "VERDICT: FAIL\nWHY: recounts facts, no angle\nFIX: add a verdict")
    f = parse_verdict(flat_out)
    v, why = decide(f["scores"], FLAT)
    assert v == "FAIL" and "TAKE" in why, (v, why)

    # deterministic em-dash override: model says PASS, Python overrules.
    dash_draft = "A real point, sharply made — and it lands."
    v2, why2 = decide({"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 9}, dash_draft)
    assert v2 == "FAIL" and "em-dash" in why2, (v2, why2)

    # refine loop: first attempt fails, second passes, injected fakes, no claude.
    attempts = ["flat one", "sharp one"]
    calls = {"n": 0}

    def fake_regen(feedback):
        d = attempts[calls["n"]]
        calls["n"] += 1
        return d

    def fake_grade(d):
        return {"verdict": "PASS" if d == "sharp one" else "FAIL",
                "fix": "add a take"}

    draft, verdict, rounds = refine(fake_regen, fake_grade, max_rounds=2)
    assert draft == "sharp one" and verdict["verdict"] == "PASS" and rounds == 1, (
        draft, verdict, rounds)

    # refine loop exhausts and returns a FAIL, never a false PASS.
    def always_flat(feedback):
        return "still flat"

    def always_fail(d):
        return {"verdict": "FAIL", "fix": "x"}

    d3, v3, r3 = refine(always_flat, always_fail, max_rounds=2)
    assert v3["verdict"] == "FAIL" and r3 == 2, (v3, r3)

    # On exhaustion it returns the BEST attempt, not the last. Refinement is not
    # monotonic: observed live, a round scoring 8/9/8/6 was discarded in favour
    # of a later 5/8/6/3 purely because that one came last.
    seq = ["weak", "strong", "weak again"]
    def cycle(feedback):
        return seq.pop(0)
    def by_text(d):
        good = d == "strong"
        return {"verdict": "FAIL", "fix": "x",
                "scores": {"VOICE": 9 if good else 2, "TAKE": 9 if good else 2,
                           "SPECIFIC": 9 if good else 2, "CLEAN": 9 if good else 2}}
    d4, v4, r4 = refine(cycle, by_text, max_rounds=2)
    assert d4 == "strong", f"returned {d4!r}, should keep the best attempt"
    assert r4 == 2, "rounds still reports attempts made, not which one won"

    # FIX: none must not become the instruction. The model writes it whenever it
    # would have passed the draft, and the maker was being told to fix "none".
    got = []
    def record(feedback):
        got.append(feedback)
        return "d"
    def none_fix(d):
        return {"verdict": "FAIL", "fix": "none", "why": "",
                "scores": {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 4}}
    refine(record, none_fix, max_rounds=1)
    assert got[0] is None, got                      # first call is the initial draft
    assert "none" != (got[1] or "").strip().lower(), got
    assert "CLEAN scored 4" in got[1], got[1]       # says the axis that actually failed
    assert "needs 8" in got[1], got[1]

    # grade returns ERROR, never PASS, when the critic call itself fails.
    _real_cc = call_claude

    def _boom(prompt, system=None, tenant=None, label=None, effort=None):
        raise RuntimeError("simulated critic failure")

    call_claude = _boom
    try:
        er = grade("anything", "voice card text")
        assert er["verdict"] == "ERROR" and er["scores"] == {}, er
    finally:
        call_claude = _real_cc

    # refine tolerates a negative max_rounds (guarded to 0), no crash, no false PASS.
    d4, v4, r4 = refine(lambda f: "x", lambda d: {"verdict": "FAIL", "fix": ""},
                        max_rounds=-1)
    assert r4 == 0 and v4["verdict"] == "FAIL", (v4, r4)

    # The em-dash veto is a tenant rule, not a universal one. Adi keeps it; a
    # tenant whose own prose uses em dashes must not have every characteristic
    # draft vetoed here before the model's scores are even considered.
    passing = {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 9}
    dashed = "the point, and this is the point, matters"
    assert decide(passing, dashed)[0] == "PASS", "control: no dash, should pass"
    real_dash = "the point \u2014 and this is the point \u2014 matters"
    assert decide(passing, real_dash)[0] == "FAIL", "default must still veto"
    assert "tenant rule" in decide(passing, real_dash)[1]
    assert decide(passing, real_dash, allow_em_dash=True)[0] == "PASS", \
        "a tenant who uses em dashes must not be vetoed for it"
    # Allowing em dashes does not lower any other bar.
    assert decide({"VOICE": 2, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 9},
                  real_dash, allow_em_dash=True)[0] == "FAIL"

    # ---- CLEAN is computed from quoted evidence, not felt --------------------
    # The defect this replaces, measured: across 12 recorded rounds for a real
    # tenant, CLEAN came back 3, 4, 5 or 6 and never once reached its bar of 8,
    # twice alongside the grader's own FIX: none. It was the sole axis below bar
    # on the best attempt of every run, so nothing could ever ship.
    d = 'Here is the thing about ports. The latency dropped significantly.'
    out_t = ('VOICE: 9\nTAKE: 9\nSPECIFIC: 9\n'
             'TELLS: "Here is the thing" (throat-clearing); "significantly" (adverb crutch)\n'
             'VERDICT: FAIL\nWHY: two tells\nFIX: cut them')
    pv = parse_verdict(out_t, draft=d)
    assert len(pv["tells"]) == 2, pv["tells"]
    assert pv["scores"]["CLEAN"] == 6, pv["scores"]          # 10 - 2*2
    assert decide(pv["scores"], d)[0] == "FAIL"

    # One blemish survives. The bar is 8, so a single quibble must not be able to
    # block a draft forever, which is precisely what used to happen.
    one = 'The latency dropped significantly after we moved the port.'
    out_1 = ('VOICE: 9\nTAKE: 9\nSPECIFIC: 9\n'
             'TELLS: "significantly" (adverb crutch)\n'
             'VERDICT: PASS\nWHY: fine\nFIX: none')
    p1 = parse_verdict(out_1, draft=one)
    assert p1["scores"]["CLEAN"] == 8, p1["scores"]
    assert decide(p1["scores"], one) == ("PASS", ""), decide(p1["scores"], one)

    # A clean draft scores 10. "Found nothing" is an answer, not a failure to look.
    out_0 = ('VOICE: 9\nTAKE: 9\nSPECIFIC: 9\nTELLS: none\n'
             'VERDICT: PASS\nWHY: clean\nFIX: none')
    assert parse_verdict(out_0, draft=one)["scores"]["CLEAN"] == 10

    # A tell the grader cannot quote from the draft is discarded. This is the
    # anti-vibe check: an impression costs nothing, evidence costs 2 points.
    out_ghost = ('VOICE: 9\nTAKE: 9\nSPECIFIC: 9\n'
                 'TELLS: "a phrase that is simply not present" (throat-clearing)\n'
                 'VERDICT: FAIL\nWHY: x\nFIX: y')
    pg = parse_verdict(out_ghost, draft=one)
    assert pg["tells"] == [], pg["tells"]
    assert pg["scores"]["CLEAN"] == 10, pg["scores"]
    assert pg["tells_claimed"] == 1, pg               # the claim is still recorded

    # Fail closed on a malformed response: no TELLS line at all must not read as
    # a perfect score. Falls back to the model's own number, then to 0.
    out_old = ('VOICE: 9\nTAKE: 9\nSPECIFIC: 9\nCLEAN: 5\n'
               'VERDICT: PASS\nWHY: x\nFIX: none')
    po = parse_verdict(out_old, draft=one)
    assert po["tells_claimed"] is None, po
    assert po["scores"]["CLEAN"] == 5, po["scores"]
    garbage = parse_verdict("total nonsense", draft=one)
    assert garbage["scores"]["CLEAN"] == 0, garbage["scores"]
    assert decide(garbage["scores"], one)[0] == "FAIL"

    # Quoting across a line break still matches: models re-wrap what they quote.
    wrapped = "we moved the port\nand the latency dropped"
    assert verified_tells(wrapped, [("the port and the latency", "x")])

    # Curly quotes, because a model that has been told to quote will use them.
    assert len(parse_tells('TELLS: “significantly” (adverb)\n')) == 1

    # The feedback the maker receives names removable strings, not a score.
    fb = _feedback({"verdict": "FAIL", "fix": "none", "why": "",
                    "scores": {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 6},
                    "tells": [("Here is the thing", "throat-clearing")]})
    assert "Here is the thing" in fb, fb
    assert "CLEAN scored" not in fb, fb   # the quote replaces the useless number
    # And the pattern by name, not just the sentence carrying it. Without this
    # the maker deletes the quoted line and writes the same construction two
    # sentences later, which is what three consecutive CLEAN 6 rounds looked
    # like on a real run.
    assert "throat-clearing" in fb, fb

    # One prohibition per distinct pattern, however many phrases carried it.
    fb2 = _feedback({"verdict": "FAIL", "fix": "none", "why": "",
                     "scores": {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 6},
                     "tells": [("not a product, a service", "not-X-it's-Y"),
                               ("not taste, survival", "not-X-it's-Y")]})
    assert fb2.count("not-X-it's-Y") == 3, fb2   # two quoted, named once

    # main()'s argument handling, which no selftest here ever touched. A
    # two-argument _arg call passed pyflakes and the whole suite, then raised
    # TypeError the first time somebody ran the command.
    _real_argv = sys.argv
    try:
        for argv, expect in (
            (["x", "--register", "banger"], "banger"),
            (["x"], "analytical"),
            (["x", "--register"], "analytical"),      # flag with nothing after it
        ):
            sys.argv = argv
            assert _arg("--register", "analytical") == expect, argv
        sys.argv = ["x", "--draft", "hello"]
        assert _arg("--draft") == "hello"
        assert _arg("--absent") is None
    finally:
        sys.argv = _real_argv

    # VERIFY slots are extracted, not guessed at.
    assert verify_slots("no slots here") == []
    assert verify_slots("a [VERIFY: the 2025 figure] b") == ["the 2025 figure"]
    two = "[VERIFY: seat count] and [VERIFY: the date]"
    assert verify_slots(two) == ["seat count", "the date"]
    assert verify_slots("[verify: lowercase works]") == ["lowercase works"]
    assert verify_slots(None) == []

    print("selftest: all assertions passed")
    return ok


def _arg(flag, default=None):
    """Value after a CLI flag, or `default` if the flag is missing or trailing.

    gen.py's version has always taken a default and this one did not, so a
    two-argument call parsed fine, passed pyflakes, passed the selftest, and
    raised TypeError the first time a human ran the command. Selftests here
    cover the pure logic, never main()'s argument handling."""
    if flag not in sys.argv:
        return default
    i = sys.argv.index(flag)
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else default


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    voice = VOICE_CARD.read_text()

    if "--demo" in sys.argv:
        for label, d in (("KNOWN-GOOD (real Adi line)", GOOD),
                         ("KNOWN-FLAT (fact, no take)", FLAT)):
            r = grade(d, voice, register="banger")
            print(f"\n=== {label} ===\n{d}")
            print(f"  scores: {r['scores']}  ->  {r['verdict']}"
                  + (f"  [{r['override']}]" if r['override'] else ""))
            print(f"  why: {r['why']}")
            if r["verdict"] == "FAIL":
                print(f"  fix: {r['fix']}")
        return 0

    if "--compare-effort" in sys.argv:
        draft = _arg("--compare-effort")
        if draft is None:
            print("error: --compare-effort needs a draft", file=sys.stderr)
            return 2
        # The same bytes, graded twice. Comparing two different drafts graded at
        # two different settings tells you nothing: the texts differ, so any gap
        # is attributable to either. This is the only version of the question
        # that has an answer.
        print("grading the SAME draft at each effort level\n")
        for eff in ("high", "medium", "low"):
            r = grade(draft, voice, register=_arg("--register", "analytical"),
                      effort=eff)
            tells = r.get("tells") or []
            print(f"  {eff:7} {r['verdict']:4}  scores={r['scores']}")
            for q, t in tells:
                print(f"          tell: \"{q}\" ({t})")
            claimed = r.get("tells_claimed")
            if claimed is not None and claimed > len(tells):
                print(f"          ({claimed - len(tells)} claimed but unquotable)")
        print("\nIf lower effort finds fewer REAL tells, it is not cheaper, it "
              "is more lenient,\nand a gate that passes more is not a gate that "
              "works better.")
        return 0

    if "--draft" in sys.argv:
        draft = _arg("--draft")
        if draft is None:
            print("error: --draft needs a value", file=sys.stderr)
            return 2
        r = grade(draft, voice, _arg("--context"), log=True)
        print(f"{r['verdict']}  scores={r['scores']}")
        print(f"why: {r['why']}")
        if r["verdict"] == "FAIL":
            print(f"fix: {r['fix']}")
        return {"PASS": 0, "FAIL": 1}.get(r["verdict"], 2)  # 2 signals ERROR

    print("usage: critic.py [--selftest | --demo | --draft TEXT [--context TEXT]]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
