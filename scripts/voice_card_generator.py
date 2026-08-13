#!/usr/bin/env python3
"""Karamel voice-card generator: a person's raw writing + a short intake -> a
tuned voice card. The moat.

The drafter, banger, and content_prompter all read a voice card; the quality of
every post they generate is bounded by how well that card captures the person.
This turns a ~15-minute intake (their best real writing plus a few identity/lane
answers) into a card in Karamel's canonical structure, GROUNDED in their actual
lines, not a generic template. Voice match is the thing nobody can copy.

Concierge model (the GTM): Adi fills the intake on a founder's behalf, paste
their strongest posts/emails, answer the identity/lane/no-go questions, run
this, hand the card to the drafter. This is the one real product under the
concierge service.

Design stance (post X platform-manipulation label, 2026-06): every card this
ships is ORIGINAL-FIRST. Original posts in the person's voice are the primary,
safe distribution surface. Replies are surgical and rate-limited, no bulk list
activity. Account safety is baked into the product, not bolted on.

Same $0 mechanism as drafter.py / reflector.py: claude -p on the subscription,
CLAUDE* env stripped, cwd=/tmp. Em-dash scrub (Adi's hard rule, and the single
highest-precision AI tell). Read-only on any existing card: writes to
data/voice_cards/<slug>.md, never overwrites 03_voice_card.md.

Flags:
  --intake PATH      intake JSON (required unless --print-template)
  --print            write to stdout instead of the file
  --dry-run          build and show the prompt, do not call claude (prompt QA)
  --print-template   emit the blank intake schema (JSON) and exit
  --slug NAME        output filename stem (default: derived from the name)
"""
import json
import re
import sys
from pathlib import Path

import llm
from karamel_common import DATA, ROOT, has_em_dash, now_et, now_iso

EXEMPLAR = ROOT / "03_voice_card.md"          # structural bar, never copied
OUT_DIR = DATA / "voice_cards"
INTAKES_DIR = DATA / "intakes"
EM_DASH = re.compile(r"\s*[—–]\s*")  # collapse surrounding space so a scrub leaves no double gap

MIN_SAMPLES = 6        # below this, there is not enough signal to ground a card
REQUIRED_KEYS = ("name", "samples")

# Blank intake. --print-template writes this so a new founder intake starts
# from a known shape. Every field except name/samples is optional, but the more
# you fill, the sharper the card. Samples are the gold: real lines in their
# voice. Quote 10 to 25 of their best.
TEMPLATE = {
    "name": "",
    "handle": "",
    "one_liner": "",
    "identity": {
        "can_claim": [],
        "cannot_claim": [],
    },
    "lanes": [
        {"name": "", "note": ""}
    ],
    "registers": "",
    "obsessions": [],
    "sounds_like": [],
    "not_like": [],
    "hard_no": [],
    "tells": [],
    "samples": [],
}


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "founder"


def load_intake(path):
    """Read + validate an intake JSON. Raises ValueError on a bad intake so the
    caller fails loudly rather than shipping a thin card."""
    data = json.loads(Path(path).read_text())
    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"intake missing required field(s): {', '.join(missing)}")
    samples = [s for s in (data.get("samples") or []) if str(s).strip()]
    if len(samples) < MIN_SAMPLES:
        raise ValueError(
            f"only {len(samples)} non-empty samples; need at least {MIN_SAMPLES} "
            "to ground a voice. Paste more of their real writing."
        )
    data["samples"] = samples
    return data


def _bullets(items):
    return "\n".join(f"  - {x}" for x in items) if items else "  (none given)"


def _lanes_block(lanes):
    out = []
    for ln in lanes or []:
        nm = (ln.get("name") or "").strip()
        if not nm:
            continue
        note = (ln.get("note") or "").strip()
        out.append(f"  - {nm}" + (f": {note}" if note else ""))
    return "\n".join(out) if out else "  (none given; infer lanes from the samples)"


def _samples_block(samples):
    return "\n\n".join(f'SAMPLE {i}:\n"""{s.strip()}"""'
                       for i, s in enumerate(samples, 1))


def build_generation_prompt(intake, exemplar):
    allow_em_dash = bool(intake.get("allow_em_dash"))
    ident = intake.get("identity") or {}
    return f"""You are the voice architect for Karamel. You turn one person's raw writing plus a short intake into a VOICE CARD: the single source of truth that downstream tools use to draft posts and replies that sound exactly like that person. The card is only as good as how faithfully it captures THEM, so ground everything in their actual writing, never in a generic template.

Below is an EXEMPLAR card (it belongs to a different person, Adi). Use its STRUCTURE, depth, and the way it is specific as the bar to clear. Do NOT copy its content, claims, persona, examples, lanes, or obsessions. Those are Adi's. You are writing a card for the person in the intake.

=== EXEMPLAR CARD (structure only, do not copy content) ===
{exemplar}
=== END EXEMPLAR ===

Now the person you are writing the card FOR.

NAME: {intake.get('name')}
HANDLE: {intake.get('handle') or '(not given)'}
ONE-LINER: {intake.get('one_liner') or '(infer from samples)'}

IDENTITY, claims they CAN make (use only these, never inflate):
{_bullets(ident.get('can_claim'))}

IDENTITY, claims they must NOT make:
{_bullets(ident.get('cannot_claim'))}

LANES they play in:
{_lanes_block(intake.get('lanes'))}

REGISTER NOTES: {intake.get('registers') or '(infer the mix from the samples)'}

RECURRING OBSESSIONS (the personality layer):
{_bullets(intake.get('obsessions'))}

SOUNDS LIKE (reference points, not imitation):
{_bullets(intake.get('sounds_like'))}

DOES NOT SOUND LIKE:
{_bullets(intake.get('not_like'))}

HARD NO-GO topics:
{_bullets(intake.get('hard_no'))}

TELLS they hate / that read as not-them:
{_bullets(intake.get('tells'))}

THEIR REAL WRITING (the gold, study this hardest):

{_samples_block(intake.get('samples'))}

=== YOUR TASK ===
Write a complete voice card for {intake.get('name')} in the exemplar's structure. Rules:

1. GROUND THE TEXTURE IN THE SAMPLES. Read the samples and extract their REAL habits: average sentence length, how they open (fact / scene / question / list), punctuation patterns, the register mix, vocabulary, what they are funny about. The "Voice texture" section must describe what you actually observe, with specifics, not platitudes.
2. THE BAR EXAMPLES MUST BE THEIR REAL LINES. Quote 6 to 10 lines drawn from the samples above as the example bar, each with a one-line note on why it works. Do NOT invent posts in their voice; use what they actually wrote.
3. IDENTITY is a hard boundary. Only let them claim what is in "can make". Forbid everything in "must not make". If unsure whether a claim is defensible, leave it out.
4. ORIGINAL-FIRST. Original posts in their voice are the primary distribution surface and the bulk of output. Replies are surgical, add a specific fact or counter, and are capped at a small real number per day (state a number, default 10). No bulk list-building, no reply-at-volume. Write this into the card explicitly; it keeps their account safe.
{_dash_rules(allow_em_dash)}
7. Include a "Pre-publish checklist" and a "Version log" with a single v1 entry dated today.

Output ONLY the voice card as markdown, starting with "# {intake.get('name')}: Voice Card" and a metadata line. No preamble, no closing commentary."""


def call_claude(prompt, system=None):
    """One model call, via llm.py. Was a subprocess to the operator's
    personal `claude` CLI, which billed one person's seat for everyone's
    work and died whenever that login expired."""
    return llm.complete(prompt, system=system, label="voice-card")


DASH_RULES_BANNED = """5. ANTI-PATTERNS section must forbid em-dashes and en-dashes (the highest-precision AI tell), plus the tells they listed, plus generic AI connective tissue.
6. ZERO em-dashes or en-dashes anywhere in YOUR output. Use commas, colons, semicolons, periods, or "to" for ranges. This is non-negotiable."""

DASH_RULES_ALLOWED = """5. ANTI-PATTERNS section must list the tells they listed plus generic AI connective tissue. Do NOT forbid em-dashes: this person uses them in their own writing, and banning them would make the card contradict the samples it is built from.
6. Preserve em-dashes exactly as they appear in their quoted lines. The bar examples must be their real punctuation, not a normalised version of it."""


def _dash_rules(allow_em_dash):
    """Rules 5 and 6 were written for one person and asserted as universal.

    Rule 5 made every card forbid em-dashes; rule 6 stripped them from the
    generator's own output, including the bar examples, which are supposed to be
    the person's REAL lines. For an author who uses em-dashes that produced a
    card quoting them with their punctuation rewritten, then instructing the
    drafter to avoid the punctuation it had just misquoted."""
    return DASH_RULES_ALLOWED if allow_em_dash else DASH_RULES_BANNED


def scrub(text):
    return EM_DASH.sub(", ", text or "")


def strip_fences(text):
    """The model sometimes wraps the card in a ```markdown fence. Drop it."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def main():
    if "--print-template" in sys.argv:
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return 0

    if "--intake" not in sys.argv:
        print("usage: voice_card_generator.py --intake PATH [--print] "
              "[--dry-run] [--slug NAME]", file=sys.stderr)
        print("       voice_card_generator.py --print-template", file=sys.stderr)
        return 2

    intake_path = sys.argv[sys.argv.index("--intake") + 1]
    do_print = "--print" in sys.argv
    dry_run = "--dry-run" in sys.argv
    slug = None
    if "--slug" in sys.argv:
        slug = slugify(sys.argv[sys.argv.index("--slug") + 1])

    try:
        intake = load_intake(intake_path)
    except FileNotFoundError:
        print(f"intake error: no such file: {intake_path}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"intake error: {e}", file=sys.stderr)
        return 2
    slug = slug or slugify(intake.get("name"))
    prompt = build_generation_prompt(intake, EXEMPLAR.read_text())

    if dry_run:
        print(prompt)
        print(f"\n--- dry run: {len(intake['samples'])} samples, "
              f"prompt {len(prompt)} chars, would write data/voice_cards/{slug}.md ---",
              file=sys.stderr)
        return 0

    card = strip_fences(call_claude(prompt))
    # Belt and suspenders, but only for tenants who ban them. Scrubbing
    # unconditionally rewrote the author's own quoted lines.
    if not bool(intake.get("allow_em_dash")) and has_em_dash(card):
        card = scrub(card)
    card = card.rstrip() + (
        f"\n\n<!-- generated by Karamel voice_card_generator from "
        f"{Path(intake_path).name} on {now_iso()} -->\n"
    )

    if do_print:
        print(card)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{slug}.md"
    out_path.write_text(card)
    print(f"wrote {out_path}  ({len(card)} chars, "
          f"{card.count(chr(10)) + 1} lines)  {now_et().strftime('%H:%M ET')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
