#!/usr/bin/env python3
"""Karamel updater: pull the new version and actually put it into service.

Shipping a fix is not the same as it running. Two things in this system make
that gap real, and both are handled here rather than left as folklore:

  1. `poller_daemon.py` does `import poller` once and then loops forever. A
     `git pull` changes the file on disk and nothing else; the running process
     keeps the old module in memory until it is restarted. A pull without a
     kickstart is a deploy that did not happen.
  2. The launchd plists hardcode absolute paths. If a release adds or renames a
     component, the plists have to be regenerated or the new one never runs.

Fast-forward only, and never on a dirty tree. Both are refusals rather than
merges: a conflict on someone else's Mac is a conflict they cannot resolve and
did not ask for, and it would leave them with neither the old working version
nor the new one.

  python3 updater.py --check     is there a new version, change nothing
  python3 updater.py             update, refresh agents, restart them
  python3 updater.py --quiet     same, but silent when already current (launchd)
  python3 updater.py --selftest  pure-logic tests, no network
"""
from __future__ import annotations

import subprocess
import sys

from shared import PROJECT, agent_labels

# Restarting these is the difference between a pull and a deploy. Anything that
# holds state in memory across fires belongs here. Named by component, not by
# label: the host runs these under the older com.cowork.* prefix, so a hardcoded
# label meant the one agent that MUST be restarted never was.
LONG_RUNNING = ("poller",)
TIMEOUT = 120


class UpdateError(RuntimeError):
    pass


def git(*args, cwd=None, strip=True):
    """Run git, returning stdout. Raises with git's own words, which are better
    than anything paraphrased here.

    strip=False for porcelain output: stripping eats the leading space of the
    FIRST status line only, so line[3:] dropped a character from the first
    filename and no others."""
    r = subprocess.run(["git", *args], cwd=str(cwd or PROJECT),
                       capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise UpdateError((r.stderr.strip() or r.stdout.strip() or
                           f"git {' '.join(args)} exited {r.returncode}"))
    return r.stdout.strip() if strip else r.stdout


def is_git_checkout():
    try:
        git("rev-parse", "--git-dir")
        return True
    except (UpdateError, OSError, subprocess.TimeoutExpired):
        return False


def dirty_files():
    """Tracked files with local edits. Untracked files are ignored: data and
    logs live in this tree, and they must not block a security fix."""
    out = git("status", "--porcelain", "--untracked-files=no", strip=False)
    return [l[3:] for l in out.splitlines() if l.strip()]


def parse_counts(out):
    """`git rev-list --left-right --count upstream...HEAD` -> (behind, ahead)."""
    parts = (out or "").split()
    if len(parts) != 2:
        raise UpdateError(f"could not read rev-list output: {out!r}")
    return int(parts[0]), int(parts[1])


def status():
    """(behind, ahead, branch). Fetches first, so it reflects the remote."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("fetch", "--quiet", "origin")
    upstream = f"origin/{branch}"
    behind, ahead = parse_counts(
        git("rev-list", "--left-right", "--count", f"{upstream}...HEAD"))
    return behind, ahead, branch


def changed_summary(old_rev, new_rev):
    try:
        return git("log", "--oneline", f"{old_rev}..{new_rev}")
    except UpdateError:
        return ""


def refresh_agents(owner):
    """Regenerate the plists, then restart what holds state in memory.

    kickstart -k is the operative flag. `launchctl load` on an already-loaded
    label is a no-op, which is why "I reloaded it" and "it is running the new
    code" have been two different things here before."""
    notes = []
    rc = subprocess.run(
        [sys.executable, str(PROJECT / "deploy" / "install.py"),
         "--owner", owner], capture_output=True, text=True, timeout=TIMEOUT)
    notes.append("plists refreshed" if rc.returncode == 0
                 else f"plist refresh failed: {rc.stderr.strip()[:120]}")
    for short in LONG_RUNNING:
        done = False
        misses = []
        for label in agent_labels(short):
            r = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"],
                capture_output=True, text=True, timeout=TIMEOUT)
            if r.returncode == 0:
                notes.append(f"{label} restarted")
                done = True
                break
            misses.append((label, (r.stderr or "").strip()))
        if not done:
            if all("No such process" in e for _, e in misses):
                notes.append(f"{short} not loaded under any known label, "
                             f"nothing to restart")
            else:
                detail = "; ".join(f"{l}: {e[:60]}" for l, e in misses if e)
                notes.append(f"{short} restart failed: {detail}")
    return notes


def _uid():
    import os
    return os.getuid()


def update(owner="adi", quiet=False, check_only=False):
    def out(msg):
        if not quiet:
            print(msg)

    if not is_git_checkout():
        print("This copy of Karamel was not installed from git, so it cannot "
              "update itself.\nAsk for a fresh copy, or reinstall with:\n"
              "  git clone <url> karamel", file=sys.stderr)
        return 2

    try:
        behind, ahead, branch = status()
    except (UpdateError, OSError, subprocess.TimeoutExpired) as e:
        print(f"could not reach the update server: {e}", file=sys.stderr)
        return 1

    if behind == 0:
        out(f"Already up to date ({branch}).")
        return 0

    out(f"{behind} new commit(s) on {branch}.")
    if check_only:
        out("Run ./karamel update to install them.")
        return 0

    if ahead:
        print(f"This copy has {ahead} local commit(s) the server does not have. "
              f"Not updating, because a fast-forward is impossible and merging "
              f"someone else's machine is not something to do unattended.",
              file=sys.stderr)
        return 1

    dirty = dirty_files()
    if dirty:
        print("Local edits would be overwritten, so nothing was changed:",
              file=sys.stderr)
        for f in dirty[:10]:
            print(f"  {f}", file=sys.stderr)
        print("Move or revert them, then run ./karamel update again.",
              file=sys.stderr)
        return 1

    old = git("rev-parse", "HEAD")
    try:
        git("merge", "--ff-only", f"origin/{branch}")
    except UpdateError as e:
        print(f"update failed, nothing changed: {e}", file=sys.stderr)
        return 1
    new = git("rev-parse", "HEAD")

    summary = changed_summary(old, new)
    if summary and not quiet:
        print("\nWhat changed:")
        for line in summary.splitlines()[:15]:
            print(f"  {line}")

    print("\nPutting it into service:" if not quiet else "", end="" if quiet else "\n")
    for note in refresh_agents(owner):
        out(f"  {note}")
    out(f"\nUpdated {old[:7]} -> {new[:7]}.")
    return 0


def selftest():
    assert parse_counts("3\t0") == (3, 0)
    assert parse_counts("0 0") == (0, 0)
    assert parse_counts("12   4") == (12, 4)
    for bad in ("", "5", "a b"):
        try:
            parse_counts(bad)
            raise AssertionError(f"should have refused {bad!r}")
        except (UpdateError, ValueError):
            pass

    # The daemon that caches its imports must be in the restart set, or every
    # update silently ships code that does not run.
    assert "poller" in LONG_RUNNING

    # Named by component, so both label prefixes are tried. The host runs the
    # older one, and a hardcoded label meant the restart silently did nothing.
    labels = agent_labels("poller")
    assert "com.karamel.poller" in labels and "com.cowork.poller" in labels, labels
    assert labels[0].startswith("com.karamel."), "new name must be preferred"

    print("selftest: all assertions passed")
    return True


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    import os
    owner = os.environ.get("KARAMEL_OWNER") or "adi"
    if "--owner" in sys.argv:
        i = sys.argv.index("--owner")
        if i + 1 < len(sys.argv):
            owner = sys.argv[i + 1]
    return update(owner=owner, quiet="--quiet" in sys.argv,
                  check_only="--check" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
