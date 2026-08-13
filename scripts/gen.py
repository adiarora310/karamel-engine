#!/usr/bin/env python3
"""Karamel generator: the maker, wired to the gate. Cycle 2 of the pilot build.

Cycle 1 built the critic (the checker) and the refine loop. This is the maker,
and the pipeline that connects them: generate, critique, refine, ship only a
PASS. A flat draft does not just get rejected, it gets rewritten with the
critic's one fix and re-graded, up to a bound, before it ever reaches Adi.

The maker objective is the flat-voice fix: LAND A TAKE, not "add a fact." A draft
that recounts facts with no point of view is an automatic fail at the gate, so
the maker is told to write a verdict, not a summary.

Zero fabrication (Adi's hard rule): the maker never invents a statistic, date, or
quote. Fresh specifics become [VERIFY: what to confirm] slots. Register-aware:
the critic relaxes the quotable and fragmentation rules for bangers, so this can
draft a punchy one-liner without the gate fighting it.

Same $0 plumbing as the rest: reuses critic.call_claude (claude -p, CLAUDE*
stripped, cwd=/tmp). No em-dashes.

CLI:
  --topic "..." [--register banger|analytical] [--rounds N] [--seed "..."]
  --demo        rescue demo: seed a known-flat draft, watch the loop fix it
  --selftest    pure-logic + injected-fake wiring test, no claude
"""
import re
import sys

from pathlib import Path

import critic
import llm
import tenants
from karamel_common import DATA, append_jsonl, now_iso

EM_DASH = re.compile(r"\s*[—–]\s*")
GENERATED = DATA / "generated.jsonl"

REGISTER_NOTE = {
    "banger": ("Banger register: one or two sentences, a specific cultural or business "
               "detail almost no one else clocks, funny because it is true, declarative. "
               "A short fragment or a quotable one-liner is the intended voice."),
    "analytical": ("Analytical register: the cross-domain move. A named fact, then the "
                   "reframe into economics, investing, or founder craft. Tight sentences."),
}


def build_draft_prompt(voice, topic, register, feedback=None, author=None,
                       allow_em_dash=False):
    """The maker's brief.

    It used to state one CLEAN rule ("no throat-clearing") while the critic
    graded against ten, so the maker was marked down on a rubric it had never
    seen and CLEAN became the axis that failed every round. Both sides now read
    the same list from critic.clean_axis(), so they cannot drift apart."""
    who = author or tenants.default_author()
    clean_rules, clean_note = critic.clean_axis(register)
    dash_rule = ("" if allow_em_dash else
                 "No em-dashes or en-dashes. ")
    reg_note = REGISTER_NOTE.get(register, "Pick the register that fits per the voice card.")
    fb = (f"\nThe previous draft was rejected. The single fix to make: {feedback}\n"
          "Rewrite to fix exactly that. Keep what already worked.\n" if feedback else "")
    return f"""You are drafting an original post in {who}'s voice. Their voice card is in your system prompt; obey every rule in it, especially the anti-patterns and the hard do-not lines.

TOPIC OR SEED: {topic}
REGISTER: {register}. {reg_note}

Write ONE original post. The bar: it must LAND A TAKE, a point of view or a verdict, not just recount a fact. "This happened, then this happened" is an automatic fail. Say something only {who} would say.

Zero fabrication: do not invent statistics, dates, dollar figures, or quotes. Use evergreen, widely-known facts, or where a fresh specific belongs write [VERIFY: what to confirm]. A plausible-looking invented number is worse than a VERIFY slot.

DO NOT REUSE THEIR LINES. The voice card quotes their real sentences as the bar. Those are there to show you the texture, not to be borrowed. Lifting one, or lightly rewording one, is the most common way a draft fails: it reads as pastiche of the person rather than as the person. Write new sentences that could sit beside those, never the ones already there.

CLEAN is the axis drafts fail on, and it is graded against this exact list. Avoid every one:
{clean_rules}
{clean_note}
{dash_rule}Open with a fact, a name, or a scene, never a question.
{fb}
Output ONLY the post text, nothing else."""


def draft_post(voice, topic, register, feedback=None, author=None,
               allow_em_dash=False, tenant=None):
    """The maker. One claude call, em-dash scrubbed unless the tenant uses them.
    The scrub was unconditional and silent: it rewrote em dashes to commas on
    the way out, so a tenant whose voice includes them could never receive a
    draft that read like them, and nothing in the output showed why."""
    out = critic.call_claude(
        build_draft_prompt(voice, topic, register, feedback, author=author,
                           allow_em_dash=allow_em_dash),
        system=voice, label="maker", tenant=tenant,
    )
    out = out.strip()
    return out if allow_em_dash else EM_DASH.sub(", ", out).strip()


def generate_gated(voice, topic, register="analytical", max_rounds=2, seed=None,
                   log=False, author=None, generated_path=None,
                   critiques_path=None, allow_em_dash=False, tenant=None):
    """Generate, critique, refine, ship only a PASS. Records the journey so the
    catch-and-rewrite is visible. If seed is given it is the round-0 draft (used
    to demonstrate rescuing a known-flat draft).

    `author` and the two paths are the tenant seam: whose voice this is written
    in and graded against, and where the artefacts land. Defaulting them keeps
    the bare CLI working; heartbeat passes a tenant's."""
    journey = []

    def regenerate(feedback):
        if feedback is None and seed is not None:
            return seed
        return draft_post(voice, topic, register, feedback, author=author,
                          allow_em_dash=allow_em_dash, tenant=tenant)

    def graded(d):
        v = critic.grade(d, voice, register=register, author=author,
                         critiques_path=critiques_path,
                         allow_em_dash=allow_em_dash, tenant=tenant)
        journey.append({
            "round": len(journey), "draft": d, "verdict": v["verdict"],
            "scores": v.get("scores", {}), "why": v.get("why", ""),
            "fix": v.get("fix", ""),
            # Recorded, not just used. A run that fails on CLEAN is unreadable
            # afterwards without the quotes that cost it the points, and the
            # override was never recorded at all, which made an em-dash veto
            # indistinguishable from a low score when reading history back.
            "tells": v.get("tells", []),
            "tells_claimed": v.get("tells_claimed"),
            "override": v.get("override", ""),
            "model_verdict": v.get("model_verdict", ""),
        })
        return v

    final, verdict, rounds = critic.refine(regenerate, graded, max_rounds)
    result = {
        "topic": topic, "register": register, "final": final,
        "verdict": verdict["verdict"], "scores": verdict.get("scores", {}),
        "rounds": rounds, "journey": journey, "generated_at": now_iso(),
    }
    if log:
        append_jsonl(generated_path or GENERATED, result)
    return result


def _print_result(r):
    for j in r["journey"]:
        tag = "PASS" if j["verdict"] == "PASS" else j["verdict"]
        print(f"\n  round {j['round']}  [{tag}]  scores={j['scores']}")
        print(f"    draft: {j['draft']}")
        # The tells are the evidence behind CLEAN, and without them a CLEAN 4
        # is a number nobody can argue with. They are also the exact strings the
        # next round is told to delete, so printing them shows whether the
        # refinement was aimed at anything real.
        tells = j.get("tells") or []
        if tells:
            shown = "; ".join(f'"{q}" ({t})' for q, t in tells[:6])
            print(f"    tells: {shown}")
        claimed = j.get("tells_claimed")
        if claimed is not None and claimed > len(tells):
            print(f"           ({claimed - len(tells)} more claimed but not "
                  f"quotable from the draft, so not counted)")
        if j["verdict"] != "PASS":
            print(f"    why:   {j['why']}")
            print(f"    fix:   {j['fix']}")
    print(f"\n=== FINAL ({r['verdict']}, {r['rounds']} refine round(s)) ===")
    if r["verdict"] == "PASS":
        print(r["final"])
    elif r["verdict"] == "ERROR":
        print("(critic errored, infrastructure fault, not a content judgment)")
    else:
        print("(no PASS within the round budget)")


# ------------------------------- CLI + tests ---------------------------------

def selftest():
    global draft_post

    p0 = build_draft_prompt("VOICE", "the topic", "banger")
    assert "the topic" in p0 and "banger" in p0.lower() and "take" in p0.lower(), p0
    p1 = build_draft_prompt("VOICE", "the topic", "analytical", feedback="add a verdict")
    assert "add a verdict" in p1, p1

    # The author reaches the prompt, and is not the default when one is named.
    # Without this the maker writes every tenant's post in Adi's name while
    # reading their voice card, which produces plausible drafts in the wrong
    # person's voice: the failure is invisible in the output format.
    pa = build_draft_prompt("VOICE", "t", "banger", author="Jane Doe")
    assert "Jane Doe's voice" in pa, pa
    assert "only Jane Doe would say" in pa, pa
    assert "Adi" not in pa, pa
    # The author is resolved, never a compiled-in literal. A hardcoded name
    # meant every copy told the model to write in one stranger's voice.
    assert tenants.default_author() in build_draft_prompt("VOICE", "t", "banger")
    assert "Someone Else" in build_draft_prompt("VOICE", "t", "banger",
                                                author="Someone Else")

    # The maker must see the same CLEAN list the critic grades against. It saw
    # one of ten, so CLEAN failed every round on rules it was never given.
    pc = build_draft_prompt("VOICE", "t", "analytical")
    rules, _ = critic.clean_axis("analytical")
    for line in [r.strip("- ").strip() for r in rules.splitlines() if r.strip()]:
        assert line in pc, f"maker never told about: {line}"
    assert "DO NOT REUSE THEIR LINES" in pc

    # Register-aware, exactly as the critic is: bangers relax two rules.
    pb = build_draft_prompt("VOICE", "t", "banger")
    assert "pull-quote bait" not in pb and "pull-quote bait" in pc

    # And the dash instruction follows the tenant, not the house.
    assert "No em-dashes" in build_draft_prompt("V", "t", "banger")
    assert "No em-dashes" not in build_draft_prompt("V", "t", "banger", allow_em_dash=True)

    # gated wiring with injected fakes, no claude: round 0 flat, refined to a PASS.
    _real_dp, _real_grade = draft_post, critic.grade
    seq = {"n": 0, "authors": [], "paths": []}

    def fake_dp(voice, topic, register, feedback=None, author=None,
                allow_em_dash=False, tenant=None):
        seq["n"] += 1
        seq["authors"].append(author)
        seq.setdefault("tenants", []).append(tenant)
        return "flat draft" if seq["n"] == 1 else "sharp draft"

    def fake_grade(d, voice, context=None, register="auto", log=False,
                   author=None, critiques_path=None, allow_em_dash=False,
                   tenant=None):
        seq["authors"].append(author)
        seq.setdefault("tenants", []).append(tenant)
        seq["paths"].append(critiques_path)
        good = d == "sharp draft"
        return {"verdict": "PASS" if good else "FAIL",
                "scores": {"TAKE": 9 if good else 2},
                "why": "recounts a fact" if not good else "lands a take", "fix": "add a take"}

    draft_post = fake_dp
    critic.grade = fake_grade
    try:
        r = generate_gated("VOICE", "topic", register="analytical", max_rounds=2,
                           author="Jane Doe", critiques_path="/tmp/jane.jsonl")
        assert r["verdict"] == "PASS" and r["final"] == "sharp draft", r
        assert len(r["journey"]) == 2 and r["journey"][0]["verdict"] == "FAIL", r["journey"]
        # Every hop carries the identity: maker and grader alike, every round.
        assert set(seq["authors"]) == {"Jane Doe"}, seq["authors"]
        assert set(seq["paths"]) == {"/tmp/jane.jsonl"}, seq["paths"]

        # The tenant must reach BOTH the maker and the critic, or spend lands
        # in a bucket named "unknown" and `--cost <name>` reports zero against
        # a real bill. That is exactly what happened: not one caller in the
        # tree passed a tenant, so per-tenant accounting never worked at all.
        seq["n"] = 0
        seq["tenants"] = []
        generate_gated("V", "t", max_rounds=0, author="A", tenant="who")
        assert seq["tenants"], "no tenant reached the maker or the critic"
        assert set(seq["tenants"]) == {"who"}, seq["tenants"]
    finally:
        draft_post = _real_dp
        critic.grade = _real_grade

    print("gen selftest: all assertions passed")


def _arg(flag, default=None):
    if flag not in sys.argv:
        return default
    i = sys.argv.index(flag)
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else default


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    card_path = _arg("--card")
    card = Path(card_path).expanduser() if card_path else critic.VOICE_CARD
    if not card.exists():
        print(f"no voice card at {card}", file=sys.stderr)
        return 2
    voice = card.read_text()
    author = _arg("--author")

    if "--demo" in sys.argv:
        # rescue demo: seed the known-flat draft, prove the loop rewrites it to a take.
        print("=== RESCUE DEMO: seed a flat draft, let the gate catch and the maker fix it ===")
        r = generate_gated(
            voice,
            topic="single-supplier risk in aviation supply chains",
            register="banger", max_rounds=2, seed=critic.FLAT)
        _print_result(r)
        return 0

    topic = _arg("--topic")
    if not topic:
        print("usage: gen.py --topic TEXT [--register banger|analytical] "
              "[--rounds N] [--seed TEXT] [--card PATH] [--author NAME] "
              "[--tenant ID] "
              "[--allow-em-dash]  |  --demo  |  --selftest", file=sys.stderr)
        return 2
    register = _arg("--register", "analytical")
    rounds_str = _arg("--rounds", "2")
    if not str(rounds_str).isdigit():
        print(f"error: --rounds must be a non-negative integer, got {rounds_str!r}",
              file=sys.stderr)
        return 2
    rounds = int(rounds_str)
    seed = _arg("--seed")
    try:
        r = generate_gated(voice, topic, register=register, max_rounds=rounds,
                           seed=seed, log=True, author=author,
                           allow_em_dash="--allow-em-dash" in sys.argv,
                           tenant=_arg("--tenant"))
    except llm.LLMError as e:
        # Every one of these is a condition someone can act on: no credit, a bad
        # key, an unreachable API. Printing a traceback for them buries the one
        # sentence that matters under twenty frames of somebody else's library.
        print(f"could not generate: {e}", file=sys.stderr)
        return 2
    _print_result(r)
    return {"PASS": 0, "FAIL": 1}.get(r["verdict"], 2)  # 2 = ERROR (infra fault, not content)


if __name__ == "__main__":
    sys.exit(main())
