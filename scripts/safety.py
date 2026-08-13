#!/usr/bin/env python3
"""Karamel safety invariants: platform-manipulation guardrails, enforced in code.

Cycle 3 of the pilot build. After the X platform-manipulation label, the safe
behavior can no longer be a policy line in a doc. It is enforced here:

  - Reply-at-scale is retired. Original content (gen.py) is the primary path;
    listener.py (X list scraping) is off the live path.
  - Replies are hard-capped per tenant per day (default 10). The gate fails
    CLOSED: if we cannot prove we are under the cap, the reply is blocked.
  - Intent signals inform what content to make and who to engage as a human.
    They never trigger automated outreach, and nothing reaches a real audience
    without the human approval step (enforced in the approval flow).

Multi-tenant from the first line (policy 1.4): every counter is keyed by tenant.

Cycle 4 (2026-08-07, doc 15). The retirement above still stands as the product
default: for a Karamel tenant, reply mining is off. What changed is that it is
no longer absolute. Adi's personal account is a named exception, so that turning
it back on for one operator does not quietly make the product's claim about
every other tenant false. Two rules keep that honest:

  - The exception is an explicit list of tenant ids, not a boolean. Widening it
    means adding a name, in a diff, that the selftest then fails on.
  - Membership is matched RAW, never slugged. _slug() collapses "../../adi" and
    "ADI/../" to "adi", which is correct for a filename and the wrong direction
    entirely for an allowlist.

Cycle 4 also wires the cap to something. Until now reply_allowed() had no
callers anywhere in the tree: the only thing actually stopping replies was
listener.py refusing to run. The gate is now called by notifier.py before a
batch goes out and record_reply() by poller.py when a human confirms a send.

CLI:
  --status [tenant]   show today's reply budget for a tenant
  --tick [tenant]     record one reply (bookkeeping; call after a human sends)
  --selftest          pure-logic + fail-closed tests, no persistent side effects
"""
import re
import sys

from karamel_common import CONFIG_DIR, now_et

MAX_REPLIES_PER_DAY = 10       # hard cap per tenant per day
REPLY_MINING_RETIRED = True    # PRODUCT default: X list scraping is off for a tenant
PRIMARY_PATH = "gen.py"        # original-first content is the default, not replies

# Named exceptions to the retirement above. Raw tenant ids, each of which must
# already equal its own _slug (asserted in selftest). Adding a name here is a
# product-policy change, not a config tweak.
#
# The names live in scripts/reply_mining_allowlist.py, which is deliberately NOT
# in the distribution manifest, because a hardcoded allowlist told every person
# who received a copy of this software the real identity of every other person
# using it. An install that does not have the file gets an empty set, which is
# the correct default for a product whose shipped answer is original content.
#
# The guarantee is unchanged: this is still a Python file on the box, not
# config, so a tenant record can never widen it. effective_reply_mining() is the
# AND of intent and this set.
try:
    from reply_mining_allowlist import TENANTS as _ALLOWED
except ImportError:
    _ALLOWED = ()
REPLY_MINING_TENANTS = frozenset(_ALLOWED)


def _today():
    return now_et().date().isoformat()


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(tenant):
    """Normalize a tenant id to a safe filename token: lowercase, only [a-z0-9-].
    Prevents path traversal and case-insensitive filesystem collisions, so tenant
    isolation (policy 1.4) cannot be broken by a crafted or case-variant id."""
    s = _SLUG_RE.sub("-", str(tenant).lower()).strip("-")
    return s or "unknown"


def _counter_path(tenant, date):
    return CONFIG_DIR / f"replies_{_slug(tenant)}_{date}.txt"


def _read_raw(tenant, date):
    """(count, ok). ok is False when the counter exists but is unreadable."""
    p = _counter_path(tenant, date)
    if not p.exists():
        return 0, True
    try:
        return int(p.read_text().strip()), True
    except (OSError, ValueError):
        return 0, False


def reply_count_today(tenant="adi"):
    """Replies recorded for this tenant today. Fails CLOSED: on an unreadable
    counter, returns the cap so the caller blocks rather than over-sends."""
    n, ok = _read_raw(tenant, _today())
    return n if ok else MAX_REPLIES_PER_DAY


def decide_reply(count, cap=MAX_REPLIES_PER_DAY):
    """Pure. (allowed, reason) given a count. Exactly `cap` replies per day."""
    if count >= cap:
        return False, f"reply cap reached ({count}/{cap}) today"
    return True, f"{count}/{cap} replies used today"


def reply_mining_allowed(tenant="adi"):
    """(allowed, reason). Is the listener -> drafter -> reply path open for this
    tenant at all? Separate from the daily cap: this is the on/off switch, the
    cap is the volume limit, and both have to pass.

    Matched RAW against REPLY_MINING_TENANTS, deliberately not slugged. _slug is
    lossy in the widening direction ("../../adi" -> "adi"), which is the correct
    property for a counter filename and a hole in an allowlist. Anything that is
    not the literal id fails closed."""
    if not REPLY_MINING_RETIRED:
        return True, "reply mining is not retired product-wide"
    if tenant in REPLY_MINING_TENANTS:
        return True, f"tenant '{tenant}' is a named exception to the retirement"
    return False, (
        f"reply mining is retired for tenant '{tenant}'; "
        f"primary path is {PRIMARY_PATH}"
    )


def reply_allowed(tenant="adi", cap=MAX_REPLIES_PER_DAY):
    """The gate. Both switches: the tenant must be permitted to reply-mine at
    all, and be under today's cap. Fails closed via reply_count_today."""
    mining_ok, why = reply_mining_allowed(tenant)
    if not mining_ok:
        return False, why
    return decide_reply(reply_count_today(tenant), cap)


def record_reply(tenant="adi"):
    """Increment the tenant's daily counter. Call only after a human actually
    sends a reply. Returns the new count."""
    date = _today()
    n, ok = _read_raw(tenant, date)
    new = (n if ok else 0) + 1
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _counter_path(tenant, date).write_text(str(new))
    return new


def selftest():
    global _read_raw

    # Pure cap decision: 0..9 allowed, the 11th attempt (count 10) is blocked.
    assert decide_reply(0)[0] is True
    assert decide_reply(9)[0] is True
    assert decide_reply(10)[0] is False
    assert decide_reply(11)[0] is False

    # Fail closed: an unreadable counter reads as the cap, so the gate blocks.
    _real = _read_raw
    _read_raw = lambda tenant, date: (0, False)
    try:
        assert reply_count_today("x") == MAX_REPLIES_PER_DAY
        assert reply_allowed("x")[0] is False
    finally:
        _read_raw = _real

    # Tenant ids are slugged: no path traversal, case-insensitive, safe filename.
    assert "/" not in _slug("a/b") and _slug("../../etc") == "etc"
    assert _slug("ADI") == _slug("adi") == "adi"
    assert _slug("..") == "unknown"
    assert ".." not in str(_counter_path("../../evil", "2026-07-06"))

    # Invariants are set to the safe values. Cycle 4 changed what this section
    # asserts, on purpose (doc 15 §6.2): the old line said the retirement was
    # absolute. It is not anymore. So the assertion now pins the exception
    # instead of being deleted, and widening the exception is what breaks it.
    assert REPLY_MINING_RETIRED is True and MAX_REPLIES_PER_DAY == 10

    # The product default is unchanged: an arbitrary tenant cannot reply-mine,
    # and the list growing by one named person does not soften that.
    assert reply_mining_allowed("karamel-tenant-1")[0] is False
    assert reply_allowed("karamel-tenant-1")[0] is False

    # No real tenant id appears below this line. These assertions ship to every
    # install, and naming the people on the list inside shipped source is the
    # disclosure the separate allowlist module exists to prevent. Everything is
    # derived from whatever the local list holds, so this passes both on a
    # machine that has one and on a fresh install that does not.
    if not REPLY_MINING_TENANTS:
        # The shipped default. Reply mining is off for everybody, including the
        # person running it, until they deliberately add the module.
        assert reply_mining_allowed("anybody-at-all")[0] is False
    else:
        # The pin lives in reply_mining_allowlist.py beside the names, so
        # widening the list still has to be written twice and still breaks a
        # test. That was always the point: adding a name is a product-policy
        # change, made in a diff, never in a config file.
        try:
            from reply_mining_allowlist import EXPECTED
            assert REPLY_MINING_TENANTS == frozenset(EXPECTED), \
                "exception list changed without updating its own pin"
        except ImportError:
            raise AssertionError(
                "reply_mining_allowlist.py must declare EXPECTED beside TENANTS")

        for tid in sorted(REPLY_MINING_TENANTS):
            assert reply_mining_allowed(tid)[0] is True, tid
            # Every listed id must already equal its own slug, or the raw match
            # and the filename would disagree about who this is.
            assert tid == _slug(tid), f"{tid!r} is not its own slug"

            # Matched RAW, so _slug's widening cannot leak into identity. Each
            # of these collides with a real id as a counter path and must still
            # be refused as a person.
            for imposter in (tid.upper(), f"../../{tid}", f"{tid} ",
                             f"{tid.upper()}/../", tid.capitalize()):
                if imposter == tid:
                    continue
                assert _slug(imposter) == tid, (imposter, _slug(imposter))
                assert reply_mining_allowed(imposter)[0] is False, imposter

            # A prefix of a listed id is a different person.
            if len(tid) > 3:
                assert reply_mining_allowed(tid[:-1])[0] is False, tid[:-1]

    # Every listed exception is already its own slug, so the identity we admit
    # and the counter file we then bill it to cannot drift apart.
    assert all(_slug(t) == t for t in REPLY_MINING_TENANTS)

    print("safety selftest: all assertions passed")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    tenant = positional[0] if positional else "adi"
    if "--tick" in sys.argv:
        print(f"recorded reply for {tenant}: now {record_reply(tenant)}")
        return 0
    allowed, reason = reply_allowed(tenant)
    mining, mining_why = reply_mining_allowed(tenant)
    print(f"tenant={tenant}  reply_allowed={allowed}  ({reason})")
    print(f"  reply_mining={mining}  ({mining_why})")
    print(
        f"reply_mining_retired={REPLY_MINING_RETIRED} (product default)  "
        f"exceptions={sorted(REPLY_MINING_TENANTS)}  primary_path={PRIMARY_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
