# Karamel

Drafts posts in your voice, sends them to you, and learns from what you keep,
edit, or bin. **It never posts anything.** You do that.

Everything runs on your own Mac. Nothing about you leaves it except the model
calls, and those go to Anthropic on your own Claude subscription.

---

## Part 1: the ten minute setup

### What you need

An always-on Mac, and Claude Code signed in on it. That is all. No API account,
no credits, no keys to paste.

Check the second one:

```bash
claude -p "reply with just: ok"
```

If that prints `ok`, you are ready. If it asks you to log in, run `claude` once
and sign in.

### Install

```bash
git clone <the repo URL you were sent> karamel && cd karamel
```

Put the voice card you were sent in `data/voice_cards/`.

Tell it to use your subscription rather than an API key:

```bash
mkdir -p ~/.config/karamel && echo '{"provider": "cli"}' > ~/.config/karamel/llm.json && chmod 600 ~/.config/karamel/llm.json
```

```bash
./karamel setup
```

Four questions. It checks the model works with a real call before asking you
anything else, finds the voice card and asks you to confirm it, and connects
Telegram or email. Telegram is quicker: no passwords, four taps in an app you
already have.

### See one before committing to anything

```bash
./karamel draft
```

One draft, printed, not sent. This takes a minute or two.

### Turn it on

```bash
./karamel start
```

Two drafts a day, at 09:00 and 15:00 your time.

---

## How a day works

Two drafts arrive on whichever channel you chose. You answer one of three ways:

| Reply | Meaning |
|---|---|
| `posted` | you published it as written |
| `edited` followed by your text | you published it, changed |
| `skip` | you binned it |

That answer is the whole training signal. Every fortnight the reflector reads
your edits, finds the patterns, and proposes specific changes to your voice
card. The card sharpens toward what you actually publish rather than what a
model guesses.

Some drafts will not arrive. A separate critic grades every one against your
card and only sends what passes. `./karamel draft` shows you the scores and the
exact phrases that cost it points, which is how you tune the card.

**`[VERIFY: ...]` in a draft is deliberate.** The writer is forbidden from
inventing a statistic, date or quote, so where a fresh specific belongs it
leaves a slot for you to fill. A draft with an unfilled slot cannot be marked
`posted`; reply `edited` with the finished text instead.

---

## Part 2: the reading half

**This part is optional, off by default, and needs a deliberate decision. Read
the whole section before turning it on.**

### What it does

It reads your X timeline through a Chrome window that is signed in as you,
finds posts worth replying to, and drafts replies for you to approve. Same rule
as everything else: it never posts. You send every reply by hand.

### The honest part

Reading X this way is not a sanctioned use of the platform. **The author of this
code had an X account labelled for platform manipulation doing exactly this.**
That label is not trivial to get removed and it affects the account's reach.

The design tries hard to stay under the thresholds that trigger it: caps on
reads per day, a cap of ten replies a day, posting windows, randomised timing,
and a tripwire that halts everything at the first sign of trouble. None of that
is a guarantee. It is one person's account risk, and it would be your account.

The original-content half in Part 1 carries none of this risk, which is why it
is the default and why this is not.

### If you want it anyway

**1. Two gates, both of which you have to open yourself.** Neither ships open.

Create the allowlist. It is a Python file rather than config on purpose:
turning this on should be a deliberate act, not an edit to a JSON file nobody
reviews.

```bash
cat > scripts/reply_mining_allowlist.py <<'EOF'
TENANTS = ("YOUR-TENANT-ID",)
EXPECTED = frozenset({"YOUR-TENANT-ID"})
EOF
```

Your tenant id is what you typed at "a short id for you" during setup, and
`./karamel status` prints it.

Then the config gate:

```bash
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.config/karamel/tenants/YOUR-TENANT-ID.json'
d = json.loads(p.read_text()); d['reply_mining'] = True
d['handle'] = 'YOUR-X-HANDLE-WITHOUT-THE-AT'
p.write_text(json.dumps(d, indent=2))
print('reply_mining on for', d['id'])
"
```

Check both agree:

```bash
PYTHONPATH=scripts python3 scripts/safety.py YOUR-TENANT-ID
```

Expect `reply_mining=True`. If either gate is shut it prints why.

**2. A signed-in Chrome, once, by hand.**

Karamel never handles your password. It attaches to a Chrome that is already
logged in. That Chrome must use its own profile directory, because Chrome 136
and later refuse remote debugging on the default profile, and because you do
not want your daily browser driven by anything.

```bash
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.karamel-chrome-YOUR-TENANT-ID" --no-first-run --no-default-browser-check
```

Log into X in that window. Leave it open and leave it signed in.

```bash
curl -s http://localhost:9222/json/version
```

If that answers, the listener can attach.

**3. One run by hand, and read the output.**

```bash
PYTHONPATH=scripts python3 scripts/listener.py --tenant YOUR-TENANT-ID --force
```

`--force` skips the timing jitter. It does not skip the caps, the tripwires or
the pause check.

| What you see | What it means |
|---|---|
| `done: N new candidates` with N above zero | working |
| `0 new candidates`, filter reasons full of `no status link` or `no text node` | X renamed something in its markup. Stop; the extractors need fixing before anything downstream is trustworthy |
| the run halts and writes a reason | read the reason. A CDP or Chrome error is a local fault. Anything about rate limits, restrictions or a challenge is the platform, and that is a stop-and-do-nothing situation |

**4. Turn it on.**

```bash
./karamel start
```

`start` reads both gates. If either is shut it says so and starts only the
writing half.

Consider running only the listener and the drafter for the first week, and the
notifier by hand. The reading half then generates evidence while nothing reaches
your phone, let alone X.

### Turning it off

Delete `scripts/reply_mining_allowlist.py`, or set `reply_mining` back to
`false`. Either one closes the gate on the next fire. Then:

```bash
./karamel stop && ./karamel start
```

---

## When something looks wrong

```bash
./karamel doctor
```

Nine checks, each with the exact command that fixes it. It changes nothing, on
purpose: this system's halt file doubles as the record of a platform tripwire,
and a diagnostic that tidies up destroys the evidence it was run to collect.

```bash
./karamel logs
```

What each component said when it last failed, newest first.

A watchdog also runs every thirty minutes and messages you when something new
breaks, with the fix in the message. It reports each problem once every six
hours at most, so a stuck component cannot train you to mute the channel.

---

## Updating

```bash
./karamel update
```

Fast forward only. It refuses rather than merging if you have local edits, so an
update can never leave you with neither the old working version nor the new one.
It also runs itself at 04:30 daily.

Your voice card and your config are not in the repo, so no update can overwrite
them.

---

## Stopping

```bash
./karamel stop
```

Unloads everything and sets a halt flag, so nothing runs even if an agent gets
reloaded. `./karamel start` resumes.

---

## What is where

| | |
|---|---|
| `~/.config/karamel/` | your config, credentials and counters |
| `data/voice_cards/` | your voice card |
| `data/tenants/<you>/` | your drafts, engagement log and reflections |
| `~/.config/karamel/*.err` | what broke |

Nothing in this repo is shared with anyone else running it. Your voice card is
not version controlled, so cloning this repo tells nobody anything about you.

---

## Limits, all enforced in code and counted on disk

Ten replies a day. A daily read cap. Posting windows. Every one of them fails
closed: if a counter cannot be read, the action is blocked rather than allowed.

**Nothing in here posts to X.** Not the drafter, not the listener, not the
notifier. Every reply and every post is sent by you, by hand, after you have
read it.
