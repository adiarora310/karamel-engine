#!/usr/bin/env python3
"""Karamel listener v2.1: scrape a tenant's X list via Playwright CDP,
filter per spec, append candidates to that tenant's candidates.jsonl.

Runs under launchd every 20 min. v1.x §11 mitigations: random jitter,
30 tweets/run cap, 100 reads/day cap, tripwire FULL HALT on login wall or
challenge page, posting-window gate, pause-state check. Every one of those
stays as it was; cycle 4 reactivates this file without loosening any of them.

Gated by safety.reply_mining_allowed(): retired for Karamel tenants, open only
for the tenant ids named in safety.REPLY_MINING_TENANTS.

Flags: --force (skip window gate + jitter, for manual testing)
       --tenant ID (default "adi")
       --source following|list  (one run only, does not change the record)
       --list-id ID             (with --source list)
"""
import random
import re
import sys
import time
from datetime import datetime, timezone

import tenants
from karamel_common import (
    DATA, add_reads, append_jsonl, in_posting_window, is_paused, load_config,
    now_iso, read_jsonl, reads_today, set_halt,
)
from safety import reply_mining_allowed

CANDIDATES = DATA / "candidates.jsonl"   # legacy path; per-tenant resolves below


def candidates_path(tenant):
    return CANDIDATES if tenant.is_legacy else tenant.data_dir / "candidates.jsonl"


def parse_age_minutes(dt_attr):
    try:
        posted = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - posted).total_seconds() // 60)
    except (ValueError, AttributeError):
        return None


def parse_likes(card):
    try:
        label = card.locator('button[data-testid="like"]').first.get_attribute(
            "aria-label", timeout=2000
        )
        m = re.search(r"([\d,]+)", label or "")
        return int(m.group(1).replace(",", "")) if m else 0
    except Exception:
        return 0


def _has(card, selector):
    """Presence test that never raises. A changed attribute should read as
    'signal absent', not crash the run."""
    try:
        return card.locator(selector).count() > 0
    except Exception:
        return False


def read_signals(card):
    """Cheap, deterministic signals about a post, read from the DOM.

    These replace the curated list. On a Following timeline the list was the
    selection, so without one there is nothing between hygiene filters and an
    expensive model call. These are what can be known for free.

    via_repost is the load-bearing one. The Following tab shows posts from
    accounts you follow, PLUS reposts, and a repost surfaces an author you may
    not follow at all. That is the difference between "someone he chose to hear
    from" and "a stranger his network amplified", and it is the check worth
    having.

    verified is captured but deliberately weak. Since 2023 the badge is a paid
    subscription rather than a notability marker, so gating on it selects for
    people who pay for Premium and excludes plenty of serious engineers who do
    not. Recorded for scoring, never used as a gate.
    """
    return {
        "verified": _has(card, '[data-testid="icon-verified"]'),
        "via_repost": _has(card, '[data-testid="socialContext"]'),
        "is_reply": _has(card, 'div:has-text("Replying to")'),
    }


def classify(cand, tenant, cfg):
    """(tier, reason). 1 is a priority read, 2 normal, 3 needs to earn it.

    Tier 3 is not a rejection. It is 'this did not clear the cheap bar, so it
    only proceeds if something more expensive judges it relevant', which is
    where the scoring pass will hook in."""
    handle = cand["author_handle"].lower()
    if tenant.tier1_handles and handle in tenant.tier1_handles:
        return 1, "named priority handle"

    likes = cand.get("likes_at_discovery") or 0
    if cand.get("via_repost"):
        # Not someone he follows: his network amplified it. Needs to be
        # genuinely notable before it is worth his attention.
        if likes >= cfg["repost_min_likes"]:
            return 3, f"reposted stranger, {likes} likes, needs relevance"
        return 0, f"reposted stranger below {cfg['repost_min_likes']} likes"

    if cand.get("is_reply"):
        # A reply inside someone else's thread. Replying to a reply is a
        # conversation he is not in, and it reads that way.
        return 0, "reply in someone else's thread"

    if likes >= cfg["min_likes_non_tier1"]:
        return 2, f"followed account, {likes} likes"
    return 3, f"followed account, only {likes} likes, needs relevance"


def extract_card(card, tenant):
    """Return a candidate dict or (None, reason)."""
    # promoted content
    if card.locator('[data-testid="placementTracking"]').count() > 0:
        return None, "promoted"

    # status link: the anchor wrapping the <time> element
    try:
        link = card.locator("a:has(time)").first
        href = link.get_attribute("href", timeout=3000) or ""
        t = card.locator("time").first.get_attribute("datetime", timeout=3000)
    except Exception:
        return None, "no status link / repost shell"

    m = re.search(r"^/([^/]+)/status/(\d+)", href)
    if not m:
        return None, f"unparseable href {href}"
    handle, tweet_id = m.group(1), m.group(2)

    age = parse_age_minutes(t)
    if age is None:
        return None, "no timestamp"

    try:
        text = card.locator('div[data-testid="tweetText"]').first.inner_text(
            timeout=3000
        )
    except Exception:
        return None, "no text node (pure RT / media-only)"

    try:
        name = (
            card.locator('div[data-testid="User-Name"]')
            .first.inner_text(timeout=3000)
            .split("\n")[0]
        )
    except Exception:
        name = handle

    return {
        **read_signals(card),
        "tweet_id": tweet_id,
        "author_handle": handle,
        "author_name": name,
        "text": text,
        "url": f"https://x.com/{handle}/status/{tweet_id}",
        "age_minutes": age,
        "likes_at_discovery": parse_likes(card),
        "discovered_at": now_iso(),
    }, None


FOLLOWING_URL = "https://x.com/home"


def select_following(page):
    """Put the home timeline on the chronological Following tab, and prove it.

    x.com/home opens whichever tab the session last used, and the other one is
    "For You": ranked by engagement, so it surfaces whatever is most
    inflammatory that hour. Drafting replies off that is the exact failure this
    source exists to avoid, and it would look like the system working.

    Returns (ok, detail). Fails CLOSED: if the tab cannot be confirmed, the
    caller skips the run rather than reading an unknown timeline."""
    try:
        tabs = page.get_by_role("tab")
        n = min(tabs.count(), 6)
    except Exception as e:
        return False, f"no tablist on the home timeline: {type(e).__name__}"

    following = None
    for i in range(n):
        t = tabs.nth(i)
        try:
            label = (t.inner_text(timeout=2000) or "").strip().lower()
        except Exception:
            continue
        if label.startswith("following"):
            following = t
            break
    if following is None:
        return False, f"no Following tab among {n} tab(s)"

    try:
        if (following.get_attribute("aria-selected", timeout=2000) or "") != "true":
            following.click(timeout=5000)
            page.wait_for_timeout(3000)
        selected = following.get_attribute("aria-selected", timeout=2000)
    except Exception as e:
        return False, f"could not select Following: {type(e).__name__}"

    if selected != "true":
        return False, "Following tab did not become selected"
    return True, "Following"


def _flag(name, argv=None):
    """Value after a CLI flag, or None. Overrides the tenant record for one run
    only: nothing here writes config back."""
    argv = argv if argv is not None else sys.argv
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def tenant_arg(argv):
    """--tenant ID, defaulting to adi. Returned raw: the gate matches raw."""
    if "--tenant" in argv:
        i = argv.index("--tenant")
        if i + 1 < len(argv):
            return argv[i + 1]
    return "adi"


def main(tenant):
    force = "--force" in sys.argv
    cfg = load_config()

    # Everything the listener touches is now the tenant's: their list, their
    # Chrome, their budget, their halt state. The only shared value left is the
    # pacing config, which is behavioural rather than identifying.
    # A one-off run against the other source should not need a config edit.
    # Reading both the Following timeline and a curated list is a legitimate
    # thing to want on the same day, and editing the record between runs is how
    # somebody ends up leaving it on the wrong one.
    source = _flag("--source") or tenant.source
    if source not in ("following", "list"):
        print(f"--source must be following or list, got {source!r}",
              file=sys.stderr)
        return 2
    list_id = (_flag("--list-id") or tenant.list_id
               or (cfg["list_id"] if tenant.is_legacy else None))
    if source == "list" and not list_id:
        print(f"tenant '{tenant.id}' reads from a list but has no list_id. "
              f"Set one, or switch source to 'following'.", file=sys.stderr)
        return 2
    # The tenant's own port, always, because the tenant record is what the
    # bootstrap launched Chrome with. This used to fall back to cfg["cdp_port"]
    # for the legacy tenant, and since LEGACY_TENANT follows KARAMEL_OWNER,
    # EVERY person is legacy on their own Mac: tenant.cdp_port was dead code and
    # every install used the config default of 9222. That is right for a tenant
    # whose port is 9222 and wrong for everybody else. The second user's
    # bootstrap opens Chrome on 9223 and nothing writes cdp_port into
    # karamel.json, so his listener connected to an empty port, failed, and
    # set_halt() fired: the reading half dead on arrival, reported in the one
    # way that reads as platform enforcement rather than a misconfiguration.
    cdp_port = tenant.cdp_port or cfg["cdp_port"]
    cap = tenant.max_reads_per_day if not tenant.is_legacy else cfg["max_reads_per_day"]

    if is_paused(tenant):
        print(f"[{tenant.id}] paused/halted, exiting")
        return 0
    if not force and not in_posting_window(tenant=tenant):
        print(f"[{tenant.id}] outside posting window, exiting")
        return 0
    if reads_today(tenant) >= cap:
        print(f"[{tenant.id}] daily read cap {cap} reached, exiting")
        return 0
    if not force:
        jitter = random.randint(0, 240)
        print(f"jitter sleep {jitter}s")
        time.sleep(jitter)

    from playwright.sync_api import sync_playwright

    candidates = candidates_path(tenant)
    seen_ids = {r["tweet_id"] for r in read_jsonl(candidates)}
    new, filtered = 0, []

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        except Exception as e:
            # NOT a halt. A browser that is not running is infrastructure, and
            # doctor._INFRA_HALT already says so: it lists "CDP attach failed"
            # as the canonical example of a halt that is not enforcement.
            #
            # Halting for it is disproportionate twice over. It stops the
            # WRITING half, which never touches X, so a quit Chrome window
            # silently cancels the 09:00 draft. And set_halt writes the flag
            # that means a platform tripwire fired, so the operator reads
            # "TRIPWIRE HALT" and reasonably concludes the account was
            # actioned. Observed on the host tonight: a Playwright protocol
            # error left the whole system halted and labelled as enforcement.
            #
            # Failing loudly is enough. The agent exits non-zero, doctor's
            # watch sees it and emails, and the reading half resumes by itself
            # when the browser comes back.
            print(f"[{tenant.id}] cannot attach to Chrome on port {cdp_port}: "
                  f"{e}", file=sys.stderr)
            print(f"[{tenant.id}] the Karamel Chrome window is probably closed. "
                  f"Reopen it and this resumes on its own.", file=sys.stderr)
            return 1
        ctx = browser.contexts[0]
        page = ctx.new_page()
        try:
            target = (FOLLOWING_URL if source == "following"
                      else f"https://x.com/i/lists/{list_id}")
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)

            # tripwires
            url = page.url
            if "/login" in url or "flow" in url:
                set_halt("login wall on list fetch, X session expired",
                         tenant=tenant)
                return 1
            body = page.locator("body").inner_text()[:2000].lower()
            for trip in ("unusual activity", "verify it", "are you a robot"):
                if trip in body:
                    set_halt(f"challenge page detected: '{trip}'", tenant=tenant)
                    return 1

            if source == "following":
                ok, detail = select_following(page)
                if not ok:
                    # Not a halt: nothing suspicious happened, we simply cannot
                    # prove which timeline this is. Reading anyway is how "For
                    # You" content quietly enters the queue.
                    print(f"[{tenant.id}] skipping run, {detail}", file=sys.stderr)
                    return 0

            cards = page.locator('article[data-testid="tweet"]')
            n = min(cards.count(), cfg["max_tweets_per_run"])
            add_reads(n, tenant)

            for i in range(n):
                # human-like pacing between card reads
                time.sleep(random.uniform(0.3, 1.2))
                cand, reason = extract_card(cards.nth(i), tenant)
                if cand is None:
                    filtered.append(reason)
                    continue
                if tenant.handle and cand["author_handle"].lower() == tenant.handle:
                    # Their own post, in their own feed. Drafting a reply to it
                    # means replying to themselves, which only happens once you
                    # read a timeline instead of a list.
                    filtered.append("own post")
                    continue
                if cand["tweet_id"] in seen_ids:
                    filtered.append("already seen")
                    continue
                if cand["age_minutes"] > cfg["max_age_minutes"]:
                    filtered.append(f"too old ({cand['age_minutes']}m)")
                    continue
                tier, why = classify(cand, tenant, cfg)
                cand["tier"], cand["tier_reason"] = tier, why
                if tier == 0:
                    filtered.append(why)
                    continue
                if tier == 3 and not cfg["keep_needs_relevance"]:
                    # Nothing scores relevance yet, so tier 3 is held back
                    # rather than drafted. Flipping this on without a scorer
                    # means paying a model call for every marginal post.
                    filtered.append(why)
                    continue
                append_jsonl(candidates, cand)
                seen_ids.add(cand["tweet_id"])
                new += 1
        finally:
            page.close()

    print(
        f"[{tenant.id}] done ({source}): {new} new candidates, "
        f"{len(filtered)} filtered "
        f"({', '.join(filtered[:8])}{'...' if len(filtered) > 8 else ''})"
    )
    return 0


def selftest():
    # The CDP port must come from the tenant record, because that is what the
    # bootstrap launched Chrome with. This was `tenant.cdp_port if not
    # tenant.is_legacy else cfg["cdp_port"]`, and since LEGACY_TENANT follows
    # KARAMEL_OWNER every person is legacy on their own Mac: the record was
    # never read, every install used the 9222 default, and the second user's
    # bootstrap opens 9223. His listener attached to nothing and set_halt()
    # fired, which is the flag that means a platform tripwire tripped.
    import tenants as _t
    for port in (9222, 9223, 9333):
        fake = _t.Tenant({"id": "x", "cdp_port": port})
        assert (fake.cdp_port or 9222) == port, (port, fake.cdp_port)
    # And a record with no port still falls back rather than resolving to None.
    assert (_t.Tenant({"id": "x"}).cdp_port or 9222) == 9222
    from pathlib import Path as _P
    src = _P(__file__).read_text()
    assert 'cdp_port = tenant.cdp_port or cfg' in src, \
        "the port must not depend on which tenant happens to own the box"

    class FakeTab:
        def __init__(self, label, selected):
            self.label, self._sel, self.clicked = label, selected, False
        def inner_text(self, timeout=0):
            return self.label
        def get_attribute(self, name, timeout=0):
            return "true" if (self._sel or self.clicked) else "false"
        def click(self, timeout=0):
            self.clicked = True

    class FakeTabs:
        def __init__(self, tabs):
            self._t = tabs
        def count(self):
            return len(self._t)
        def nth(self, i):
            return self._t[i]

    class FakePage:
        def __init__(self, tabs):
            self._tabs = FakeTabs(tabs)
        def get_by_role(self, role):
            return self._tabs
        def wait_for_timeout(self, ms):
            pass

    # Already on Following: confirmed, nothing clicked.
    f = FakeTab("Following", True)
    ok, why = select_following(FakePage([FakeTab("For you", False), f]))
    assert ok and why == "Following", (ok, why)
    assert not f.clicked

    # On For You: switched, then confirmed.
    f2 = FakeTab("Following", False)
    ok, _ = select_following(FakePage([FakeTab("For you", True), f2]))
    assert ok and f2.clicked, "must select the Following tab"

    # No Following tab at all: refuse. Reading whatever is on screen is how
    # engagement-ranked content enters the queue looking like normal output.
    ok, why = select_following(FakePage([FakeTab("For you", True)]))
    assert not ok and "no Following tab" in why, why

    # A tab that will not become selected: refuse rather than read anyway.
    class Stuck(FakeTab):
        def get_attribute(self, name, timeout=0):
            return "false"
    ok, why = select_following(FakePage([Stuck("Following", False)]))
    assert not ok and "did not become selected" in why, why

    # A page with no tablist at all: refuse.
    class Bare:
        def get_by_role(self, role):
            raise RuntimeError("no tablist")
    ok, why = select_following(Bare())
    assert not ok and "no tablist" in why, why

    # --- classify: the selection logic that replaces the curated list --------
    cfg = {"min_likes_non_tier1": 5, "repost_min_likes": 150}

    class T:
        tier1_handles = set()

    def c(**kw):
        base = {"author_handle": "someone", "likes_at_discovery": 10,
                "via_repost": False, "is_reply": False, "verified": False}
        base.update(kw)
        return base

    # Someone he follows, posting normally, with any traction: normal read.
    assert classify(c(), T(), cfg)[0] == 2

    # Same person, barely any traction: not rejected, just needs a reason.
    assert classify(c(likes_at_discovery=1), T(), cfg)[0] == 3

    # A reply inside someone else's thread is a conversation he is not in.
    assert classify(c(is_reply=True), T(), cfg)[0] == 0

    # A repost puts a stranger in front of him. Ordinary traction is not enough.
    assert classify(c(via_repost=True, likes_at_discovery=100), T(), cfg)[0] == 0
    # Genuinely notable, so it earns a relevance judgment rather than a draft.
    assert classify(c(via_repost=True, likes_at_discovery=400), T(), cfg)[0] == 3

    # Verified is captured but must never decide anything on its own: the badge
    # is a paid subscription, not a notability marker.
    for v in (True, False):
        assert classify(c(verified=v), T(), cfg)[0] == 2
        assert classify(c(verified=v, is_reply=True), T(), cfg)[0] == 0

    # A named handle still overrides everything, including the repost bar.
    class Named:
        tier1_handles = {"someone"}
    assert classify(c(via_repost=True, likes_at_discovery=0), Named(), cfg)[0] == 1

    # Signals never raise, whatever the DOM does.
    class Exploding:
        def locator(self, sel):
            raise RuntimeError("selector gone")
    sig = read_signals(Exploding())
    assert sig == {"verified": False, "via_repost": False, "is_reply": False}, sig

    # The override is per run and never written back. Somebody testing the
    # other source once should not silently leave the record pointing at it.
    assert _flag("--source", ["x", "--source", "list"]) == "list"
    assert _flag("--source", ["x"]) is None
    assert _flag("--source", ["x", "--source"]) is None      # trailing flag
    assert _flag("--list-id", ["x", "--list-id", "123"]) == "123"

    print("listener selftest: all assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    # Cycle 3 stopped this file with a string every caller could type. Cycle 4
    # replaces that with the same gate the rest of the reply path consults, so
    # "is reply mining on" has one answer in one place instead of a flag here
    # and an invariant there. Still exit 3, still refuses by default: for a
    # Karamel tenant this scrapes an X list, the pattern that got the account
    # flagged, and gen.py remains the primary path.
    _tid = tenant_arg(sys.argv)
    _ok, _why = reply_mining_allowed(_tid)
    if not _ok:
        print(f"listener.py refused for tenant '{_tid}': {_why}", file=sys.stderr)
        sys.exit(3)
    _t = tenants.load_tenant(_tid)
    if _t is None:
        print(f"listener.py: no such tenant '{_tid}'", file=sys.stderr)
        sys.exit(2)
    # Both switches, same as the drafting path: the code-level allowlist above
    # and this tenant's own config. Either one off means no scraping.
    _cfg_ok, _cfg_why = _t.effective_reply_mining()
    if not _cfg_ok:
        print(f"listener.py refused for tenant '{_tid}': {_cfg_why}", file=sys.stderr)
        sys.exit(3)
    sys.exit(main(_t))
