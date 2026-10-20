#!/usr/bin/env python3
"""LinkedIn engagement, from LinkedIn's official analytics API and nothing else.

Per person, per post and per day: reactions, comments, reposts, impressions,
profile views a post drove, followers a post drove, and the follower count.
Read with the person's own OAuth consent. No password is ever asked for, no
browser is driven, and LinkedIn is never read any other way.

WHAT THE API GIVES, AND WHAT IT DOES NOT (checked against LinkedIn's docs,
2026-10-19, versions 202604 and later)

  memberCreatorPostAnalytics   scope r_member_postAnalytics
    q=me      all of the member's posts, DAILY or TOTAL
    q=entity  one post, by its share or ugcPost URN
    IMPRESSION, MEMBERS_REACHED, REACTION, COMMENT, RESHARE, POST_SAVE,
    POST_SEND, LINK_CLICKS, FOLLOWER_GAINED_FROM_CONTENT,
    PROFILE_VIEW_FROM_CONTENT. DAILY is not offered for MEMBERS_REACHED,
    LINK_CLICKS or the two *_FROM_CONTENT metrics. LinkedIn's own note: the
    q=me reaction, comment and reshare counts "are not consistent with UI".
  memberFollowersCount         scope r_member_profileAnalytics
    q=me lifetime total; q=dateRange daily new followers.

  NOT AVAILABLE: new connections (no member API returns them) and total
  profile views. What is recorded is profile views *driven by posts*, which is
  the number that says whether posting is working.

ACCESS, WHICH IS LINKEDIN'S TO GIVE

Both scopes belong to the Community Management API, a vetted product a LinkedIn
app has to apply for. Until the app holds it and each person has clicked Allow
once, this file records nothing from LinkedIn and says so. Nothing is lost in
the meantime: linkedin.py records every confirmed post from day one, and both
endpoints take a dateRange, so the first successful pull backfills.

The Development tier allows 100 calls per member per day; DAILY_CALL_BUDGET
keeps a pull inside it.

FILES
  ~/.config/karamel/linkedin_app.json        {"client_id", "client_secret",
                                              "redirect_uri", "version"} chmod 600
  ~/.config/karamel/tenants/<id>.linkedin_token.json   the person's token, chmod 600
  data/.../<tenant>/linkedin/engagement.jsonl          one row per metric reading

CLI:
  --auth-url ID          the consent link to send that person
  --exchange ID CODE     turn the code from the redirect into their token
  --pull [ID]            one pull now (all LinkedIn tenants if no id)
  --status               who has granted access, and what was last read
  --selftest             pure-logic tests, no network
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import tenants
from shared import CONFIG_DIR, append_jsonl, now_iso, read_jsonl

APP_CFG = CONFIG_DIR / "linkedin_app.json"
API = "https://api.linkedin.com/rest/"
AUTH = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPES = ("r_member_postAnalytics", "r_member_profileAnalytics")
DEFAULT_VERSION = "202610"

#: Under the Development tier's 100 per member per day, with room for a retry.
DAILY_CALL_BUDGET = 80

#: How far back a pull reads. A post is still collecting reactions for weeks.
LOOKBACK_DAYS = 30

DAILY_METRICS = ("IMPRESSION", "REACTION", "COMMENT", "RESHARE")
TOTAL_ONLY = ("MEMBERS_REACHED", "PROFILE_VIEW_FROM_CONTENT", "FOLLOWER_GAINED_FROM_CONTENT")
POST_METRICS = ("REACTION", "COMMENT", "RESHARE", "IMPRESSION",
                "PROFILE_VIEW_FROM_CONTENT", "FOLLOWER_GAINED_FROM_CONTENT")


class NotConnected(Exception):
    """No app config or no token yet. A state, not a failure."""


def token_path(tenant_id):
    return CONFIG_DIR / "tenants" / f"{tenant_id}.linkedin_token.json"


def _read_private(path):
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"{path} is readable by others. Run: chmod 600 {path}")
    return json.loads(path.read_text())


def _write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def app_config():
    cfg = _read_private(APP_CFG)
    if not cfg or not cfg.get("client_id") or not cfg.get("client_secret") \
            or not cfg.get("redirect_uri"):
        raise NotConnected(f"{APP_CFG} missing or incomplete: needs client_id, "
                           "client_secret and redirect_uri of a LinkedIn app that "
                           "has the Community Management API")
    cfg.setdefault("version", DEFAULT_VERSION)
    return cfg


def auth_url(tenant_id, cfg, state=None):
    """The link the person opens once. It asks LinkedIn for read access to
    their own analytics and nothing else: no posting, no messages, no network."""
    q = {"response_type": "code", "client_id": cfg["client_id"],
         "redirect_uri": cfg["redirect_uri"],
         "state": state or f"{tenant_id}.{secrets.token_urlsafe(8)}",
         "scope": " ".join(SCOPES)}
    return AUTH + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def exchange(tenant_id, code, cfg, post=None):
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"]}).encode()
    data = post(TOKEN, form) if post else json.load(urllib.request.urlopen(
        urllib.request.Request(TOKEN, data=form, headers={
            "Content-Type": "application/x-www-form-urlencoded"}), timeout=30))
    if "access_token" not in data:
        raise RuntimeError(f"LinkedIn refused the code: {data.get('error_description') or data}")
    tok = {"access_token": data["access_token"],
           "expires_at": (datetime.now(timezone.utc)
                          + timedelta(seconds=int(data.get("expires_in", 0)))).isoformat(),
           "scope": data.get("scope", ""), "granted_at": now_iso()}
    if data.get("refresh_token"):
        tok["refresh_token"] = data["refresh_token"]
    _write_private(token_path(tenant_id), tok)
    return tok


def load_token(tenant_id, now=None):
    tok = _read_private(token_path(tenant_id))
    if not tok:
        raise NotConnected("has not granted LinkedIn analytics access yet")
    now = now or datetime.now(timezone.utc)
    if datetime.fromisoformat(tok["expires_at"]) <= now:
        raise NotConnected("LinkedIn access expired; they need to click the consent link again")
    return tok


# --- the queries, in Rest.li 2.0's own syntax ----------------------------------


def _date(d):
    return f"(day:{d.day},month:{d.month},year:{d.year})"


def date_range(start, end):
    """Start inclusive, end exclusive, as LinkedIn defines it."""
    return f"(start:{_date(start)},end:{_date(end)})"


def entity_param(urn):
    """A share or ugcPost URN in the finder's tagged form. An activity URN is
    not queryable here, and is refused rather than sent to fail."""
    kind = urn.split(":")[2] if urn.count(":") >= 3 else ""
    if kind not in ("share", "ugcPost"):
        raise ValueError(f"not a queryable post URN: {urn!r}")
    tag = "share" if kind == "share" else "ugc"
    return f"({tag}:{urllib.parse.quote(urn, safe='')})"


def query(path, params):
    """Rest.li values carry their own parens, colons and commas, which must not
    be percent-encoded; plain values are encoded as usual."""
    parts = []
    for k, v in params.items():
        v = str(v)
        parts.append(f"{k}={v if v.startswith('(') else urllib.parse.quote(v, safe='')}")
    return API + path + "?" + "&".join(parts)


class Budget:
    def __init__(self, calls):
        self.left = calls

    def spend(self):
        if self.left <= 0:
            raise RuntimeError("daily call budget for this member spent")
        self.left -= 1


def _get(url, token, version, budget, opener=None):
    budget.spend()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Linkedin-Version": version,
        "X-Restli-Protocol-Version": "2.0.0"})
    if opener:
        return opener(req)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _metric(el):
    m = el.get("metricType")
    return m if isinstance(m, str) else next(iter((m or {}).values()), "")


def rows_from(resp, tenant_id, scope, extra=None):
    """One row per element. Each row says what it measured and over when."""
    out = []
    for el in (resp or {}).get("elements", []):
        dr = el.get("dateRange") or {}
        s = dr.get("start")
        row = {"ts": now_iso(), "tenant": tenant_id, "platform": "linkedin",
               "scope": scope,
               "metric": _metric(el) or ("FOLLOWERS" if "memberFollowersCount" in el else ""),
               "day": f"{s['year']:04d}-{s['month']:02d}-{s['day']:02d}" if s else None,
               "count": el.get("count", el.get("memberFollowersCount"))}
        row.update(extra or {})
        out.append(row)
    return out


def pull(tenant, today=None, opener=None, cfg=None):
    """Read everything for one person and append it. Returns rows written."""
    from linkedin import Lane
    lane = Lane(tenant)
    tok = load_token(tenant.id)
    cfg = cfg or app_config()
    v, token = cfg["version"], tok["access_token"]
    budget = Budget(DAILY_CALL_BUDGET)
    today = today or date.today()
    since = today - timedelta(days=LOOKBACK_DAYS)
    rng = date_range(since, today)
    rows = []

    def get(path, params):
        return _get(query(path, params), token, v, budget, opener)

    rows += rows_from(get("memberFollowersCount", {"q": "me"}), tenant.id, "account",
                      {"window": "lifetime"})
    rows += rows_from(get("memberFollowersCount", {"q": "dateRange", "dateRange": rng}),
                      tenant.id, "account", {"window": "daily_new"})
    for m in DAILY_METRICS:
        rows += rows_from(get("memberCreatorPostAnalytics",
                              {"q": "me", "queryType": m, "aggregation": "DAILY",
                               "dateRange": rng}), tenant.id, "account", {"window": "daily"})
    for m in TOTAL_ONLY:
        rows += rows_from(get("memberCreatorPostAnalytics",
                              {"q": "me", "queryType": m, "aggregation": "TOTAL",
                               "dateRange": rng}), tenant.id, "account",
                          {"window": f"{LOOKBACK_DAYS}d"})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    for post in read_jsonl(lane.posts_path):
        urn = post.get("urn") or ""
        if not urn or ":activity:" in urn or (post.get("posted_confirmed_at") or "") < cutoff:
            continue
        for m in POST_METRICS:
            try:
                rows += rows_from(get("memberCreatorPostAnalytics",
                                      {"q": "entity", "entity": entity_param(urn),
                                       "queryType": m, "aggregation": "TOTAL"}),
                                  tenant.id, "post",
                                  {"window": "lifetime", "urn": urn,
                                   "draft_id": post.get("draft_id")})
            except RuntimeError:
                break
    path = lane.dir / "engagement.jsonl"
    for r in rows:
        append_jsonl(path, r)
    return len(rows)


def pull_all(only=None):
    from linkedin import lanes_with_linkedin
    for t in lanes_with_linkedin():
        if only and t.id != only:
            continue
        try:
            print(f"[{t.id}] linkedin analytics: {pull(t)} reading(s)")
        except NotConnected as e:
            print(f"[{t.id}] linkedin analytics: waiting, {e}")
        except Exception as e:
            print(f"[{t.id}] linkedin analytics failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
    return 0


def status():
    from linkedin import Lane, lanes_with_linkedin
    try:
        app_config()
        app = "configured"
    except NotConnected as e:
        app = f"not configured ({e})"
    print(f"LinkedIn app: {app}")
    for t in lanes_with_linkedin():
        lane = Lane(t)
        try:
            tok = load_token(t.id)
            access = f"granted, expires {tok['expires_at'][:10]}"
        except NotConnected as e:
            access = str(e)
        rows = read_jsonl(lane.dir / "engagement.jsonl")
        posts = read_jsonl(lane.posts_path)
        last = rows[-1]["ts"][:16] if rows else "never"
        print(f"  {t.id}: {access}; {len(posts)} post(s) recorded, "
              f"{len(rows)} reading(s), last read {last}")
    return 0


def selftest():
    import tempfile
    d0, d1 = date(2026, 10, 1), date(2026, 10, 19)
    assert date_range(d0, d1) == "(start:(day:1,month:10,year:2026),end:(day:19,month:10,year:2026))"
    assert entity_param("urn:li:share:7325786486870552578") == \
        "(share:urn%3Ali%3Ashare%3A7325786486870552578)"
    assert entity_param("urn:li:ugcPost:123") == "(ugc:urn%3Ali%3AugcPost%3A123)"
    for bad in ("urn:li:activity:1", "", "nonsense"):
        try:
            entity_param(bad)
            raise AssertionError(f"{bad!r} must be refused")
        except ValueError:
            pass
    q = query("memberCreatorPostAnalytics", {"q": "me", "queryType": "REACTION",
                                             "aggregation": "DAILY",
                                             "dateRange": date_range(d0, d1)})
    assert q == ("https://api.linkedin.com/rest/memberCreatorPostAnalytics?q=me"
                 "&queryType=REACTION&aggregation=DAILY&dateRange=(start:(day:1,month:10,"
                 "year:2026),end:(day:19,month:10,year:2026))"), q
    url = auth_url("someone", {"client_id": "abc", "redirect_uri": "https://x.test/cb"}, state="s")
    assert "scope=r_member_postAnalytics%20r_member_profileAnalytics" in url, url
    assert "w_member_social" not in url and "r_member_social" not in url

    # Both response shapes LinkedIn has used for metricType.
    old = {"elements": [{"count": 10, "metricType": {
        "com.linkedin.adsexternalapi.memberanalytics.v1.CreatorPostAnalyticsMetricTypeV1": "REACTION"},
        "dateRange": {"start": {"year": 2026, "month": 10, "day": 4}}}]}
    new = {"elements": [{"count": 3, "metricType": "COMMENT"}]}
    fol = {"elements": [{"memberFollowersCount": 1730}]}
    r = rows_from(old, "k", "account") + rows_from(new, "k", "post") + rows_from(fol, "k", "account")
    assert [(x["metric"], x["day"], x["count"]) for x in r] == [
        ("REACTION", "2026-10-04", 10), ("COMMENT", None, 3), ("FOLLOWERS", None, 1730)], r

    b = Budget(2)
    b.spend(); b.spend()
    try:
        b.spend()
        raise AssertionError("budget must stop the third call")
    except RuntimeError:
        pass

    # Tokens are written private and refused if anyone else can read them.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.json"
        _write_private(p, {"access_token": "x"})
        assert p.stat().st_mode & 0o777 == 0o600
        os.chmod(p, 0o644)
        try:
            _read_private(p)
            raise AssertionError("a readable token file must be refused")
        except RuntimeError:
            pass

    # A full pull against a fake LinkedIn: every call is a GET with the version
    # header, activity URNs are skipped, and the budget is respected.
    import linkedin
    with tempfile.TemporaryDirectory() as d:
        t = tenants.Tenant({"id": "li-test", "linkedin": {"enabled": True}})
        lane = linkedin.Lane(t)
        _orig_dd = type(t).data_dir
        type(t).data_dir = property(lambda self: Path(d))
        recent = datetime.now(timezone.utc).isoformat()
        for urn in ("urn:li:share:111", "urn:li:activity:222"):
            append_jsonl(lane.posts_path, {"draft_id": 1, "urn": urn,
                                           "posted_confirmed_at": recent})
        global load_token
        real = load_token
        load_token = lambda tid, now=None: {"access_token": "tok", "expires_at": "2999-01-01T00:00:00+00:00"}
        seen = []

        def opener(req):
            seen.append(req)
            assert req.get_method() == "GET"
            assert req.headers["Linkedin-version"] == "202610"
            return {"elements": [{"count": 1, "metricType": "REACTION"}]}
        try:
            n = pull(t, today=d1, opener=opener, cfg={"version": "202610"})
        finally:
            load_token = real
            type(t).data_dir = _orig_dd
        urls = [r.full_url for r in seen]
        assert len(urls) == 2 + 4 + 3 + 6, len(urls)
        assert not any("222" in u for u in urls), "an activity URN must not be queried"
        assert sum("share%3A111" in u for u in urls) == 6
        assert n == len(urls)
        assert len(read_jsonl(Path(d) / "linkedin" / "engagement.jsonl")) == n
    print("linkedin_analytics selftest: ok")
    return 0


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--status" in a:
        return status()
    if "--auth-url" in a:
        print(auth_url(a[a.index("--auth-url") + 1], app_config()))
        return 0
    if "--exchange" in a:
        i = a.index("--exchange")
        tok = exchange(a[i + 1], a[i + 2], app_config())
        print(f"granted, expires {tok['expires_at'][:10]}, scope {tok['scope']}")
        return 0
    if "--pull" in a:
        i = a.index("--pull")
        return pull_all(a[i + 1] if len(a) > i + 1 and not a[i + 1].startswith("--") else None)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
