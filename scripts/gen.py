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


EDITS_SHOWN = 8


def recent_edits(edits_path, n=EDITS_SHOWN):
    """The last few things this person changed, as lines for a brief.

    The learning signal is their own ongoing behaviour: what they asked a draft
    to become, and what they rewrote before posting. Nothing is read from their
    past activity on X."""
    import json as _json
    from pathlib import Path as _P

    p = _P(edits_path) if edits_path else None
    if not p or not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(_json.loads(line))
        except ValueError:
            continue
    out = []
    for r in rows[-n:]:
        if r.get("kind") == "revision_request" and r.get("request"):
            out.append(f'- asked: "{r["request"][:200]}"')
        elif r.get("kind") == "posted_edit" and r.get("posted"):
            out.append(f'- rewrote "{(r.get("original") or "")[:140]}" as '
                       f'"{r["posted"][:200]}"')
    return out


def learned_block(edits, who):
    if not edits:
        return ""
    return (f"\nLEARNED FROM {who.upper()}'S OWN EDITS. These are their standing "
            "preferences; apply them without being asked again:\n"
            + "\n".join(edits) + "\n")


def revise_draft(voice, draft_text, request, author, tenant=None, max_chars=None,
                 allow_em_dash=False):
    """Rewrite a delivered draft the way its person asked, in a reply.

    Their request outranks the engine's own quotas (presence, feeling, thinking,
    the cooldowns): those shape what the engine volunteers, and this is the
    person telling it. The hard vetoes still run, because a revision is still
    one tap from posting: length, dashes, unfilled blanks, refused constructions.
    Returns the revised text, or None after two attempts that fail them."""
    ceiling = int(max_chars or critic.DEFAULT_MAX_CHARS)
    prompt = (
        f"You wrote this draft post on X for {author}:\n\"\"\"{draft_text}\"\"\"\n\n"
        f"They replied asking for changes:\n\"\"\"{request}\"\"\"\n\n"
        f"Rewrite the post applying exactly what they asked, and nothing else. If "
        f"their reply is itself a rewritten version of the post, use it as the "
        f"base and only tidy it. Keep it in their voice as the card describes. "
        f"Hard limits: at most {ceiling} characters, one clear point, no "
        f"em-dashes or en-dashes, no [VERIFY] placeholders.\n\n"
        f"Output ONLY the post text."
    )
    clean = {a: 10 for a in critic.AXES}
    for _ in range(2):
        try:
            out = critic.call_claude(prompt, system=voice, label="revise", tenant=tenant)
        except Exception:
            return None
        out = out.strip().strip('"').strip()
        if not allow_em_dash:
            out = EM_DASH.sub(", ", out).strip()
        verdict, why = critic.decide(clean, out, allow_em_dash=allow_em_dash,
                                     max_chars=ceiling)
        if verdict == "PASS":
            return out
        prompt += f"\n\nThat attempt was rejected: {why}. Fix exactly that."
    return None


CONTEXT_DAYS = 14


def topic_context(voice, topic, author, tenant=None, lanes=()):
    """Today's facts on the topic and the stance this person would take.

    Khoa, 2026-10-19: a draft about flying taxis and cargo drones "would have
    been better with context he cares about, like being skeptical of Tesla's
    flying-car claims". A topic is not a take. This runs one web-searching call
    before drafting: what happened in the last two weeks, and which side of it
    this person is on, grounded in the stances in their own voice card.

    Returns {"facts": [...], "stance": str, "why": str}, or an empty dict when
    the search or the parse fails. Drafting must never stop because the
    context step did."""
    import json as _json
    from datetime import date

    lane_line = ", ".join(lanes) if lanes else "the lanes in their voice card"
    prompt = (
        f"You are researching {author}'s next post on X. Their voice card is in "
        f"the system prompt.\n\nTOPIC: {topic}\nTHEIR LANES: {lane_line}\n"
        f"TODAY: {date.today().isoformat()}\n\n"
        f"1. Use web search to find what has actually happened on this topic in "
        f"the last {CONTEXT_DAYS} days: announcements, numbers, claims people are "
        f"making. Prefer primary sources.\n"
        f"2. Decide the stance {author} would take on it, from the beliefs and "
        f"skepticisms in their voice card. A stance is a position someone could "
        f"disagree with ('Tesla's flying-car claims are a stock story, not a "
        f"product'), not a topic ('flying cars are interesting'). It must obey "
        f"the card's hard-no lines: if they never criticise named people or "
        f"companies, aim it at the behaviour or the archetype ('a demo flight is "
        f"not a customer'), and use the named company only as the example. One "
        f"sentence, at most 15 words, so it fits in a short post.\n\n"
        f"Return ONLY a JSON object, no prose, no code fence:\n"
        f'{{"facts": [{{"date": "YYYY-MM-DD", "fact": "one sentence", "source": '
        f'"domain"}}], "stance": "one first-person sentence", "why": "which of '
        f'their beliefs it comes from"}}\n'
        f"At most 3 facts, each dated and sourced. If nothing current turns up, "
        f"return an empty facts list and still give the stance."
    )
    try:
        import llm
        out = llm.complete(prompt, system=voice, label="context", tenant=tenant,
                           tools=llm.WEB_TOOLS)
    except Exception:
        return {}
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        data = _json.loads(out[i:j + 1])
    except ValueError:
        return {}
    facts = [f for f in (data.get("facts") or [])
             if isinstance(f, dict) and f.get("fact")][:3]
    stance = str(data.get("stance") or "").strip()
    return {"facts": facts, "stance": stance,
            "why": str(data.get("why") or "").strip()} if (facts or stance) else {}


def context_block(ctx):
    """The context, as the maker and the critic both read it."""
    if not ctx:
        return ""
    lines = []
    if ctx.get("stance"):
        lines.append(f"STANCE TO TAKE: {ctx['stance']}")
    for f in ctx.get("facts") or []:
        lines.append(f"- {f.get('date', '')}: {f['fact']} ({f.get('source', '')})")
    return "\n".join(lines)


def build_draft_prompt(voice, topic, register, feedback=None, author=None,
                       allow_em_dash=False, max_chars=None, banned_motifs=(),
                       owe_thinking=False, banned_stance=(), palette="",
                       require_feeling=True, owe_feeling=False, context=None):
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
    ceiling = int(max_chars or critic.DEFAULT_MAX_CHARS)
    # Measured 2026-10-19: Adi's real posts run a median of 195 characters and
    # his best-performing one is 170; the drafts had drifted to 250-280.
    aim_hi = max(60, ceiling - 20)
    aim_lo = max(40, ceiling - 60)
    # Several patterns share a label (three regexes catch throat-clearing), and
    # the brief listed it three times, which reads as emphasis nobody intended.
    thinking_rule = ("\n\nThe last two posts were closed verdicts, so this one shows its "
                     "working, and that is checked in code. Any one of: say where "
                     "the thought came from (what you read, watched, sat through, "
                     "or heard somebody say), name what you are unsure about, argue "
                     "with a view other people hold, or ask the question you "
                     "actually have. It is allowed to end unresolved."
                     if owe_thinking else "")
    refused = "\n".join(
        f"- {label}" for label in dict.fromkeys(l for _, l in critic.SLOP_PATTERNS)
    )
    # Measured on their own timeline: every banger they write has a person in
    # it, and 2 of our 59 did. See critic.has_presence for the counts.
    if register in critic.PRESENCE_REGISTERS:
        # **No one person's lines in here.** This brief goes to every tenant,
        # and it used to quote Adi's posts as the examples ("my LIC agent looks
        # suspicious to me", "Just watched", "honestly", "smh"): Khoa's first
        # three drafts came back saying "Honestly" and "My guess", which are
        # Adi's hedges, not his. The shapes are described here; each person's
        # own lines are the bar examples in their voice card.
        presence_rule = (
            "PUT YOURSELF IN THE POST, and this one is checked in code. A banger "
            f"is {who} reacting to something, not an encyclopedia entry about it. "
            "It is rejected unless it carries at least one of four shapes: first "
            "person (I, my, me), the moment they met the thing (what they read, "
            "watched, sat through, or found in their inbox), a question they are "
            "actually putting to the reader, or a reaction in the margin. Take the "
            "wording from THEIR bar examples in the voice card, never from another "
            "person's habits. A true, clever observation with nobody in it is "
            "exactly the draft this gate exists to stop.\n\n"
            + ("AND SAY HOW IT LANDED, also checked in code. Being in the post is not "
            "the same as feeling something in it: \"Just paid $7 for a cookie "
            "that will be gone next week\" is a receipt. Before you write, pick "
            "the one feeling they actually had about this (amused, annoyed, "
            "suspicious, embarrassed, impressed, jealous, confused, disappointed, "
            "delighted, nervous, guilty, sold) and let it show the way the voice "
            "card shows they do: a word, a reaction, an admission, or, if the card "
            "allows it, a reaction emoji. A clever comparison is not a feeling; it "
            "is what the machine writes instead of one.\n\n" if require_feeling else "")
            + "Vary the shape. Opening every post the same way or closing every "
            "one with the same emoji or the same hedge turns the fix into a new "
            "tic, and a repeated sign-off or hedge is refused here."
        )
    else:
        presence_rule = (
            "An analytical post is allowed to be impersonal: several of their best "
            "are. But if this is something they saw, read, tried, or sat through, "
            "start there and say so. Nothing is gained by hiding where the thought "
            "came from."
        )
    cooldown = ""
    if banned_motifs:
        names = ", ".join(sorted(banned_motifs)[:12])
        cooldown = (
            "\nALREADY USED IN RECENT POSTS, so they are refused here: "
            f"{names}.\nReach for a different reference. Returning to the same "
            "name every few posts is the clearest sign a machine is writing, and "
            "it is invisible in any single draft.\n"
        )
    if banned_stance:
        cooldown += (
            "\nALREADY USED AS THE HEDGE IN RECENT POSTS, so they are refused "
            "here: " + ", ".join(f'"{w}"' for w in sorted(banned_stance)) +
            ".\nUncertainty is welcome, the same word for it twice running is a "
            "tic. There are plenty: I wonder, my guess, probably, no idea yet, "
            "I can't tell, maybe I have this wrong.\n"
        )
    ctx_text = context_block(context)
    ctx_note = (
        "\nCURRENT CONTEXT, researched today. The post states the stance below "
        "in so many words, in their voice (their own hedge is fine: 'I think' "
        "still leaves the position explicit), and uses one of these facts as "
        "the evidence, not all of them. Never leave the reader to infer the "
        f"stance from facts placed side by side: say it. A reader must be able "
        f"to say in one line what {who} thinks.\n{ctx_text}\n" if ctx_text else
        f"\nTake a clear stance {who} would defend, not just a topic. A reader "
        "must be able to say in one line what they think.\n")
    if palette:
        cooldown += f"\n{palette}\n"
    if owe_feeling:
        cooldown += (
            "\nTHE LAST TWO POSTS CARRIED NO FEELING, so this one must, and that "
            "is checked in code. Deadpan is part of this voice; three in a row "
            "is not. Say how it landed the way their voice card shows they do "
            "(their own words for liking, disliking, being surprised or "
            "unconvinced), in their palette, not a stranger's.\n")
    fb = (f"\nThe previous draft was rejected. The single fix to make: {feedback}\n"
          "Rewrite to fix exactly that. Keep what already worked.\n" if feedback else "")
    return f"""You are drafting an original post in {who}'s voice. Their voice card is in your system prompt; obey every rule in it, especially the anti-patterns and the hard do-not lines.

TOPIC OR SEED: {topic}
REGISTER: {register}. {reg_note}
{ctx_note}

Write ONE original post. The bar: {who} has to be somewhere in the argument. "This happened, then this happened" is an automatic fail. But a verdict is only one way to clear the bar, and leaning on it is what makes these read as machine-written: a question they actually have, a disagreement with something other people believe, a speculation, or a noticing that does not resolve all count, and on the accounts this voice is modelled on (Hobart, Tavel, Lessin) that is how most posts read. Do NOT reach for the three-beat shape of fact, clever reframe, universal law. Say something only {who} would say.{thinking_rule}

Zero fabrication: do not invent statistics, dates, dollar figures, or quotes. Use evergreen, widely-known facts, or where a fresh specific belongs write [VERIFY: what to confirm]. A plausible-looking invented number is worse than a VERIFY slot.

DO NOT REUSE THEIR LINES. The voice card quotes their real sentences as the bar. Those are there to show you the texture, not to be borrowed. Lifting one, or lightly rewording one, is the most common way a draft fails: it reads as pastiche of the person rather than as the person. Write new sentences that could sit beside those, never the ones already there.

CLEAN is the axis drafts fail on, and it is graded against this exact list. Avoid every one:
{clean_rules}
{clean_note}
{dash_rule}Open on a fact, a name, a scene, or the moment you met the thing. A question is allowed when it is a real one you are putting to the reader; never as throat-clearing.

{presence_rule}

Real posts carry fingerprints: contractions, an aside, a hedge, a reaction. Use THEIR fingerprints, as the voice card describes them; prose with none is the tell you are trying to avoid, not the goal.

IT MUST STAND ALONE. A stranger reads it cold, with nothing above it. Name who and what in the sentence that needs them: "my dog" rather than "she", the product rather than "it". A post that only makes sense to somebody who was in the room is rejected.

LENGTH IS A HARD GATE: {ceiling} characters, counted. A draft over it is rejected before anything else is read. Aim for {aim_lo} to {aim_hi}: shorter and tighter beats complete. ONE CLEAR POINT per draft: if you can name two separate points in it, cut one. No setup sentence before the point, no second example when one landed, no closing line that restates what you already said.

These constructions are refused deterministically. A draft containing one is rejected no matter how good it is:
{refused}
{cooldown}{fb}
Output ONLY the post text, nothing else."""


def draft_post(voice, topic, register, feedback=None, author=None,
               allow_em_dash=False, tenant=None, max_chars=None,
               banned_motifs=(), owe_thinking=False, banned_stance=(),
               palette="", require_feeling=True, owe_feeling=False,
               context=None):
    """The maker. One claude call, em-dash scrubbed unless the tenant uses them.
    The scrub was unconditional and silent: it rewrote em dashes to commas on
    the way out, so a tenant whose voice includes them could never receive a
    draft that read like them, and nothing in the output showed why."""
    out = critic.call_claude(
        build_draft_prompt(voice, topic, register, feedback, author=author,
                           allow_em_dash=allow_em_dash, max_chars=max_chars,
                           banned_motifs=banned_motifs,
                           owe_thinking=owe_thinking,
                           banned_stance=banned_stance, palette=palette,
                           require_feeling=require_feeling,
                           owe_feeling=owe_feeling, context=context),
        system=voice, label="maker", tenant=tenant,
    )
    out = out.strip()
    return out if allow_em_dash else EM_DASH.sub(", ", out).strip()


def generate_gated(voice, topic, register="analytical", max_rounds=2, seed=None,
                   log=False, author=None, generated_path=None,
                   critiques_path=None, allow_em_dash=False, tenant=None,
                   max_chars=None, samples_path=None, require_feeling=True,
                   feeling_quota=False, lanes=(), research=True, edits_path=None):
    """Generate, critique, refine, ship only a PASS. Records the journey so the
    catch-and-rewrite is visible. If seed is given it is the round-0 draft (used
    to demonstrate rescuing a known-flat draft).

    `author` and the two paths are the tenant seam: whose voice this is written
    in and graded against, and where the artefacts land. Defaulting them keeps
    the bare CLI working; heartbeat passes a tenant's."""
    journey = []
    # Read once per topic rather than per round: the log does not change while
    # we are drafting against it, and the maker and the grader must be looking
    # at the same cooldown or the maker gets blamed for a rule it never saw.
    # One path, read by both halves. `tenant` arrives as an id string from
    # heartbeat (it goes into the log row), so the attribute fallback is only
    # for a caller that passes the object.
    log_for_rules = generated_path or getattr(tenant, "generated_path", None)
    ceiling = max_chars if max_chars is not None else getattr(tenant, "max_chars", None)
    cooling = critic.topic_exempt(critic.recent_motifs(log_for_rules), topic)
    # Their emotional range, measured by the GoEmotions model from their own
    # writing. Empty when there is no samples file or no model installed.
    if samples_path is None and log_for_rules:
        samples_path = Path(log_for_rules).parent / "samples.jsonl"
    try:
        import emotion
        pal = emotion.palette_line(samples_path, author or "them")
        avoid = emotion.drift_avoid(critic.shipped_finals(log_for_rules),
                                    emotion.palette_for(samples_path))
    except Exception:
        pal, avoid = "", None
    if avoid:
        pal = (pal + f" Their last drafts have leaned {avoid}, which is rare for "
               f"them: this one must not lead {avoid}. Checked in code.").strip()
    stale_hedges = critic.recent_stance_words(log_for_rules)
    owe_feeling = feeling_quota and not critic.recent_feeling(log_for_rules)
    # Once per topic, before any round: every round argues the same stance
    # from the same facts, and the critic grades against them too.
    ctx = (topic_context(voice, topic, author or "them", tenant=tenant, lanes=lanes)
           if research and seed is None else {})
    if edits_path is None and log_for_rules:
        edits_path = Path(log_for_rules).parent / "edits.jsonl"
    learned = learned_block(recent_edits(edits_path), author or "them")
    if learned:
        pal = (pal + "\n" + learned).strip()

    def regenerate(feedback):
        if feedback is None and seed is not None:
            return seed
        return draft_post(voice, topic, register, feedback, author=author,
                          allow_em_dash=allow_em_dash, tenant=tenant,
                          max_chars=ceiling, banned_motifs=cooling,
                          owe_thinking=not critic.recent_public_thinking(log_for_rules),
                          banned_stance=stale_hedges, palette=pal,
                          require_feeling=require_feeling,
                          owe_feeling=owe_feeling, context=ctx)

    def graded(d):
        v = critic.grade(d, voice, register=register, author=author,
                         generated_path=log_for_rules, max_chars=ceiling,
                         banned_motifs=cooling, palette=pal,
                         require_feeling=require_feeling,
                         feeling_quota=feeling_quota, avoid_sentiment=avoid,
                         brief_note=context_block(ctx),
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
        "context": ctx or None,
        "learned_from_edits": bool(learned),
        # Whose voice this was written in. Every other artefact carries it and
        # this one did not, so a row read back off disk could not be attributed
        # to anybody: fine while each person owns a Mac, wrong the moment two
        # share one, and the operator view is exactly where that would surface.
        "tenant": tenant,
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
            # The deterministic veto, printed first and on its own line.
            # Without it the CLI showed a draft scoring 8/8/7/10 with the
            # critic writing "fix: none" and a FAIL verdict, and the actual
            # reason (a motif under cooldown) was nowhere on screen.
            if j.get("override"):
                print(f"    VETO:  {j['override']}")
            print(f"    why:   {j['why']}")
            print(f"    fix:   {j['fix']}")
    print(f"\n=== FINAL ({r['verdict']}, {r['rounds']} refine round(s)) ===")
    if r["verdict"] != "PASS":
        vetoes = [j["override"] for j in r["journey"] if j.get("override")]
        if vetoes and all(v == vetoes[0] for v in vetoes):
            # Every round died on the same deterministic rule: that is a topic
            # the maker cannot win, not a draft it keeps getting wrong.
            print(f"    every round died on the same rule: {vetoes[0]}")
    if r["verdict"] == "PASS":
        print(r["final"])
    elif r["verdict"] == "ERROR":
        print("(critic errored, infrastructure fault, not a content judgment)")
    else:
        print("(no PASS within the round budget)")


# ------------------------------- CLI + tests ---------------------------------

def selftest():
    global draft_post, topic_context
    # Never a real web search from a selftest: on a configured machine that
    # would be a live, slow, billed call on every run.
    _real_ctx = topic_context
    topic_context = lambda *a, **k: {"facts": [], "stance": "test stance", "why": ""}

    p0 = build_draft_prompt("VOICE", "the topic", "banger")
    # The bar used to be the word "take", which the brief no longer uses: it
    # demanded a verdict and that is the shape being fixed.
    assert "the topic" in p0 and "banger" in p0.lower(), p0
    assert "somewhere in the argument" in p0, p0
    assert "three-beat shape" in p0, p0
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
                allow_em_dash=False, tenant=None, max_chars=None,
                banned_motifs=(), owe_thinking=False, banned_stance=(),
                palette="", require_feeling=True, owe_feeling=False,
                context=None):
        seq["n"] += 1
        seq.setdefault("ceilings", []).append(max_chars)
        seq["authors"].append(author)
        seq.setdefault("tenants", []).append(tenant)
        return "flat draft" if seq["n"] == 1 else "sharp draft"

    def fake_grade(d, voice, context=None, register="auto", log=False,
                   author=None, critiques_path=None, allow_em_dash=False,
                   tenant=None, max_chars=None, generated_path=None,
                   banned_motifs=None, palette="", require_feeling=True,
                   feeling_quota=False, avoid_sentiment=None, brief_note=""):
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
        topic_context = _real_ctx

    # The maker reads the same presence rule the critic enforces, per register.
    b = build_draft_prompt("v", "t", "banger")
    a = build_draft_prompt("v", "t", "analytical")
    assert "PUT YOURSELF IN THE POST" in b and "checked in code" in b
    assert "SAY HOW IT LANDED" in b and "SAY HOW IT LANDED" not in a
    deadpan = build_draft_prompt("v", "t", "banger", require_feeling=False)
    assert "PUT YOURSELF IN THE POST" in deadpan and "SAY HOW IT LANDED" not in deadpan
    withctx = build_draft_prompt("v", "t", "analytical", context={
        "facts": [{"date": "2026-10-19", "fact": "Archer flew over SF Bay.", "source": "archer.com"}],
        "stance": "Air taxis are a logistics story first."})
    assert "STANCE TO TAKE: Air taxis are a logistics story first." in withctx
    assert "2026-10-19: Archer flew over SF Bay." in withctx
    assert "Take a clear stance" in build_draft_prompt("v", "t", "analytical")
    import tempfile as _tf, json as _js
    _e = Path(_tf.mkdtemp()) / "edits.jsonl"
    _e.write_text("\n".join(_js.dumps(r) for r in (
        {"kind": "revision_request", "request": "shorter, and less polite"},
        {"kind": "posted_edit", "original": "long version", "posted": "short version"})))
    lines = recent_edits(_e)
    assert lines == ['- asked: "shorter, and less polite"',
                     '- rewrote "long version" as "short version"'], lines
    assert "LEARNED FROM KHOA'S OWN EDITS" in learned_block(lines, "Khoa")
    assert recent_edits(None) == [] and learned_block([], "x") == ""
    owed = build_draft_prompt("v", "t", "analytical", owe_feeling=True)
    assert "CARRIED NO FEELING" in owed and "CARRIED NO FEELING" not in a
    assert "PUT YOURSELF IN THE POST" not in a
    assert "allowed to be impersonal" in a
    assert "never as throat-clearing" in b

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
    # --tenant used to be passed straight through as a string, so every
    # per-tenant rule read off the object (the length ceiling, the motif
    # cooldown, the emoji cooldown) was silently absent from CLI runs and a
    # draft tested here was graded more leniently than the same draft in
    # production. Resolve it to the tenant, or say which ids exist.
    tenant_id = _arg("--tenant")
    tenant = None
    if tenant_id:
        # load_tenant returns None for an unregistered id rather than raising,
        # and None here would mean "no tenant rules" without saying so.
        tenant = tenants.load_tenant(tenant_id)
        if tenant is None:
            known = ", ".join(t.id for t in tenants.list_tenants()) or "none"
            print(f"no such tenant {tenant_id!r} (registered: {known})",
                  file=sys.stderr)
            return 2
    try:
        r = generate_gated(
            voice, topic, register=register, max_rounds=rounds, seed=seed,
            log=True,
            author=author or (tenant.name if tenant else None),
            # The id, not the object: it goes into the log row verbatim and a
            # Tenant is not JSON serializable. The rules travel as their own
            # arguments, which is also how heartbeat calls this.
            tenant=tenant.id if tenant else None,
            generated_path=tenant.generated_path if tenant else None,
            critiques_path=tenant.critiques_path if tenant else None,
            max_chars=tenant.max_chars if tenant else None,
            samples_path=tenant.samples_path if tenant else None,
            require_feeling=tenant.require_feeling if tenant else True,
            feeling_quota=tenant.feeling_quota if tenant else False,
            allow_em_dash=(tenant.allow_em_dash if tenant
                           else "--allow-em-dash" in sys.argv))
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
