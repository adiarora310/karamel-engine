#!/usr/bin/env python3
"""Karamel evaluator: drain evaluator_queue.jsonl for jobs whose 24h window has
elapsed, scrape the posted reply's public metrics via CDP, write them into the
matching engagement.jsonl row.

X-touching: respects pause/halt (the /halt command stops this) and §11
mitigations (random jitter, 30s+ delay between fetches, cap per run, daily
read cap, login/challenge tripwire -> FULL HALT).

Flags: --print (scrape but don't persist), --force (ignore the daily read cap)
       --expire-stale (retire long-overdue jobs WITHOUT scraping; see below)
       --hours N (staleness threshold for --expire-stale, default 48)
       --dry-run (with --expire-stale: report what would change, write nothing)

On draining a queue that went stale. After a dormancy, every queued job is due
at once and the temptation is to let the evaluator run them off. Do not. The
result is written into engagement.jsonl as `metrics_24h`, so scraping a job that
came due weeks ago files today's cumulative totals under a name that says 24
hours. Nothing downstream can tell the difference, and the weekly digester and
voice-card refinements both read that field. `--expire-stale` retires those jobs
terminally and touches no browser.
"""
import random
import re
import sys
import time
from datetime import datetime, timezone

import tenants
from karamel_common import (
    add_reads, is_paused, load_config, now_iso, read_jsonl, reads_today,
    set_halt,
)
from shared import ENGAGEMENT, EVAL_QUEUE, write_jsonl_atomic

MAX_PER_RUN = 5  # §11: top 5 posted replies per run

# A job is scheduled 24h after the reply posts. Past this much *additional*
# lateness the number on the page is no longer a 24h number, so collecting it
# would mislabel rather than measure. One extra day of grace covers a laptop
# that slept through a window; anything beyond it is a dormancy, not a delay.
STALE_AFTER_HOURS = 48


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_count(label):
    """'47 Likes. Like' / '1,234 reposts' / '12.5K replies' -> int."""
    if not label:
        return None
    m = re.search(r"([\d,\.]+)\s*([KM]?)", label)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suf = m.group(2)
    if suf == "K":
        num *= 1_000
    elif suf == "M":
        num *= 1_000_000
    return int(num)


def aria(card, testid):
    try:
        return card.locator(f'button[data-testid="{testid}"]').first.get_attribute(
            "aria-label", timeout=2500
        )
    except Exception:
        return None


def scrape_metrics(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)
    u = page.url
    if "/login" in u or "/i/flow" in u:
        set_halt("login wall during evaluator scrape, X session expired")
        raise SystemExit(1)
    body = page.locator("body").inner_text()[:1500].lower()
    for trip in ("unusual activity", "verify it", "are you a robot"):
        if trip in body:
            set_halt(f"challenge page during evaluator: '{trip}'")
            raise SystemExit(1)
    if "this post was deleted" in body or "doesn't exist" in body or "doesn’t exist" in body:
        return {"deleted": True, "pulled_at": now_iso()}
    card = page.locator('article[data-testid="tweet"]').first
    return {
        "likes": parse_count(aria(card, "like") or aria(card, "unlike")),
        "replies": parse_count(aria(card, "reply")),
        "reposts": parse_count(aria(card, "retweet") or aria(card, "unretweet")),
        "pulled_at": now_iso(),
    }


def expire_stale(max_overdue_hours=STALE_AFTER_HOURS, dry_run=False):
    """Retire long-overdue queued jobs terminally, without touching X.

    Draining a stale queue by running it is not a neutral cleanup: the scrape
    lands in engagement.jsonl as `metrics_24h`, so a job weeks past due files
    cumulative totals under a 24-hour label. This marks them expired instead, so
    the queue empties, the evaluator can be re-enabled, and the dataset keeps
    saying only what it can actually support.
    """
    queue = read_jsonl(EVAL_QUEUE)
    now = datetime.now(timezone.utc)
    expired, kept, other = 0, 0, 0

    for row in queue:
        if row.get("status") != "queued":
            other += 1
            continue
        due = parse_iso(row.get("scheduled_for"))
        if due is None:
            row["status"] = "expired"
            row["expired_ts"] = now_iso()
            row["expired_reason"] = "unparseable scheduled_for"
            expired += 1
            print(f"expire draft #{row.get('draft_id')}: unparseable scheduled_for")
            continue
        overdue_h = (now - due).total_seconds() / 3600
        if overdue_h > max_overdue_hours:
            row["status"] = "expired"
            row["expired_ts"] = now_iso()
            row["expired_reason"] = (
                f"{overdue_h:.0f}h past due; a 24h metric cannot be recovered this late"
            )
            expired += 1
            print(f"expire draft #{row.get('draft_id')}: {overdue_h:.0f}h past due")
        else:
            kept += 1

    print(
        f"\n{expired} expired, {kept} still collectable, {other} already terminal "
        f"(threshold {max_overdue_hours}h past due)"
    )
    if dry_run:
        print("dry run: nothing written")
    elif expired:
        write_jsonl_atomic(EVAL_QUEUE, queue)
        print(f"wrote {EVAL_QUEUE}")
    else:
        print("nothing to expire, queue unchanged")
    return 0


def main():
    do_print = "--print" in sys.argv
    force = "--force" in sys.argv

    if "--expire-stale" in sys.argv:
        hours = STALE_AFTER_HOURS
        if "--hours" in sys.argv:
            hours = float(sys.argv[sys.argv.index("--hours") + 1])
        return expire_stale(hours, dry_run="--dry-run" in sys.argv)

    if is_paused():
        print("paused/halted, exiting")
        return 0

    cfg = load_config()
    # The owner's port, same source the listener and the bootstrap use. This
    # file had no tenant awareness at all and read cfg["cdp_port"], which is
    # 9222 unless karamel.json says otherwise, and nothing writes cdp_port
    # there. On any install whose bootstrap opened Chrome on a different port
    # this attaches to nothing, and the failure below calls set_halt(), which
    # is the flag that means a platform tripwire fired.
    owner = tenants.load_tenant(tenants.LEGACY_TENANT)
    cdp_port = (owner.cdp_port if owner else None) or cfg["cdp_port"]
    if not force and reads_today() >= cfg["max_reads_per_day"]:
        print("daily read cap reached, exiting")
        return 0

    queue = read_jsonl(EVAL_QUEUE)
    now = datetime.now(timezone.utc)
    due = [
        i for i, r in enumerate(queue)
        if r.get("status") == "queued"
        and parse_iso(r.get("scheduled_for")) is not None
        and parse_iso(r["scheduled_for"]) <= now
    ]
    if not due:
        print("no evaluator jobs due")
        return 0
    due = due[:MAX_PER_RUN]
    eng = read_jsonl(ENGAGEMENT)

    from playwright.sync_api import sync_playwright
    evaluated = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        except Exception as e:
            set_halt(f"CDP attach failed in evaluator: {e}")
            return 1
        ctx = browser.contexts[0]
        if ctx.pages:
            page, created = ctx.pages[0], False  # reuse existing tab (robust on CDP)
        else:
            page, created = ctx.new_page(), True
        try:
            time.sleep(random.uniform(0, 90))  # jitter
            for n, i in enumerate(due):
                row = queue[i]
                url = row.get("reply_url")
                if not url:
                    queue[i]["status"] = "skipped_no_url"
                    continue
                if n > 0:
                    time.sleep(random.uniform(30, 45))  # §11 between-fetch delay
                metrics = scrape_metrics(page, url)
                add_reads(1)
                for e in eng:
                    if e.get("draft_id") == row.get("draft_id") or (
                        e.get("tweet_id") == row.get("tweet_id") and e.get("reply_url") == url
                    ):
                        e["metrics_24h"] = metrics
                        break
                queue[i]["status"] = "done"
                queue[i]["done_ts"] = now_iso()
                evaluated += 1
                print(f"evaluated draft #{row.get('draft_id')}: {metrics}")
        finally:
            if created:
                page.close()

    if not do_print:
        write_jsonl_atomic(EVAL_QUEUE, queue)
        write_jsonl_atomic(ENGAGEMENT, eng)
    print(f"done: evaluated {evaluated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
