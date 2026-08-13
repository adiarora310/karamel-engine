"""karamel poller daemon — long-running launchd-managed wrapper around poller.main().

Spec: v1.5 override of §12.1. Replaces the per-minute scheduled-task `vc-poller`
which had 15-30 min production latency. This daemon runs continuously, calling
`poller.main()` (one getUpdates long-poll cycle, timeout=20s) in a forever loop.

Restarts on crash via launchd KeepAlive. On per-iteration errors we log to
stderr (which launchd routes to ~/.config/karamel/poller.err) and sleep 5s
before retrying — do NOT exit (instruction: "don't exit").
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make scripts/ importable regardless of launchd's working dir.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import poller  # noqa: E402  uses poller.main + poller.LONG_POLL_TIMEOUT


ERROR_SLEEP_SECONDS = 5


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    # stdout → ~/.config/karamel/poller.log via launchd StandardOutPath
    print(f"[{stamp()}] {msg}", flush=True)


def log_err(msg: str) -> None:
    # stderr → ~/.config/karamel/poller.err via launchd StandardErrorPath
    print(f"[{stamp()}] {msg}", file=sys.stderr, flush=True)


def main() -> int:
    log(
        f"poller_daemon starting · pid={os.getpid()} · "
        f"LONG_POLL_TIMEOUT={poller.LONG_POLL_TIMEOUT}s · "
        f"cwd={os.getcwd()}"
    )
    iteration = 0
    while True:
        iteration += 1
        try:
            rc = poller.main()
            if rc not in (0, None):
                log_err(f"iter {iteration}: poller.main returned {rc}")
        except KeyboardInterrupt:
            log("KeyboardInterrupt — daemon exiting cleanly")
            return 0
        except SystemExit as e:
            # Spec: "don't exit". launchd KeepAlive would respawn us anyway,
            # but per instruction we log and continue in-process.
            log_err(f"iter {iteration}: poller raised SystemExit({e.code}); continuing")
            time.sleep(ERROR_SLEEP_SECONDS)
        except Exception as e:
            log_err(
                f"iter {iteration}: unhandled exception in poller.main: {e}\n"
                + traceback.format_exc()
            )
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
