#!/usr/bin/env python3
"""Render launchd agents for THIS checkout, on THIS machine.

The plists beside this file hardcode /Users/adi and the path
~/Documents/Claude/karamel. That is fine for the machine they were written on
and wrong everywhere else, which makes them undeployable by anyone else: the
agents load, run, and fail to find a script that is not there.

This generates them instead, from the checkout this file sits in and the user
running it. It writes nothing outside ~/Library/LaunchAgents and it never loads
anything: loading is a decision, and an installer that starts scraping X on your
behalf because you ran a setup script is not one.

  python3 deploy/install.py --print          show what would be written
  python3 deploy/install.py                  write the plists, load nothing
  python3 deploy/install.py --owner jane      the tenant id that owns this box

After writing, load only what you want, one at a time:
  launchctl load ~/Library/LaunchAgents/com.karamel.heartbeat.plist
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / "scripts"
HOME = Path.home()
CONFIG = HOME / ".config" / "karamel"
AGENTS = HOME / "Library" / "LaunchAgents"

# label -> (script, args, schedule). schedule is either an interval in seconds
# or a list of (hour, minute) for calendar firing.
#
# The X-touching agents live in READING_AGENTS below rather than here. Their
# plists ARE generated now, because writing a file is not running one and this
# script has never loaded anything. What keeps them off is that ./karamel start
# consults the tenant's effective reply-mining state, which is the AND of their
# config and the allowlist module that is not distributed.
AGENTS_SPEC = {
    "heartbeat": ("heartbeat.py", ["--all"], [(9, 0), (15, 0)]),
    "drafter": ("drafter.py", [], 1200),
    "notifier": ("notifier.py", [], 1200),
    "inbox": ("inbox.py", ["--once"], 300),
    "weekly-digester": ("weekly_digester.py", [], [(18, 0)]),
    # Every tenant, not just the owner of the box. This is the loop that makes a
    # voice card sharpen toward what someone actually publishes; running it for
    # one person on a multi-tenant install means everybody else's answers are
    # recorded and never read.
    "reflector": ("reflector.py", ["--all"], [(17, 0)]),
    # The two agents that exist for a machine you cannot log into. The doctor
    # reports failures to the operator's own channel; without it, a break on
    # someone else's Mac looks exactly like a quiet week. The updater is how a
    # fix reaches them once it is written.
    "doctor": ("doctor.py", ["--watch"], 1800),
    "updater": ("updater.py", ["--quiet"], [(4, 30)]),
}

# The reading half. Generated so a tenant who has deliberately turned reply
# mining on has working plists, and inert for everyone else because nothing
# loads them. Chrome is here too: without it on the right port the listener's
# connect_over_cdp fails and set_halt() fires, which reads like enforcement.
READING_AGENTS = {
    # Ten minutes. The read cap, not the interval, is what bounds a day: 200
    # reads at up to 30 a run means the cap binds after roughly seven full runs
    # regardless of how often this fires. A shorter interval buys freshness, not
    # volume, which is the point of it: a reply is worth sending while the post
    # is still live.
    "listener": ("listener.py", [], 600),
    "evaluator": ("evaluator.py", [], 3600),
}

KEEPALIVE = {
    # Always up, so "is it working" is a bookmark rather than a command
    # somebody has to remember. Localhost only, read-only, costs nothing idle.
    "dashboard": ("dashboard.py", [], None),
    # poller_daemon does one long-poll pass and loops forever, so launchd keeps
    # it alive rather than scheduling it. Note it imports poller once and never
    # re-imports: a code change needs `launchctl kickstart -k`, not a git pull.
    "poller": ("poller_daemon.py", [], None),
}


def plist_for(label, script, args, schedule, owner):
    prog = ["/usr/bin/python3", str(SCRIPTS / script)] + list(args)
    d = {
        "Label": f"com.karamel.{label}",
        "ProgramArguments": prog,
        "WorkingDirectory": str(PROJECT),
        "EnvironmentVariables": {
            "PYTHONPATH": str(SCRIPTS),
            "KARAMEL_HOME": str(PROJECT),
            "KARAMEL_OWNER": owner,
        },
        "StandardOutPath": str(CONFIG / f"{label}.log"),
        "StandardErrorPath": str(CONFIG / f"{label}.err"),
        "RunAtLoad": False,
    }
    if schedule is None:
        d["KeepAlive"] = True
        d["RunAtLoad"] = True
        d["ThrottleInterval"] = 10
    elif isinstance(schedule, int):
        d["StartInterval"] = schedule
    else:
        d["StartCalendarInterval"] = [{"Hour": h, "Minute": m} for h, m in schedule]
    return d


def chrome_plist(owner):
    """The bot Chrome the listener attaches to over CDP.

    A dedicated --user-data-dir is mandatory rather than tidy: Chrome 136+
    refuses CDP on the default profile, and it keeps this out of the daily
    browser. Per-owner port and profile, because two tenants sharing one
    logged-in browser means one person's account posting the other's replies.

    Launches the binary directly. `open -na` returns immediately, so KeepAlive
    would respawn it forever.

    Nothing here handles credentials. Somebody logs into X in that profile once,
    by hand, before this is ever loaded."""
    port = 9222
    try:
        sys.path.insert(0, str(SCRIPTS))
        import tenants
        t = tenants.load_tenant(owner)
        if t:
            port = t.cdp_port
    except Exception:
        pass
    return {
        "Label": f"com.karamel.chrome-{owner}",
        "ProgramArguments": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={HOME / f'.karamel-chrome-{owner}'}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(CONFIG / f"chrome-{owner}.log"),
        "StandardErrorPath": str(CONFIG / f"chrome-{owner}.err"),
    }


def main():
    owner = "adi"
    if "--owner" in sys.argv:
        i = sys.argv.index("--owner")
        if i + 1 < len(sys.argv):
            owner = sys.argv[i + 1]
    dry = "--print" in sys.argv

    if not SCRIPTS.is_dir():
        print(f"no scripts/ beside {__file__}. Run this from inside a checkout.",
              file=sys.stderr)
        return 2

    print(f"checkout : {PROJECT}")
    print(f"user     : {os.environ.get('USER', HOME.name)}")
    print(f"owner    : {owner}")
    print(f"agents   : {AGENTS}")
    print()

    everything = {**AGENTS_SPEC, **READING_AGENTS, **KEEPALIVE}
    written = 0
    for label, (script, args, schedule) in everything.items():
        if not (SCRIPTS / script).exists():
            print(f"  skip {label}: {script} not in this checkout")
            continue
        d = plist_for(label, script, args, schedule, owner)
        target = AGENTS / f"com.karamel.{label}.plist"
        if dry:
            when = ("KeepAlive" if schedule is None else
                    f"every {schedule}s" if isinstance(schedule, int) else
                    " and ".join(f"{h:02d}:{m:02d}" for h, m in schedule))
            print(f"  would write {target.name:34s} {script:20s} {when}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            plistlib.dump(d, f)
        print(f"  wrote {target}")
        written += 1

    if dry:
        print("\ndry run, nothing written")
        return 0
    # After the dry-run check, never before it. Creating this as a side effect
    # of --print made an empty new config directory outrank the populated one
    # the running system was using.
    CONFIG.mkdir(parents=True, exist_ok=True)

    chrome = chrome_plist(owner)
    if not dry:
        with open(AGENTS / f"com.karamel.chrome-{owner}.plist", "wb") as f:
            plistlib.dump(chrome, f)
        written += 1
        print(f"  wrote {AGENTS / f'com.karamel.chrome-{owner}.plist'}")

    print(f"\n{written} agent(s) written, none loaded.")
    print("Load them one at a time, starting with the one that touches nothing:")
    print("  launchctl load ~/Library/LaunchAgents/com.karamel.heartbeat.plist")
    print("\nNothing will run usefully until ~/.config/karamel/ has credentials.")
    print("See SETUP.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
