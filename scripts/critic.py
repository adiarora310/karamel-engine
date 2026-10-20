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
import pathlib
import re
import subprocess
import sys

import llm
import tenants

from karamel_common import DATA, ROOT, has_em_dash, now_iso, append_jsonl

#: The unfilled-blank marker, in both shapes the maker actually writes.
#:
#: It matched only "[VERIFY: what to confirm]". A draft on 2026-10-17 shipped
#: carrying two bare "[VERIFY]" markers straight to the approval inbox, because
#: the colon is what the pattern keyed on and the maker had dropped it. The
#: whole point of the gate is that the literal marker must never reach somebody
#: one tap from publishing it, so the colon and the description are now optional.
VERIFY_RE = re.compile(r"\[VERIFY(?::\s*([^\]]*))?\]", re.IGNORECASE)
VOICE_CARD = ROOT / "03_voice_card.md"
CRITIQUES = DATA / "critiques.jsonl"

# Used only when a caller does not name one. Entry points resolve a tenant and
# pass tenant.name; this keeps the bare CLI (critic.py --demo) working without
# making every library call require a registry lookup. Resolved at call time
# rather than compiled in: as a literal it shipped one person's name to
# everybody who received a copy.

# PASS bar. TAKE is mandatory and high: a draft with no point of view does not ship.
#
# **Rebalanced 2026-10-17, from 460 graded rounds on the host.** CLEAN at 8 was
# the sole reason for 189 of 317 failures, 60% of everything rejected, while
# VOICE, TAKE and SPECIFIC averaged 7.9, 8.1 and 7.8 and almost never blocked
# anything. CLEAN is computed as 10 minus 2 per quoted tell, so two tells killed
# a draft however good its take was, and the gate most responsible for rejection
# was the one least connected to whether a post lands.
#
# The subjective tell-hunt now costs less, and the constructions that genuinely
# read as machine-written are refused deterministically instead (SLOP_PATTERNS
# below), where a model cannot be talked out of them and cannot invent new ones.
THRESHOLDS = {"VOICE": 6, "TAKE": 7, "SPECIFIC": 6, "CLEAN": 6}
AXES = ("VOICE", "TAKE", "SPECIFIC", "CLEAN")

#: Hard character ceiling for a draft, when the tenant does not set its own.
#:
#: There was no length gate at all until now, which is why the median draft ran
#: to 746 characters and the median across rounds to 1,052. Nothing in the maker
#: or the grader ever mentioned length, so the model wrote essays and the critic
#: graded their phrasing.
#:
#: 280 is the native unit of the platform: it is what a reader sees without
#: pressing "Show more", and it is short enough that a draft must choose one
#: idea. **It is a starting number, not a measured one.** Karamel has never
#: recorded how a post performed, so nothing here is fitted to engagement; a
#: tenant can set `max_chars` and the number should move once real data exists.
DEFAULT_MAX_CHARS = 280

#: Constructions that read as machine-written, refused in Python rather than
#: scored by a model.
#:
#: These were all on the CLEAN rubric already, where they depended on the grader
#: noticing them, quoting them exactly, and spending two of its ten points. Here
#: they are a veto, which is what they deserve: no amount of good take makes
#: "it's not X, it's Y" sound like a person wrote it.
# Closing on a law of the universe ("Every DTC brand hides its spec sheet...").
# 13% of our drafts end this way against 5% of the reference writers': it is how
# a model signals profundity when the thought is finished and the post is not.
# Length matters: Sarah Tavel closes a real post on "Most teams are just
# scratching the surface." (7 words) and that is a plain claim, not a
# pronouncement. Every one of ours is 14 words or longer, so the pattern wants
# at least 11 words after the opener.
UNIVERSAL_CLOSER = (
    r"(?m)(?:^|(?<=[.!?]\s))(?:Every|Most|Any|All|Anyone|Everyone|Nobody|Each)\b"
    r"(?:\s+[^\s.!?]+){11,}[.!?]?\s*$"
)

SLOP_PATTERNS = (
    (UNIVERSAL_CLOSER, "universal-law closer"),
    # Apostrophes come in both shapes because the maker writes curly ones and a
    # person pasting an edit writes straight ones, and a pattern that only
    # catches one of them catches half the drafts.
    (r"(?i)\bit['\u2019]?s not (?:just )?(?:about )?[^.!?]{1,45}, it['\u2019]?s\b",
     "not-X-it's-Y contrast"),
    # The pattern above only knew "It's not X, it's Y". "That's not a bakery,
    # it's Supreme's drop model with frosting" shipped to the inbox on
    # 2026-10-18 because the subject was "That". Any pronoun subject, any
    # negation, comma or full stop between the halves. It also catches one of
    # Lessin's real posts ("That's not a bug. That's the whole story..."): the
    # construction is human once, and a tell the fifth time, and this engine
    # does not know which time it is on.
    (r"(?i)\b(?:it|that|this|these|those|they|he|she)"
     r"(?:['\u2019]s\s+not|\s+is\s+not|\s+isn['\u2019]t|['\u2019]re\s+not|"
     r"\s+are\s+not|\s+aren['\u2019]t|\s+was\s+not|\s+wasn['\u2019]t)"
     r"\s+(?:just\s+)?(?:about\s+)?[^.!?]{1,45}[,.;:]\s*"
     r"(?:it|that|this|they|he|she)(?:['\u2019]s|['\u2019]re|\s+is|\s+are|\s+was)\b",
     "not-X-it's-Y contrast"),
    (r"(?i)\bhere (?:is|['\u2019]s) the thing\b", "throat-clearing opener"),
    (r"(?i)^\s*here['\u2019]?s (?:what|why)\b", "throat-clearing opener"),
    (r"(?i)\bthe thing is,", "throat-clearing opener"),
    (r"(?i)\bthe (?:real|bigger|actual) question is\b", "rhetorical pivot"),
    (r"(?i)\bthat['\u2019]?s the (?:whole |real )?(?:point|difference|story)\.?\s*$",
     "summarising last line"),
    (r"(?i)\b(?:this|that|it) (?:is|was)n['\u2019]?t (?:just )?about\b", "isn't-about setup"),
    (r"(?i)\blet that sink in\b", "engagement-bait closer"),
    (r"(?i)\bin (?:today['\u2019]?s|this) (?:fast[- ]paced |rapidly )?(?:world|landscape|era)\b",
     "essay opener"),
)


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
    # Was "('Unfair.', 'I love it.', 'smh.')... 65% of the author's own posts":
    # Adi's lines and Adi's number, sent to every tenant's critic.
    "dramatic fragmentation for rhythm: runs of noun fragments like 'Speed. Scale. "
    "Trust.' or 'Not a bakery. A drop.' A single short reaction or verdict in the "
    "author's own manner (see their bar examples) is NOT this tell, so do not "
    "dock it",
]


def clean_axis(register, allowed=()):
    """The CLEAN-axis rule list and register note, grounded in stop-slop.
    A rule whose construction this person is allowed is left off their list."""
    keys = {"not-X-it's-Y contrast": "not-X-it's-Y"}
    drop = [keys.get(a, a) for a in allowed]
    checks = [c for c in STOP_SLOP_UNIVERSAL if not any(d in c for d in drop)]
    if register != "banger":
        checks += STOP_SLOP_LONGFORM_ONLY
    rules = "\n".join(f"    - {c}" for c in checks)
    note = ("Banger register: a quotable one-liner or a short fragment is the intended "
            "voice, not a flaw. Do not dock CLEAN for those."
            if register == "banger"
            else "Long-form or analytical register: reward varied rhythm and full sentences.")
    return rules, note


def build_critique_prompt(voice, draft, context=None, register="auto", author=None,
                          palette="", brief_note="", allowed=()):
    """`author` is the name the grader is told it works for. It was the literal
    a single hardcoded name until the registry existed, which meant every tenant's
    drafts were graded against a stranger: the voice card would say one person
    and the instruction would name another. Callers pass tenant.name."""
    who = author or tenants.default_author()
    ctx = (f'It is a reply to this post:\n"""{context}"""'
           if context else "It is an original post (no reply context).")
    clean_rules, clean_note = clean_axis(register, allowed)
    # The person's measured emotional range (emotion.palette_line). Guidance for
    # VOICE, not a veto: seventeen posts is too few to refuse a draft on.
    pal_note = (f"\n{palette} A draft whose feeling is outside that range "
                "scores lower on VOICE." if palette else "")
    # Today's researched facts and the stance the maker was given. The facts
    # are not fabrication; a draft that ignores the stance has no take.
    if brief_note:
        pal_note += ("\nThe maker was given this researched context and stance. "
                     "Facts listed here are sourced, not invented. A draft that "
                     "does not take this stance, or a sharper one, scores low on "
                     f"TAKE:\n{brief_note}")
    return f"""You are the strictest editor {who} has. Your only job is to catch drafts that do not sound like them, or that fall flat, before they ever reach them. You did not write this draft. Be skeptical. Default to FAIL if it is generic, flat, or could have been written by anyone.

Their voice card is in your system prompt. Grade against it.
{ctx}
Register for this draft: {register}. {clean_note}{pal_note}

DRAFT TO GRADE:
\"\"\"{draft}\"\"\"

ON [VERIFY: ...] SLOTS. The maker is forbidden from inventing a statistic, date, or quote, and is instructed to write [VERIFY: what to confirm] where a fresh specific belongs. A slot is that rule working, not a hedge. Grade the draft as if each slot will be filled with a real, correct specific before it is posted, because a human fills it. Do NOT dock SPECIFIC or VOICE merely for the presence of a slot.
Two things still fail. A draft whose central claim IS the slot, so that nothing survives if the number turns out unremarkable, is weak: dock TAKE. And a draft that is vague everywhere OTHER than its slots is vague: grade that normally.

STANDALONE is a yes/no, judged before the axes. A post is read cold by a stranger scrolling, with no prior post for context. It fails if a pronoun names somebody the draft never introduced ("She picks the tennis ball" when no person or animal was named), if it refers to a scene, product or event the reader is assumed to already know, or if the first sentence cannot be understood on its own. This is the most common way a draft that reads fine to its author reads as nonsense to everybody else.

Score each axis 1 to 10:
- VOICE: matches the card's texture, register, openers, and rules.
- TAKE: a clear stance someone could disagree with, stated so a reader can say in one line what the writer thinks; a topic or an observation without a position scores LOW. The writer is somewhere in the argument. A verdict is one way; so is a question they actually have, a disagreement with a view other people hold, a speculation ("I wonder if"), or a noticing that does not resolve. A draft that recounts true facts with no angle scores LOW. "This happened, then this happened" is a fail. So is the opposite failure, and it is the common one here: a closed three-beat ruling (fact, clever reframe, universal law) with nothing unresolved and nobody to disagree with. Measured against the writers this account is modelled on, 55% of their posts show the thinking rather than only the conclusion; do not reward a draft for sounding finished.
- SPECIFIC: contains at least one concrete number, name, or exact detail.
- ONE POINT: a post makes one clear point. If the draft makes two separate points, or spends a sentence setting up before it gets to the point, dock TAKE and say which part to cut.

CLEANLINESS IS NOT SCORED BY YOU. You report evidence and the score is computed from it. List every AI tell you find, and for each one QUOTE THE OFFENDING TEXT VERBATIM from the draft. A tell you cannot quote does not count and will be discarded, so do not report an impression. Quote the shortest span that contains the problem.
The tells to look for:
{clean_rules}
    - em-dashes or en-dashes (also enforced deterministically)

Report them on the TELLS line as: "exact quote" (name of tell); "exact quote" (name of tell)
Write TELLS: none if the draft is clean. Reporting nothing when the draft is clean is the correct answer, not a failure to look.

Output EXACTLY these eight lines and nothing else:
STANDALONE: YES or NO
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
    sa = re.search(r"^STANDALONE:\s*(YES|NO)", out, re.MULTILINE | re.IGNORECASE)
    # Absent reads as yes. A grader that skipped the line has told us nothing,
    # and inventing a veto out of silence would fail every draft the moment the
    # prompt drifts.
    standalone = (sa.group(1).upper() != "NO") if sa else True
    vm = re.search(r"^VERDICT:\s*(PASS|FAIL)", out, re.MULTILINE | re.IGNORECASE)
    why = re.search(r"^WHY:\s*(.+)$", out, re.MULTILINE)
    fix = re.search(r"^FIX:\s*(.+)$", out, re.MULTILINE)
    model_verdict = (vm.group(1).upper() if vm else "FAIL")
    return {
        "scores": scores,
        "model_verdict": model_verdict,
        "tells": verified,
        "tells_claimed": len(tells) if tells is not None else None,
        "standalone": standalone,
        "why": (why.group(1).strip() if why else ""),
        "fix": (fix.group(1).strip() if fix else ""),
    }


def verify_slots(draft):
    """The [VERIFY] asks in a draft, in order. Empty list if none.

    A bare marker has no description, and it is still a blank: it comes back as
    "unnamed blank" rather than as an empty string, so the message the maker
    gets names something."""
    return [(m or "").strip() or "unnamed blank" for m in VERIFY_RE.findall(draft or "")]


#: Capitalised words that start sentences or are grammar rather than reference.
#: Without this list every draft "uses the motif The".
MOTIF_STOPWORDS = frozenset({
    "The", "This", "That", "These", "Those", "They", "There", "Then", "Their",
    "Every", "Most", "Nobody", "Everyone", "Some", "Many", "You", "Your", "It",
    "If", "When", "What", "Which", "Who", "Why", "How", "And", "But", "For",
    "Not", "Now", "One", "Two", "Three", "First", "Last", "Here", "His", "Her",
    "Its", "Our", "We", "Was", "Were", "Are", "Is", "Be", "Been", "Had", "Has",
    "Have", "Will", "Would", "Can", "Could", "Should", "A", "An", "In", "On",
    "At", "By", "To", "Of", "So", "No", "Yes", "After", "Before", "Once",
    # The blank marker is not a reference, and web2 lists "pay" and "hear"
    # but not "paid" or "heard": "Paid $7 for a Crumbl cookie" banked "paid"
    # and vetoed "Paid an airline to choose 14C" in the next run.
    "VERIFY", "Verify", "Paid", "Heard",
})

MOTIF_RE = re.compile(r"\b([A-Z][a-zA-Z]{2,})\b")

#: How many recent drafts a reference is locked out for once it is used.
#:
#: **Measured, not guessed.** Across 143 shipped drafts for one tenant, "Rams"
#: appeared in 60 of them, 42%, with "Dieter" in 9% and "Braun" in 6%. Nearly
#: every second post reached for the same man. Each draft was individually fine
#: and the set read as a machine with one trick, which is the failure a per-draft
#: grader structurally cannot see: it is only visible across a feed.
MOTIF_LOOKBACK = 5

#: References that are part of the persona and therefore the easiest to overuse.
#:
#: The rolling cooldown above treats every proper noun alike, which is right for
#: a story that happened to come up twice. It is far too weak for a signature:
#: "Rams" was in 42% of 143 shipped drafts and "Ring" in 14%, because the voice
#: card named the design lens as the spine of the analytical register and the
#: maker obeyed. A signature that appears every other post stops being a
#: signature and becomes the tell.
#:
#: Locked for SIGNATURE_LOOKBACK drafts rather than banned, because they are
#: genuinely his: the point is a budget, not an exile.
SIGNATURE_MOTIFS = frozenset({"rams", "dieter", "braun", "ring"})
SIGNATURE_LOOKBACK = 20


#: Third-person pronouns that need somebody named before them.
#:
#: **Gendered only, measured against the corpus.** Including they/them/their
#: flagged 17 of 143 shipped drafts, and reading them, most were right: "hardware
#: startups ... they die eight months later" needs no person. Gendered pronouns
#: flag 3 of 143 and still catch the draft this rule was written for. The plural
#: and neuter cases are left to the model's STANDALONE judgment, which is weaker
#: but is not wrong three times in every hundred.
PRONOUNS = re.compile(r"\b(she|he|her|him|hers)\b", re.I)

#: Nouns that can own a pronoun, and they only count when something marks them
#: as a thing in the scene rather than a word in a phrase.
#:
#: The first version matched any of these anywhere, so "hardware user research"
#: supplied an antecedent for "she" and the draft this whole rule exists to
#: catch sailed through. A bare noun inside a compound is not somebody; "my
#: dog", "the founder" and a plural like "fans" are.
ANIMATE_SINGULAR = (
    "dog|puppy|cat|wife|husband|partner|son|daughter|kid|child|baby|mom|mother|"
    "dad|father|friend|founder|ceo|cto|coo|investor|operator|customer|buyer|"
    "user|fan|teammate|player|coach|neighbour|neighbor|barista|owner|client|"
    "student|engineer|guy|woman|man|kid"
)
ANIMATE_PLURAL = (
    "dogs|kids|children|founders|investors|operators|customers|buyers|users|"
    "fans|people|teammates|players|coaches|owners|clients|students|engineers|"
    "women|men|parents|employees"
)
ANIMATE = re.compile(
    r"(?:\b(?:my|our|his|her|their|your|the|a|an|this|that|one|another)\s+"
    r"(?:\w+\s+){0,1}(?:" + ANIMATE_SINGULAR + r")\b)"
    r"|(?:\b(?:" + ANIMATE_PLURAL + r")\b)",
    re.I,
)
FULL_NAME = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")


def orphan_pronoun(draft):
    """The first pronoun with nobody named before it, or None.

    **The deterministic half of the standalone rule.** The model judges this
    well and not reliably: asked twice about the same draft, the grader called
    it NO here and YES during the run that shipped it, which is the failure mode
    of putting a binary veto inside a generation. So the clearest case is caught
    in Python, where the answer does not change between runs, and the model
    keeps the cases a regex cannot see.
    """
    text = draft or ""
    for m in PRONOUNS.finditer(text):
        before = text[:m.start()]
        if ANIMATE.search(before) or FULL_NAME.search(before):
            return None
        return m.group(0)
    return None


APOS = r"['\u2019]"

# --- the human element -------------------------------------------------
#
# Measured, not asserted. Across 155 shipped drafts and 23 posts the two
# humans actually wrote (Adi's own timeline and Lessin's), the one property
# that separates them is whether the author is anywhere in the post:
#
#                       his posts     our drafts
#   first person           43%           36%
#   lived opener           22%            0%     <- never once
#   a question             22%            3%
#   stance word            26%            1%
#   any of the four        74%           37%
#
# Split by register the gap stops being subtle: every banger he writes has a
# person in it, and 2 of our 59 did. A banger is the register where he reacts
# to something, so a banger with nobody reacting is the authorless draft that
# reads as a machine ("Every Netflix sports doc has the GM-stares-at-a-
# whiteboard shot now..."). Analytical posts are allowed to be impersonal:
# three of his best are, and gating them would cost the voice more than it
# buys. Closing-sentence length was the other candidate and it was dropped:
# his closers and ours have the same distribution, so it measures nothing.
FIRST_PERSON = re.compile(
    r"\b(i|i" + APOS + r"m|i" + APOS + r"ve|i" + APOS + r"d|i" + APOS +
    r"ll|my|me|mine)\b", re.I)
LIVED_ENTRY = re.compile(
    r"^(just|spent|watched|read|tried|walked|sat|been|got|ate|bought|"
    r"after \w+ (episodes|weeks|days|years|months|minutes|hours))\b", re.I)
STANCE = re.compile(
    r"\b(honestly|smh|kinda|lol|unfair|i mean|not surprising|turns out|"
    r"somehow|apparently|god damn|whatever it is)\b", re.I)
# Only the registers where he is reacting to something. Analytical stays free.
PRESENCE_REGISTERS = frozenset({"banger"})


FIRST_PLURAL = re.compile(
    r"\b(we|we" + APOS + r"re|we" + APOS + r"ve|we" + APOS + r"ll|us|our|ours)\b",
    re.I)


def has_presence(draft, plural=False):
    """Is the author anywhere in this post?

    Four shapes, all of them his own: he says I/my, he opens on the moment he
    met the thing ("Just watched Toy Story 5 today"), he asks the reader
    something ("Why is everyone acting so dumb?"), or he reacts in the margin
    ("honestly", "smh"). Any one of them is a person. None of them is a
    machine reciting what is true about a subject."""
    text = (draft or "").strip()
    return bool(
        FIRST_PERSON.search(text)
        or (plural and FIRST_PLURAL.search(text))
        or LIVED_ENTRY.match(text)
        or "?" in text
        or STANCE.search(text)
    )


#: The system word list, used to tell a proper noun from an ordinary word that
#: happens to start a sentence. Present on macOS (a symlink to web2) and read
#: once; None when it is missing, and the morphology rules below carry on alone.
_WORDLIST = None
_WORDLIST_READ = False


def _common_words():
    global _WORDLIST, _WORDLIST_READ
    if not _WORDLIST_READ:
        _WORDLIST_READ = True
        try:
            _WORDLIST = frozenset(
                w.strip().lower()
                for w in pathlib.Path("/usr/share/dict/words").read_text(
                    errors="ignore").splitlines() if w.strip()
            )
        except OSError:
            _WORDLIST = None
    return _WORDLIST


SENTENCE_START = re.compile(r"(?:^|(?<=[.!?])\s+|^\s*)")
INFLECTED = re.compile(r".+(ing|ed|ly)$", re.I)


def motifs(text):
    """The proper nouns a draft leans on, lowercased for comparison.

    **A capital letter is not evidence at the start of a sentence.** Every
    sentence-initial word is capitalised, so "Just watched...", "Walked into
    ...", "People keep calling it a retreat" banked *just*, *walked* and
    *people* as references, and the next five drafts could not use those words
    at all. It bit hardest right after the lived-opener rule landed, because
    that rule asks for exactly those openers. A sentence-initial word now has
    to earn its place: an acronym (DSW), a name that recurs mid-sentence, or
    one followed by another capital (Taylor Swift) counts; anything the system
    dictionary knows, or that looks inflected, does not."""
    text = text or ""
    starts = {m.end() for m in re.finditer(r"(?:^|[.!?]\s+)", text)}
    found = [(m.start(), m.group(1)) for m in MOTIF_RE.finditer(text)]
    mid = {w for pos, w in found if pos not in starts}
    common = _common_words()
    out = set()
    for pos, w in found:
        if w in MOTIF_STOPWORDS:
            continue
        if pos in starts:
            tail = text[pos + len(w):pos + len(w) + 24]
            earned = (
                w.isupper()                              # DSW, NBA, EFL
                or w in mid                              # recurs mid-sentence
                or bool(re.match(r"\s+[A-Z][a-z]", tail))  # Taylor Swift
                # "Ring" is both a signature and an ordinary word, and the
                # dictionary cannot tell them apart. The signatures are the
                # references this budget exists for, so they always count.
                or w.lower() in SIGNATURE_MOTIFS
            )
            if not earned:
                if INFLECTED.match(w):
                    continue
                if common is not None and w.lower() in common:
                    continue
                if common is None:
                    continue
        out.add(w.lower())
    return out


def shipped_finals(generated_path):
    """The texts that actually shipped, oldest first.

    **Every rolling window reads this, and only PASS rows count.** The log
    also holds rejected attempts with their last draft in `final`, and the
    windows were reading those too: a Crumbl topic that failed once put
    "crumbl" on cooldown, so its own retry was vetoed for "reusing" a name
    that never reached the inbox, and the maker started writing "the
    pink-box cookie chain" to dodge it and was failed for vagueness. A
    cooldown is about what the reader has seen. Missing or unreadable log
    means an empty list: a budget that cannot be computed must never block."""
    import json as _json

    path = pathlib.Path(generated_path) if generated_path else None
    if not path or not path.exists():
        return []
    out = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except ValueError:
                continue
            if row.get("verdict") == "PASS" and row.get("final"):
                out.append(row["final"])
    except OSError:
        return []
    return out


def topic_exempt(cooling, topic):
    """The cooldown minus whatever the topic itself names.

    The budget exists to stop the maker reaching for the same reference
    unprompted, which is the Rams reflex. A topic about Crumbl that is
    refused the word "Crumbl" is not that: it is unwinnable, and the maker
    writes "the pink-box cookie chain" instead and fails for vagueness. The
    signatures stay locked even when a topic names them, because those are
    the reflex the budget was built for."""
    return frozenset(set(cooling) - (motifs(topic or "") - SIGNATURE_MOTIFS))


def recent_motifs(generated_path, lookback=MOTIF_LOOKBACK):
    """Every proper noun the last few shipped drafts used.

    Reads the tenant's own log rather than taking an argument from the
    caller, because the caller that forgets to pass it gets no cooldown and
    nothing says so."""
    finals = shipped_finals(generated_path)
    used = set()
    for text in finals[-lookback:]:
        used |= motifs(text)
    # **The signatures share one budget.** Tracking them separately let a batch
    # of five drafts spend Rams on the first and Ring on the third, which is
    # precisely the complaint ("each one has either Ring or Rams"). Any one of
    # them inside the window locks all of them, so the feed goes twenty drafts
    # between visits to that corner of the persona rather than twenty per name.
    for text in finals[-SIGNATURE_LOOKBACK:]:
        if motifs(text) & SIGNATURE_MOTIFS:
            used |= SIGNATURE_MOTIFS
            break
    return frozenset(used)

EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
EMOJI_LOOKBACK = 3


def closing_emoji(draft):
    """The reaction emoji a draft signs off with, or None.

    His own posts end on one in 4 of 17, and on four different ones. The first
    batch drafted after the card said the emoji were allowed ended 3 of 3 on
    the same 🤌🏻, which is how the Rams reflex started: permission read as
    instruction. Same answer as Rams, then, a cooldown rather than a ban."""
    tail = (draft or "").rstrip()[-8:]
    m = None
    for m in EMOJI.finditer(tail):
        pass
    return m.group(0) if m else None


def recent_closing_emoji(generated_path, lookback=EMOJI_LOOKBACK):
    """True if any of the last few shipped drafts already signed off with one."""
    return any(closing_emoji(t) for t in shipped_finals(generated_path)[-lookback:])

# --- thinking in public ------------------------------------------------
#
# Measured against the writers the card names as the models (Hobart, Tavel,
# Lessin) and against Adi's own timeline, next to 155 shipped drafts:
#
#                                  them    his    ours
#   says where the thought came from  32%    29%     3%
#   hedge or uncertainty anywhere     23%    18%     5%
#   a question anywhere               23%    22%     3%
#   argues with a view people hold     9%    12%     1%
#   any of the four                   55%    53%    17%
#
# Byrne Hobart is named on the card as the closest stylistic neighbour and
# not one of his top posts is the shape this engine produces: they are
# wonderings, questions, jokes, and arguments with somebody. Ours are closed
# three-beat rulings, because the maker's brief demanded a verdict and the
# TAKE axis rewarded one.
#
# 45% of their posts have none of the four, so this cannot be a per-draft
# veto without flattening the voice the other way. It is a quota instead:
# three closed verdicts in a row is a feed with a tic, so after two the next
# draft has to show its working.
PUBLIC_LOOKBACK = 2
HEDGE = re.compile(
    r"\b(i wonder|probably|maybe|my guess|i think|i suspect|i bet|seems|"
    r"might|could be wrong|not sure|i conclude|honestly|odd)\b", re.I)
PROVENANCE = re.compile(
    r"\b(was talking to|i hear|i keep hearing|i see a lot of people|OH:|"
    r"tried to|just read|just watched|just saw|looking at|i am reading|"
    r"somebody asked|read this|spent|walked into|lost another|sat through|"
    r"after \w+ episodes)\b", re.I)
OTHERS_VIEW = re.compile(
    r"\b(many people|a lot of people|everyone says|people predict|"
    r"the consensus|most people (?:think|say)|the standard take|"
    r"everyone(?:['\u2019]s| is) acting)\b", re.I)


# --- feeling --------------------------------------------------------------
#
# Presence was not enough. "Just paid $7 for a Crumbl cookie that will be gone
# from the menu next week" has the author in it and no feeling at all: it is a
# receipt. Measured on his posts against the reference writers and our drafts:
#
#                                   him    Hobart/Tavel/Lessin   ours (bangers)
#   names or shows a feeling        65%          18%                57%
#   a 1-3 word reaction ("Unfair.") 65%          14%
#
# Two things follow. The reference writers are dry, so emotion is the part of
# this voice that is specifically his, not something borrowed from them. And
# his posts without a feeling are all his data posts ("Q1 2026: $300B of
# venture funding..."); his reactions nearly always carry one. So a banger,
# which is a reaction, must name or show a feeling, and analytical posts stay
# as dry as his do.
#
# The vocabulary is the GoEmotions taxonomy (Google, 27 labels from 58k Reddit
# comments) written the way people actually type those feelings, plus the
# slang and punctuation that VADER documents as intensity. EmoLex and LIWC were
# considered and are not used: both need a paid licence for commercial use.
FEELING = re.compile(
    r"\b(love|loved|lovely|hate|hated|scary|scared|terrified|annoy\w*|"
    r"frustrat\w*|embarrass\w*|jealous|obsessed|mad|furious|unfair|respect|"
    r"proud|ashamed|guilty|sad|gutted|thrilled|excited|bored|confus\w*|"
    r"suspicious|regret\w*|relieved|disgust\w*|cringe|hurts|delight\w*|insane|"
    r"wild|crazy|dumb|stupid|ridiculous|absurd|bonkers|amazing|beautiful|grim|"
    r"weird|creepy|nervous|worried|afraid|surprising|brutal|iconic|yikes|ugh|"
    r"smh|lol|lmao|god damn|damn|hilarious|painful|petty|furious|salty|"
    r"miss it|can['\u2019]t stop|i['\u2019]m sold|rent free)\b", re.I)
FEELING_REGISTERS = frozenset({"banger"})


# VADER (MIT, Hutto & Gilbert 2014): 7,520 social-media words rated for
# sentiment strength by human raters. Shipped in scripts/lexicons/ with its
# licence. Only the token and its mean rating are used.
VADER_PATH = pathlib.Path(__file__).resolve().parent / "lexicons" / "vader_lexicon.txt"
VADER_STRONG = 1.5
_VADER = None
_VADER_READ = False
VADER_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?")
# Words that put the author right before the rated word: "I'm obsessed",
# "kinda heartbreaking", "honestly brutal", "looks suspicious".
REACTION_CUES = frozenset({
    "i", "i'm", "im", "i've", "my", "me", "it's", "that's", "so", "kinda",
    "sorta", "honestly", "too", "really", "pretty", "lowkey", "am", "feel",
    "felt", "looks", "just",
})


def _vader():
    global _VADER, _VADER_READ
    if not _VADER_READ:
        _VADER_READ = True
        lex = {}
        try:
            for line in VADER_PATH.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        lex[parts[0]] = float(parts[1])
                    except ValueError:
                        continue
        except OSError:
            lex = {}
        _VADER = lex or None
    return _VADER


def reaction_words(draft):
    """Strongly rated VADER words that sit where a reaction sits.

    **Not a plain sentiment threshold, on purpose.** VADER scores what a post
    is about: "Some bubbles build things... Bet on what survives" peaks at 2.3
    on "survived", a post about fraud scores as emotional because of "fraud".
    Used that way it separated his posts (53%) from our authorless bangers
    (39%) barely at all. Counted only in reaction position, meaning a sentence
    of four words or fewer ("Brutal.") or within three words after the author
    ("I'm obsessed", "kinda heartbreaking"), it lifts recognition of his own
    posts from 65% to 71% over the word list and still scores the old bangers
    and the Crumbl receipt at zero."""
    lex = _vader()
    if not lex:
        return set()
    found = set()
    text = (draft or "").lower().replace("\u2019", "'")
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        toks = VADER_TOKEN.findall(sentence)
        short = 0 < len(toks) <= 4
        for i, w in enumerate(toks):
            if abs(lex.get(w, 0.0)) < VADER_STRONG:
                continue
            if short or any(c in REACTION_CUES for c in toks[max(0, i - 3):i]):
                found.add(w)
    return found


def feelings(draft):
    """The feeling words a draft uses, lowercased: the word list's matches
    plus any VADER word in reaction position."""
    return ({m.group(0).lower() for m in FEELING.finditer(draft or "")}
            | reaction_words(draft))


def has_feeling(draft):
    """Does the post say how the author felt, not only what happened to them?

    A named feeling ("suspicious", "I love it", "unfair"), a reaction in slang
    ("smh"), an exclamation, a reaction emoji, or a strongly rated VADER word
    in reaction position. "Just paid $7" is presence; "Just paid $7 and I'm
    weirdly proud of it" is a feeling."""
    text = draft or ""
    if (FEELING.search(text) or "!" in text or EMOJI.search(text)
            or reaction_words(text)):
        return True
    # The GoEmotions model, when installed, reads feelings no list names
    # ("I keep going back anyway"). Only an author-side reading counts: see
    # emotion.SUBJECT_LABELS for why praise of the subject does not.
    return bool(_emotion_rescue(text))


def _emotion_rescue(text):
    try:
        import emotion
    except ImportError:
        return None
    try:
        return emotion.rescues(text)
    except Exception:
        return None


STANCE_WORDS = (
    "honestly", "i wonder", "my guess", "probably", "maybe", "i think",
    "i suspect", "i bet", "not sure", "i conclude", "odd", "kinda", "smh",
    "no idea", "i can't tell", "i cannot tell",
)


def stance_words(draft):
    """Which uncertainty markers and feeling words a draft uses, lowercased.

    Feeling words share the hedges' budget for the same reason: anything newly
    required gets grabbed and repeated ("unfair" in every banger would be the
    🤌🏻 problem again)."""
    text = (draft or "").lower().replace("\u2019", "'")
    return {w for w in STANCE_WORDS if w in text} | feelings(text)


def recent_stance_words(generated_path, lookback=2):
    """The hedges and feeling words the last couple of shipped drafts used.

    The maker reaches for one word and keeps it: the first batch after the
    emoji was documented ended 3 of 3 on the same 🤌🏻, and the first batch
    after hedging was allowed opened its closer with "Honestly" in 2 of 3.
    Hobart and Tavel vary theirs across "I wonder", "probably", "I conclude",
    "maybe I've misunderstood", so the budget is per word, not per draft."""
    used = set()
    for t in shipped_finals(generated_path)[-lookback:]:
        used |= stance_words(t)
    return frozenset(used)

def thinks_in_public(draft):
    """Does the post show the thinking, or only the conclusion?

    Four shapes, all taken off the reference writers: it says where the
    thought came from, it is uncertain about something, it asks a real
    question, or it argues with a view other people hold."""
    text = draft or ""
    return bool(
        PROVENANCE.search(text) or HEDGE.search(text)
        or "?" in text or OTHERS_VIEW.search(text)
    )


FEELING_LOOKBACK = 2


def recent_feeling(generated_path, lookback=FEELING_LOOKBACK):
    """True if any of the last few shipped drafts carried a feeling.

    Fewer shipped drafts than the window means no quota yet."""
    finals = shipped_finals(generated_path)
    if len(finals) < lookback:
        return True
    return any(has_feeling(t) for t in finals[-lookback:])


def recent_public_thinking(generated_path, lookback=PUBLIC_LOOKBACK):
    """True if any of the last few shipped drafts showed its working.

    Fewer shipped drafts than the window means no quota yet: a brand new
    tenant must not have its first post forced into a shape."""
    finals = shipped_finals(generated_path)
    if len(finals) < lookback:
        return True
    return any(thinks_in_public(t) for t in finals[-lookback:])

def slop_hits(draft, allowed=()):
    """The refused constructions present in a draft, as (quote, label) pairs.

    Quoted like the model's tells are quoted, and for the same reason: a
    rejection somebody cannot point at is a rejection they cannot act on.
    `allowed` holds labels this person genuinely uses (tenant setting)."""
    hits = []
    for pattern, label in SLOP_PATTERNS:
        if label in allowed:
            continue
        m = re.search(pattern, draft or "", re.MULTILINE)
        if m:
            hits.append((m.group(0).strip(), label))
    return hits


def decide(scores, draft, allow_em_dash=False, max_chars=None, banned_motifs=(),
           standalone=True, register=None, emoji_cooling=False,
           must_think=False, banned_stance=(), require_feeling=True,
           must_feel=False, avoid_sentiment=None, presence_plural=False,
           allowed_constructions=()):
    """Pure. The gate decision, computed in Python not trusted to the model.
    Every axis must clear its threshold. Returns (verdict, override_or_empty).

    The em-dash veto is per-tenant, not universal. It is Adi's rule and the
    highest-precision AI tell in his card, so it stays on by default, but a
    tenant whose own prose uses em dashes would otherwise have every draft that
    sounds like them rejected here before the model's verdict was even read."""
    if not allow_em_dash and has_em_dash(draft):
        return "FAIL", "em-dash present (tenant rule, deterministic)"
    # Length before anything a model said. A 900-character draft is not a post
    # that needs tightening, it is the wrong artefact, and sending it back with
    # the count is the feedback that fixes it.
    ceiling = int(max_chars or DEFAULT_MAX_CHARS)
    if draft and len(draft.strip()) > ceiling:
        return "FAIL", f"too long: {len(draft.strip())} chars, ceiling {ceiling}"
    orphan = orphan_pronoun(draft)
    if orphan:
        return "FAIL", (
            f'"{orphan}" with nobody named before it: a cold reader cannot tell who'
        )
    if not standalone:
        return "FAIL", "needs context a stranger does not have"
    repeats = sorted(motifs(draft) & set(banned_motifs or ()))
    if repeats:
        return "FAIL", (
            "reference used in the last {} drafts: {}".format(
                MOTIF_LOOKBACK, ", ".join(repeats[:3]))
        )
    repeated = sorted(stance_words(draft) & set(banned_stance or ()))
    if repeated:
        return "FAIL", (
            '"{}" again: the last posts already leaned on it, and a hedge that '
            "repeats is a tic, not a voice".format(repeated[0])
        )
    if must_think and not thinks_in_public(draft):
        return "FAIL", (
            "the last two posts were closed verdicts and so is this one: show "
            "the thinking (where it came from, what is uncertain, the view "
            "being argued with, or the question actually being asked)"
        )
    if emoji_cooling and closing_emoji(draft):
        return "FAIL", (
            "closing emoji again: he signs off with one in about one post in "
            "four, and not twice running"
        )
    if register in PRESENCE_REGISTERS and not has_presence(draft, plural=presence_plural):
        return "FAIL", (
            "nobody in the post: a banger is him reacting to something, so it "
            "needs I/my, the moment he met it, a question, or a reaction"
        )
    if avoid_sentiment:
        try:
            import emotion
            group = emotion.lead_group(draft)
        except Exception:
            group = None
        if group == avoid_sentiment:
            return "FAIL", (
                f"drifting {avoid_sentiment}: recent drafts already leaned that "
                "way and this person rarely does, so lean the way their palette does"
            )
    if must_feel and not has_feeling(draft):
        return "FAIL", (
            "the last two posts carried no feeling and neither does this one: "
            "say how it landed, the way their voice card shows they do"
        )
    if require_feeling and register in FEELING_REGISTERS and not has_feeling(draft):
        return "FAIL", (
            "no feeling: he is in the post but never says how it landed. Name "
            "it (annoyed, suspicious, I love it, unfair, smh) or show it"
        )
    hits = slop_hits(draft, allowed=allowed_constructions)
    if hits:
        return "FAIL", "refused construction: " + "; ".join(
            f"{label} ({quote[:40]})" for quote, label in hits[:2]
        )
    # A draft that still names a blank is not finished. The maker is allowed to
    # write [VERIFY: median seed] rather than invent a number it cannot know,
    # which is the right instinct and the reason the marker exists: the
    # alternative is a fabricated statistic published in someone's own voice.
    #
    # It used to ship anyway, in an email shaped around the warning and with no
    # post link, and that email is not wanted. Dropping only the email would
    # send the same draft looking ordinary, one tap from publishing the literal
    # text "[VERIFY: median seed]", so the draft is what gets rejected. Failing
    # here sends the maker back with the reason and it usually returns with a
    # sentence it can support instead.
    slots = verify_slots(draft)
    if slots:
        return "FAIL", ("unfilled blank(s): " + "; ".join(slots[:4]))
    below = [a for a in AXES if scores.get(a, 0) < THRESHOLDS[a]]
    if below:
        return "FAIL", f"below bar on {', '.join(below)}"
    return "PASS", ""


def grade(draft, voice, context=None, register="auto", log=False, author=None,
          critiques_path=None, allow_em_dash=False, tenant=None,
          effort=None, generated_path=None, max_chars=None,
          banned_motifs=None, palette="", require_feeling=True,
          feeling_quota=False, avoid_sentiment=None, brief_note="",
          presence_plural=False, allowed_constructions=()):
    """The gate. One claude call, then a deterministic Python decision. On a
    critic-call failure returns verdict ERROR (never PASS), so an infrastructure
    fault is never read as a clean content FAIL or PASS.

    `author` names whose editor this is; `critiques_path` is where the log goes.
    Both default to the legacy single-tenant values so existing callers and the
    CLI keep working; heartbeat passes the tenant's."""
    log_path = critiques_path or CRITIQUES
    try:
        out = call_claude(
            build_critique_prompt(voice, draft, context, register, author=author,
                                  palette=palette, brief_note=brief_note,
                                  allowed=allowed_constructions),
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
    # **The seam that made every cooldown here inert.** Callers pass `tenant`
    # as the tenant *id* (heartbeat passes tenant.id, so the log row stays
    # JSON), and getattr("adi", "generated_path", None) is None, so the motif
    # budget, the emoji budget and the thinking quota all read an empty log and
    # waved everything through. Only the maker side saw them, because gen
    # passes the path explicitly. The path and the ceiling now arrive as
    # arguments, and the tenant attribute is the fallback for a caller that
    # hands over the object.
    log_path_for_rules = generated_path or getattr(tenant, "generated_path", None)
    ceiling = max_chars if max_chars is not None else getattr(tenant, "max_chars", None)
    verdict, override = decide(
        parsed["scores"], draft, allow_em_dash=allow_em_dash,
        max_chars=ceiling,
        banned_motifs=(banned_motifs if banned_motifs is not None
                       else recent_motifs(log_path_for_rules)),
        standalone=parsed.get("standalone", True),
        register=register,
        emoji_cooling=recent_closing_emoji(log_path_for_rules),
        must_think=not recent_public_thinking(log_path_for_rules),
        banned_stance=recent_stance_words(log_path_for_rules),
        require_feeling=require_feeling,
        must_feel=feeling_quota and not recent_feeling(log_path_for_rules),
        avoid_sentiment=avoid_sentiment,
        presence_plural=presence_plural,
        allowed_constructions=allowed_constructions,
    )
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
    # The deterministic vetoes first, because they are the reason the draft
    # failed and the model does not know about them: decide() can reject a
    # draft the critic itself scored above the bar on every axis, in which
    # case nothing else in here has anything to say and the maker was being
    # told to "sharpen it" for an unfilled blank or an em dash.
    override = (verdict.get("override") or "").strip()
    if "blank" in override.lower():
        parts.append(
            "This draft still contains a [VERIFY: ...] placeholder, so it "
            "cannot be published. Do not invent a number to fill it. Rewrite "
            "the sentence so it makes a point you can support without the "
            "missing fact, or drop that sentence. " + override)
    elif "em-dash" in override.lower():
        parts.append("Remove the em dash. Use a comma, a colon or a full stop.")
    elif override:
        parts.append(override)
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
    # Reads the bar from THRESHOLDS rather than pinning a literal. The number
    # moved on 2026-10-17 and this assertion was the only thing that noticed.
    assert f"needs {THRESHOLDS['CLEAN']}" in got[1], got[1]

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

    # The human element, pinned to the real drafts on both sides. The first is
    # the draft Adi pasted back ("where human element"); the rest are his.
    authorless = ("Every Netflix sports doc has the GM-stares-at-a-whiteboard-of-names "
                  "shot now. Drive to Survive shot it first because Ferrari and "
                  "Mercedes refused cameras in season one and Haas had a wall.")
    assert not has_presence(authorless)
    for his in (
        "After three episodes of The Pyramid Scheme my LIC agent looks suspicious "
        "to me. The performance has broken my fraud detection. Unfair.",
        "Just watched Toy Story 5 today, and we keep buying tickets to watch it "
        "happen to us.",
        "Why is everyone acting so dumb? Lenders took the actual recovery years ago.",
        "They did build the name first, and honestly the name is doing $2B of work.",
    ):
        assert has_presence(his), his
    clean = {"VOICE": 9, "TAKE": 9, "SPECIFIC": 9, "CLEAN": 9}
    # Gated for the register where he is reacting, free everywhere else.
    assert decide(clean, authorless, register="banger")[0] == "FAIL"
    assert decide(clean, authorless, register="analytical")[0] == "PASS"
    assert decide(clean, authorless, register=None)[0] == "PASS"
    # His fish post as he wrote it, with the 🕺🕺 doing the feeling. Without
    # them it has presence and no feeling, and the feeling gate (below) says so.
    assert decide(clean, "Just read it. The fish is still dead. \U0001F57A\U0001F57A",
                  register="banger")[0] == "PASS"
    # A capital letter at the start of a sentence is not a proper noun.
    assert "people" not in motifs("People keep calling it a retreat.")
    assert "just" not in motifs("Just watched my fourth Criterion Closet video.")
    assert "walked" not in motifs("Walked into my fourth coffee shop this week.")
    assert "criterion" in motifs("Just watched my fourth Criterion Closet video.")
    assert "nike" in motifs("Nike spent years going direct. Nike came back.")
    assert "dsw" in motifs("DSW shelves filled up again.")
    assert {"taylor", "swift"} <= motifs("Taylor Swift bought the masters back.")
    assert "ring" in motifs("Ring shipped a doorbell."), "signatures always count"
    assert "michelin" in motifs("Michelin gave it a star.")

    # Thinking in public: a quota, not a veto, and it reads the four shapes.
    assert thinks_in_public("I wonder if the tax planning move is a foundation.")
    assert thinks_in_public("Was talking to a founder and noticed something.")
    assert thinks_in_public("Have any of the labs tried this in history?")
    assert thinks_in_public("A lot of people predict this future. I don't.")
    assert not thinks_in_public(
        "Nike walked back into Foot Locker after cutting wholesale. The margin "
        "went straight to Meta's ad auction.")
    assert decide(clean, "Nike walked back into Foot Locker.",
                  must_think=True)[0] == "FAIL"
    assert decide(clean, "Nike walked back into Foot Locker.",
                  must_think=False)[0] == "PASS"
    assert decide(clean, "I wonder why Nike walked back into Foot Locker.",
                  must_think=True)[0] == "PASS"
    # The pronouncement closer, which fires on 17% of our drafts and none of
    # theirs. Tavel's own 7-word "Most teams are..." closer must survive.
    assert slop_hits("Every DTC brand hides its spec sheet behind a founder "
                     "letter and calls that transparency.")
    assert not slop_hits("Most teams are just scratching the surface.")

    # The feeling quota: deadpan is allowed, three deadpans in a row is not.
    assert decide(clean, "LOIs do not count as customers. I keep saying it.",
                  register="analytical", must_feel=True)[0] == "FAIL"
    assert decide(clean, "LOIs do not count as customers. I love that rule.",
                  register="analytical", must_feel=True)[0] == "PASS"
    import json as _jq, tempfile as _tfq
    _q = pathlib.Path(_tfq.mkdtemp()) / "q.jsonl"
    _q.write_text("\n".join(_jq.dumps({"verdict": "PASS", "final": x}) for x in (
        "Signed contracts count.", "Send the forwardable blurb.")))
    assert recent_feeling(_q) is False
    _q.write_text(_q.read_text() + "\n" + _jq.dumps(
        {"verdict": "PASS", "final": "I love a wild idea."}))
    assert recent_feeling(_q) is True

    # The feeling rule is per person: a deadpan verdict is Khoa's voice.
    assert decide(clean, "LOIs do not count as customers. I keep saying it.",
                  register="banger", require_feeling=False)[0] == "PASS"
    assert decide(clean, "LOIs do not count as customers. I keep saying it.",
                  register="banger", require_feeling=True)[0] == "FAIL"

    # Presence is not feeling. The Crumbl receipt has one and not the other.
    receipt = ("Just paid $7 for a Crumbl cookie that will be gone from the "
               "menu next week.")
    assert has_presence(receipt) and not has_feeling(receipt)
    assert decide(clean, receipt, register="banger")[0] == "FAIL"
    assert decide(clean, receipt, register="analytical")[0] == "PASS"
    assert has_feeling("The performance has broken my fraud detection. Unfair.")
    assert has_feeling("Whatever it is. I love it.")
    assert has_feeling("Stop copying other people's work. smh.")
    assert decide(clean, "Just paid $7 for a Crumbl cookie. Unfair how good it was.",
                  register="banger")[0] == "PASS"
    assert decide(clean, "Just paid $7 for a Crumbl cookie. Unfair how good it was.",
                  register="banger", banned_stance={"unfair"})[0] == "FAIL"

    # VADER widens the net only where a reaction sits, so a post about fraud
    # is not "emotional" and a receipt still is not.
    if _vader():
        assert "heartbreaking" in reaction_words("Kinda heartbreaking how good it was.")
        assert reaction_words("Brutal.") == {"brutal"}
        assert not reaction_words("Lenders took the recovery after the fraud case.")
        assert has_feeling("Paid $7 for a cookie and I'm weirdly proud of it.")
        assert not has_feeling(receipt)

    # Per-person exceptions: Thomas writes "we", and "not X" is his reframe.
    tl = "That makes us optimistic. Realistically."
    assert not has_presence(tl) and has_presence(tl, plural=True)
    assert decide(clean, tl, register="banger", require_feeling=False)[0] == "FAIL"
    assert decide(clean, tl, register="banger", require_feeling=False,
                  presence_plural=True)[0] == "PASS"
    reframe = "It's not about the model, it's about the review loop we build."
    assert slop_hits(reframe) and not slop_hits(reframe, allowed=["not-X-it's-Y contrast"])
    assert "not-X-it's-Y" not in clean_axis("analytical", ["not-X-it's-Y contrast"])[0]
    assert "not-X-it's-Y" in clean_axis("analytical")[0]

    # The Crumbl draft, which shipped because its subject was "That".
    assert slop_hits("Just paid $7 for a Crumbl cookie. That's not a bakery, "
                     "it's Supreme's drop model with frosting.")
    assert slop_hits("They aren't selling cookies, they're selling a line.")

    # Only shipped drafts count toward any window.
    import json as _j, tempfile as _tf
    _log = pathlib.Path(_tf.mkdtemp()) / "g.jsonl"
    _log.write_text("\n".join(_j.dumps(r) for r in (
        {"verdict": "PASS", "final": "Paid $7 for a Crumbl cookie. Unfair."},
        {"verdict": "FAIL", "final": "Glossier is in Sephora now, honestly."},
    )))
    assert shipped_finals(_log) == ["Paid $7 for a Crumbl cookie. Unfair."]
    assert "glossier" not in recent_motifs(_log)
    assert "paid" not in recent_motifs(_log) and "crumbl" in recent_motifs(_log)
    assert "honestly" not in recent_stance_words(_log)
    assert "verify" not in motifs("Crumbl swaps [VERIFY: how many] flavors.")
    # A topic may name its own subject; it may not unlock a signature.
    assert "crumbl" not in topic_exempt({"crumbl", "nike"}, "Crumbl rotates its menu")
    assert "nike" in topic_exempt({"crumbl", "nike"}, "Crumbl rotates its menu")
    assert "rams" in topic_exempt({"rams"}, "What Rams would make of Crumbl")

    # A hedge that repeats is a tic: same budget as the sign-off emoji.
    assert stance_words("Honestly I respect the funnel.") == {"honestly", "respect"}
    assert decide(clean, "Honestly I respect the funnel.",
                  banned_stance={"honestly"})[0] == "FAIL"
    assert decide(clean, "My guess is it goes the same way.",
                  banned_stance={"honestly"})[0] == "PASS"

    # The sign-off emoji is rationed the way a signature reference is.
    assert closing_emoji("the countdown is the ad. \U0001F90C\U0001F3FB") is not None
    assert closing_emoji("no emoji here.") is None
    assert closing_emoji("") is None
    assert decide(clean, "Just read it. The fish is still dead. \U0001F57A",
                  register="banger", emoji_cooling=True)[0] == "FAIL"
    assert decide(clean, "Just read it. The fish is still dead. \U0001F57A",
                  register="banger", emoji_cooling=False)[0] == "PASS"
    assert decide(clean, "Just read it. The fish is still dead, smh.",
                  register="banger", emoji_cooling=True)[0] == "PASS"

    # Presence is not a licence: the other vetoes still run first.
    assert decide(clean, "I think, honestly, this is fine " + "x" * 300,
                  register="banger")[0] == "FAIL"

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
