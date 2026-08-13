# Karamel

Drafts posts in your voice, sends them to you, and learns from what you keep,
edit, or bin. **It never posts anything.** You do that.

```bash
./karamel setup
./karamel start
```

Then `./karamel doctor` if anything ever looks wrong: it names what is broken
and the exact command that fixes it.

Full instructions are in [SETUP.md](SETUP.md).

## Updating

```bash
./karamel update
```

Fast-forward only, and it refuses rather than merging if you have local edits,
so an update can never leave you with neither the old working version nor the
new one. It also runs itself once a day.

## What is not in this repo

Your voice card and your model key are yours. Neither is version controlled
here, so no update can overwrite them, and cloning this repo reveals nothing
about anyone using it.
