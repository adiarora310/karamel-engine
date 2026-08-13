# Voice card intake

Send this to the person. Their answers become `data/intakes/<slug>.json`, which
`voice_card_generator.py` turns into a card. Every draft they ever receive is
bounded by what is in here.

**15 minutes, and section 10 is most of the value.** A card built from good
answers to 1-9 and weak samples reads like a competent stranger. A card built
from 8 real posts reads like them. If they only have time for one section, it is
section 10.

Answers are quoted back into the card, so verbatim beats polished. "I never use
exclamation marks and I hate the word 'excited'" is more useful than "my tone is
measured and professional."

---

**1. Name and handle.** As they want to be referred to, plus their X handle.

**2. One line: who are you?** How they would introduce themselves to someone
whose respect they want. Not a job title.

**3. What can you claim?** Things they have actually done, built, shipped,
invested in, been in the room for. Specifics with numbers where they exist. This
is what the drafter is allowed to reference as lived experience.

**4. What can you NOT claim?** The dangerous half, and skipping it is how a
draft ends up asserting something that gets them caught out. Titles they do not
hold, exits they did not have, rooms they were not in, expertise adjacent to but
not actually theirs. Be blunt.

**5. Your lanes.** Three or four subjects they can post about with authority,
each with a sentence on the angle they take. "Fintech, but specifically why
compliance eats margin, from having done it."

**6. Registers.** How their writing changes by mode. Most people have two: a
short punchy one and a longer analytical one. When do they use which? Any
recurring structures ("I usually open with a number").

**7. Obsessions.** Things they return to unprompted, whether or not they seem
professional. The cross-domain ones matter most, because a take nobody else
would make usually comes from an interest nobody else has.

**8. Sounds like / not like.** Five each. Descriptions of their voice, and the
adjacent voices they would hate to be mistaken for. "Not LinkedIn-inspirational"
and "not a Bloomberg terminal" both do real work.

**9. Hard no.** Anything they will not post about, ever. Partisan politics, a
competitor, an employer, a family situation, a former client. The critic treats
these as automatic rejections, so nothing here reaches them as a draft.

**10. Samples. The important one.** Eight or more pieces of their own writing
they would be happy to have judged on. X posts are ideal; Slack messages,
emails, and blog paragraphs all work. Pasted raw, typos included.

Not their most *successful* posts, their most *characteristic* ones. Something
that got four likes but sounds exactly like them is worth more here than
something that went wide because it was broadly agreeable.

**11. Tells.** Habits, in their words. Punctuation they avoid, words they
overuse, whether they use emoji, sentence length, how they open and close, any
phrase that is unmistakably theirs.

---

## Turning it into a card

```bash
python3 scripts/voice_card_generator.py --print-template > data/intakes/<slug>.json
# fill it in from the answers above
python3 scripts/voice_card_generator.py --intake data/intakes/<slug>.json --dry-run
python3 scripts/voice_card_generator.py --intake data/intakes/<slug>.json
```

`--dry-run` builds the prompt without calling the model, which is worth reading
once to see how much of the card is grounded in their samples versus generated
around them. Output lands in `data/voice_cards/<slug>.md`. It never overwrites
`03_voice_card.md`.

Then register them and point the tenant at the card:

```bash
python3 scripts/tenants.py --create <slug> --name "Their Name" --email them@example.com
```

Set `voice_card` in `~/.config/cowork/tenants/<slug>.json` to
`data/voice_cards/<slug>.md`, then dry-run the whole path before anything
reaches them:

```bash
python3 scripts/heartbeat.py --tenant <slug> --print --force
```

`--print` routes to stdout regardless of their configured channel, so a bad card
is caught by you rather than delivered to them.
