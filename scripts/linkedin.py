#!/usr/bin/env python3
"""The LinkedIn lane: drafts written for LinkedIn, emailed apart from X drafts.

Everything else in this engine was built for X: a 280-character ceiling, X
wording in the maker's brief, topics invented from the voice card. A LinkedIn
post is a different artefact. It is read by a different audience, it is longer,
it is judged on whether the first two lines earn the "see more", and it is where
one pilot has a large enough network for engagement to mean something.

So this is a lane of its own, in its own file, beside the X path rather than
threaded through it. It reuses the parts that are not about X: the critic's
grading and deterministic vetoes, the refine loop, the emotion palette, the
email credentials. It writes nothing the X path reads, and the X path writes
nothing it reads.

WHAT IT DOES, ONCE PER DUE HOUR PER PERSON

1. Reads public news feeds in that person's space and keeps what is fresh and
   not already used. Public RSS, never LinkedIn: this lane does not read
   LinkedIn at all, signed in or otherwise.
2. Asks one model call to pick the item this person would actually have a
   position on, and to state that position in a line. Nothing fits, nothing is
   drafted: a generic summary is the thing this lane exists to not send.
3. Writes the post against the person's voice card, with the item's facts as
   the only facts it may use, what we know about any named person in it, and
   the changes this person has asked for on earlier drafts.
4. Grades it with the engine's critic, plus LinkedIn's own refusals (a link in
   the body, hashtag soup, a pitch for an opener, engagement bait for a
   closer), and sends a failing draft back with the reason, twice at most.
5. Emails it with "[LinkedIn]" first in the subject.

AND BETWEEN THOSE

The reply loop. Someone answers a LinkedIn draft email with "skip", "posted"
plus the link, "edited:" plus their own version, or just a sentence about what
to change. A sentence about what to change comes back as a revised draft in the
same thread, and every change is kept: the next draft's brief carries this
person's recent edits so they are applied without being asked twice.

STANCE, AND WHAT IT IS NOT

Every draft takes a position on something current. That is not the instruction
this engine removed on 2026-10-17 ("LAND A TAKE", which produced the same closed
three-beat ruling on every post). The position is about a specific item, it may
be held as a disagreement, an admission or a real question, and the three-beat
shape is still refused. What is required is that a reader can say what the
person thinks; how it is said stays open.

THREE RULES WORTH NOT LOSING

1. The person publishes. Nothing here posts, schedules or holds a posting
   credential. The email carries a link that opens LinkedIn's composer with the
   text filled in, and nothing goes out until they press Post.
2. No LinkedIn reading. Engagement comes from LinkedIn's official analytics API
   (linkedin_analytics.py) under the person's own consent, or not at all.
3. "[Karamel]" stays out of these subjects. inbox.py searches for it, and a
   LinkedIn reply it fetched would be a reply it could only misread.

CLI:
  --tick                    what the agent runs: replies, any due drafts, the
                            daily analytics pull
  --run ID [--force] [--dry-run]   draft for one tenant now
  --inbox [--dry-run]       one pass over LinkedIn replies
  --enable ID 'JSON'        switch the lane on for a tenant, merging JSON
  --prepare ID 'JSON'       the same, for someone with no email yet: stored,
                            and inert until their record can deliver
  --load-agent              write the plists and load the LinkedIn agent
  --person ID "Name" "cares about" ["relationship"]
                            add someone whose interests a draft should use
  --sample FILE             draft once for the tenant record in FILE and print
                            it; sends nothing, writes nothing
  --status                  each tenant's lane, next send hours and counts
  --selftest                pure-logic tests, no network, no model
"""
from __future__ import annotations

import csv
import email
import html
import imaplib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import critic
import tenants
from shared import CONFIG_DIR, append_jsonl, now_iso, read_jsonl, write_jsonl_atomic

# --- the numbers ---------------------------------------------------------------

#: Short and tight, and still a LinkedIn post. The feed shows roughly the first
#: two lines before "see more", so the first 200 characters carry the post and
#: the rest earns its place or goes.
DEFAULT_MAX_CHARS = 700

#: Every third hour of the posting window, counted from its start: 07, 10, 13,
#: 16, 19 on the default window. A pilot, 2026-10-19: hourly "looks like a bot".
DEFAULT_EVERY_HOURS = 3

#: A news item older than this is history, not something to have a view on.
ITEM_MAX_AGE_H = 72

#: An item needs this much summary to be written about. Below it the maker has
#: a headline and nothing else, and fills the gap: on 2026-10-20 a dry run for
#: one pilot, given only a headline in which a co-founder called a protocol
#: imperfect, wrote what his company "plans to support", which no text it was
#: given says. A plausible invented detail in someone's own name.
MIN_SUMMARY_CHARS = 120

#: Rounds of critic feedback before a draft is dropped rather than sent.
MAX_ROUNDS = 2

SUBJECT_MARKER = "[LinkedIn]"

#: How a draft is named in the body. The "L" is load-bearing: inbox.py reads
#: "#<10+ digits>" as an X draft id, and this must never look like one.
DRAFT_ID_RE = re.compile(r"#L(\d{10,})")

STATE = CONFIG_DIR / "linkedin_state.json"

#: Public feeds, checked live on 2026-10-19: each served stories dated that day.
#: Two candidates were dropped on the same check (Axios Pro, empty; VentureBeat's
#: feed, last item 3 September). A tenant can name its own with "sources".
#:
#: Rechecked 2026-10-20 for something the first check missed: whether an item
#: carries a summary at all. Sifted's 24 items were all headline-only, and
#: Hacker News's "summary" is a link and a comment count, so both were removed;
#: Ars Technica's AI feed was tested and left out for the same reason. GitHub's
#: changelog was left out on purpose: a pilot's card forbids speaking for
#: GitHub, and the picker would happily choose its product news.
SOURCE_SETS = {
    "startups_vc_deeptech": [
        ("TechCrunch Venture", "https://techcrunch.com/category/venture/feed/"),
        ("TechCrunch Startups", "https://techcrunch.com/category/startups/feed/"),
        ("Crunchbase News", "https://news.crunchbase.com/feed/"),
        ("The Robot Report", "https://www.therobotreport.com/feed/"),
        ("IEEE Spectrum Robotics", "https://spectrum.ieee.org/feeds/topic/robotics.rss"),
        ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ],
    "devtools_ai": [
        ("The New Stack", "https://thenewstack.io/feed/"),
        ("InfoQ", "https://feed.infoq.com/"),
        ("Simon Willison", "https://simonwillison.net/atom/everything/"),
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("Stratechery", "https://stratechery.com/feed/"),
        ("LeadDev", "https://leaddev.com/rss.xml"),
        ("The Pragmatic Engineer", "https://newsletter.pragmaticengineer.com/feed"),
    ],
    # Checked 2026-10-20 over a one-week window for items with a real summary:
    # HBR's feed returned nothing, and Kellogg, McKinsey, Gallup and Stern's own
    # news had no summarised item that week, so they are not here.
    "management_comms": [
        ("Fast Company Work Life", "https://www.fastcompany.com/work-life/rss"),
        ("PR Daily", "https://www.prdaily.com/feed/"),
        ("Ragan", "https://www.ragan.com/feed/"),
        ("Poets & Quants", "https://poetsandquants.com/feed/"),
        ("Knowledge at Wharton", "https://knowledge.wharton.upenn.edu/feed/"),
        ("MIT Sloan Management Review", "https://sloanreview.mit.edu/feed/"),
        ("Nielsen Norman Group", "https://www.nngroup.com/feed/rss/"),
        ("Behavioral Scientist", "https://behavioralscientist.org/feed/"),
    ],
    "founders_ai": [
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("TechCrunch Startups", "https://techcrunch.com/category/startups/feed/"),
        ("Stratechery", "https://stratechery.com/feed/"),
        ("Crunchbase News", "https://news.crunchbase.com/feed/"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ],
}

EM_DASH = re.compile(r"\s*[—–]\s*")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- the lane's config ---------------------------------------------------------


class Lane:
    """One person's LinkedIn lane, read off the "linkedin" object in their
    tenant record. Absent, or enabled false, means off.

    Read from the raw record rather than added to tenants.Tenant, so this lane
    can ship without a change to the file every other component imports.

      "linkedin": {
        "enabled": true,
        "profile_url": "https://www.linkedin.com/in/...",
        "space": "startup ecosystem, VC, deep tech, entrepreneurship",
        "source_set": "startups_vc_deeptech",     or "sources": [[name, url], ...]
        "lanes": ["Deep tech and physical AI", "Founder craft", ...],
        "every_hours": 3, "drafts_per_run": 1, "max_chars": 700,
        "email": "optional, else the tenant's email channel"
      }
    """

    def __init__(self, tenant):
        raw = (getattr(tenant, "_raw", None) or {}).get("linkedin") or {}
        self.tenant = tenant
        self.enabled = bool(raw.get("enabled"))
        self.profile_url = raw.get("profile_url") or ""
        self.space = (raw.get("space") or "").strip()
        self.every_hours = max(1, int(raw.get("every_hours") or DEFAULT_EVERY_HOURS))
        self.drafts_per_run = max(1, int(raw.get("drafts_per_run") or 1))
        self.max_chars = int(raw.get("max_chars") or DEFAULT_MAX_CHARS)
        named = raw.get("sources")
        if named:
            self.sources = [(str(n), str(u)) for n, u in named]
        else:
            self.sources = list(SOURCE_SETS.get(raw.get("source_set") or "", []))
        self.lanes = [str(x).strip() for x in (raw.get("lanes")
                      or getattr(tenant, "lanes", None)
                      or (getattr(tenant, "_raw", {}) or {}).get("lanes") or [])
                      if str(x).strip()]
        # Feeling, per person. require_feeling makes a felt reaction a hard
        # gate rather than guidance; feeling_words are this person's own words
        # for it, so a voice whose feeling is "optimistic, realistically" is
        # not graded on someone else's "I love it"; no_exclamation is for a
        # register that never uses one. Kept in the record, not in code, so
        # nobody's vocabulary ships in the package.
        self.require_feeling = bool(raw.get("require_feeling"))
        self.feeling_words = [str(w).strip().lower() for w in (raw.get("feeling_words") or [])
                              if str(w).strip()]
        self.no_exclamation = bool(raw.get("no_exclamation"))
        # The opposite case: a voice whose celebratory posts really do carry
        # one, so for them it counts as a feeling. Never both.
        self.exclamation_is_feeling = (bool(raw.get("exclamation_is_feeling"))
                                       and not self.no_exclamation)
        # A weekly send instead of every few hours: {"weekday": "sun", "hour": 9},
        # on the person's own clock. It bypasses the posting window, whose
        # default leaves Sunday open only from 16:00.
        sched = raw.get("schedule") or None
        if sched:
            day = str(sched.get("weekday", "sun")).lower()[:3]
            if day not in WEEKDAYS:
                raise ValueError(f"tenant {tenant.id!r}: schedule.weekday {day!r}")
            sched = {"weekday": day, "hour": int(sched.get("hour", 9))}
        self.schedule = sched
        # Every draft of a run in one email, numbered, instead of one each.
        self.digest = bool(raw.get("digest"))
        # {"amplify": 80, "own": 20}: crediting people from their roster versus
        # their own take on the news, held as a ratio across runs.
        mix = raw.get("mix") or None
        self.mix = ({"amplify": int(mix.get("amplify", 0)), "own": int(mix.get("own", 0))}
                    if mix else None)
        self.roster_path = Path(raw["roster"]) if raw.get("roster") else (
            Path(tenant.data_dir) / "roster.csv")
        # Subjects to keep out of their feed, matched like the app's banned
        # zones: a stem reaches its inflections ("politic" -> "politics").
        self.banned_zones = [str(z).strip().lower() for z in (raw.get("banned_zones") or [])
                             if str(z).strip()]
        # A weekly reader can be offered anything from the past week.
        self.max_age_h = int(raw.get("max_age_hours")
                             or (24 * 7 if sched else ITEM_MAX_AGE_H))
        ch = getattr(tenant, "channel", None) or {}
        self.email = raw.get("email") or (ch.get("address") if ch.get("type") == "email" else "")

    @property
    def dir(self):
        return Path(self.tenant.data_dir) / "linkedin"

    drafts_path = property(lambda self: self.dir / "drafts.jsonl")
    generated_path = property(lambda self: self.dir / "generated.jsonl")
    critiques_path = property(lambda self: self.dir / "critiques.jsonl")
    edits_path = property(lambda self: self.dir / "edits.jsonl")
    posts_path = property(lambda self: self.dir / "posts.jsonl")
    seen_path = property(lambda self: self.dir / "seen_items.jsonl")

    @property
    def people_path(self):
        """Who matters to this person and what each cares about. Data the user
        gives us, one {"name", "cares_about", "relationship"} per line. Shared
        with any other lane that wants it, so it sits beside, not inside, the
        LinkedIn directory."""
        return Path(self.tenant.data_dir) / "people.jsonl"

    def due(self, now=None):
        """Is this hour one of theirs? Their posting window, every Nth hour of
        it from the start, on THEIR clock. Same rule as Tenant.due, applied to
        this lane's own interval, so the two lanes can run at different speeds.

        It read New York time until 2026-10-20, which was right for the first
        two people and wrong for someone in Berlin: his 08:00 would have been
        a New York 08:00, the middle of his afternoon."""
        from karamel_common import in_posting_window
        here = (now or self.tenant.now()).astimezone(self.tenant.tz)
        if self.schedule:
            return (WEEKDAYS[here.weekday()] == self.schedule["weekday"]
                    and here.hour == self.schedule["hour"])
        if not in_posting_window(here, tenant=self.tenant):
            return False
        start = int((getattr(self.tenant, "posting_window", None) or {}).get("start", 7))
        return (here.hour - start) % self.every_hours == 0

    def send_hours(self):
        if self.schedule:
            return [self.schedule["hour"]]
        w = getattr(self.tenant, "posting_window", None) or {"start": 7, "end": 21}
        s, e = int(w["start"]), int(w["end"])
        return [h for h in range(s, e) if (h - s) % self.every_hours == 0]


def lanes_with_linkedin():
    return [t for t in tenants.list_tenants() if Lane(t).enabled]


# --- news ----------------------------------------------------------------------


_TAG = re.compile(r"<[^>]+>")


_FEED_BOILERPLATE = re.compile(
    r"(Article URL|Comments URL):\s*\S+|Points:\s*\d+|#\s*Comments:\s*\d+", re.I)


def _text(s):
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def _summary(s):
    """The item's own words, with a feed's link-and-count boilerplate removed:
    Hacker News's "summary" is nothing else, and it is long enough to pass for
    a real one."""
    return " ".join(_FEED_BOILERPLATE.sub(" ", _text(s)).split())


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _when(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_text, source):
    """RSS 2.0 or Atom, as plain dicts. Malformed input gives an empty list
    rather than an exception: one broken feed must not cost the other six."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif name in ("title", "link", "description", "summary", "content",
                          "pubdate", "published", "updated"):
                fields.setdefault(name, (child.text or "").strip())
        link = fields.get("link", "")
        title = _text(fields.get("title"))
        if not title or not link.startswith(("http://", "https://")):
            continue
        out.append({
            "source": source,
            "title": title,
            "link": link,
            "summary": _summary(fields.get("description") or fields.get("summary")
                                or fields.get("content"))[:900],
            "published": _when(fields.get("pubdate") or fields.get("published")
                               or fields.get("updated")),
        })
    return out


def fetch_items(sources, fetch=None):
    """Every item from every source. A source that fails is skipped, logged."""
    items = []
    for name, url in sources:
        try:
            if fetch:
                body = fetch(url)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Karamel/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read(2_000_000).decode("utf-8", "replace")
            items.extend(parse_feed(body, name))
        except Exception as e:  # network, encoding, anything: lose one source
            print(f"  source failed: {name} ({type(e).__name__})", file=sys.stderr)
    return items


def fresh(items, seen_links, now, max_age_h=ITEM_MAX_AGE_H):
    """Recent, undated-is-dropped, unused, summarised, one per link, newest first.

    Undated is dropped rather than trusted: "current" is the whole point, and an
    item that cannot say when it is from cannot promise it."""
    cutoff = now - timedelta(hours=max_age_h)
    keep, links = [], set()
    for it in items:
        if not it["published"] or it["published"] < cutoff:
            continue
        if it["link"] in seen_links or it["link"] in links:
            continue
        if len(it.get("summary") or "") < MIN_SUMMARY_CHARS:
            continue
        links.add(it["link"])
        keep.append(it)
    keep.sort(key=lambda it: it["published"], reverse=True)
    return keep


# --- people --------------------------------------------------------------------


def load_people(path):
    return [p for p in read_jsonl(path) if (p.get("name") or "").strip()]


def people_in(item, people):
    """Named people from this person's list who appear in the item, by full
    name. Full name only: a surname alone is how a stranger's obituary was
    once attributed to a colleague."""
    hay = " " + re.sub(r"[^a-z0-9 ]+", " ",
                       f"{item.get('title', '')} {item.get('summary', '')}".lower()) + " "
    hay = " ".join(hay.split()).join("  ")
    out = []
    for p in people:
        name = " ".join(re.sub(r"[^a-z0-9 ]+", " ", p["name"].lower()).split())
        if len(name.split()) >= 2 and f" {name} " in hay:
            out.append(p)
    return out


def people_note(found, author):
    if not found:
        return ""
    lines = []
    for p in found:
        bits = [f"- {p['name']}"]
        if p.get("cares_about"):
            bits.append(f"cares about: {p['cares_about']}")
        if p.get("relationship"):
            bits.append(f"how {author} knows them: {p['relationship']}")
        lines.append("; ".join(bits))
    return (
        "PEOPLE IN THIS ITEM whom " + author + " knows. If the post speaks to or "
        "about them, make it about what they care about, not about " + author +
        ". Never open with an ask, a pitch or a congratulations-then-pivot.\n"
        + "\n".join(lines))


# --- learning from edits ---------------------------------------------------------


def lessons(edits_path, limit=8):
    """The changes this person has asked for, newest first, deduplicated.

    Verbatim, not summarised: "less salesy" from them is a better instruction
    than any paraphrase of it from us."""
    rows = read_jsonl(edits_path)[-40:]
    said, out = set(), []
    for r in reversed(rows):
        if r.get("kind") == "revise":
            ask = " ".join((r.get("instruction") or "").split())[:240]
            if ask and ask.lower() not in said:
                said.add(ask.lower())
                out.append(f'- They asked: "{ask}"')
        elif r.get("kind") == "edited" and r.get("after"):
            out.append("- They rewrote a draft themselves. Their version read: "
                       f'"{" ".join(r["after"].split())[:300]}"')
        if len(out) >= limit:
            break
    if not out:
        return ""
    return ("HOW THEY HAVE EDITED EARLIER LINKEDIN DRAFTS. Apply these "
            "preferences now, without being asked again:\n" + "\n".join(out))


def record_edit(lane, draft, kind, instruction="", after=""):
    append_jsonl(lane.edits_path, {
        "ts": now_iso(), "draft_id": draft.get("draft_id"), "kind": kind,
        "instruction": instruction, "before": draft.get("draft_text", ""),
        "after": after,
    })


# --- what LinkedIn refuses -------------------------------------------------------


_PITCH_OPEN = re.compile(
    r"^\s*(i['’]?m (so )?(excited|thrilled|delighted|proud|honou?red) to|"
    r"(so )?(excited|thrilled|delighted|proud) to|"
    r"i help\b|dm me|let['’]?s connect|we['’]?re hiring|book a call|"
    r"big news|announcing\b|happy to (announce|share))", re.I)
_BAIT_CLOSE = re.compile(
    r"^\s*(agree\??|thoughts\??|what do you think\??|who else\b.*|"
    r"drop (a|your) comment.*|comment below.*|repost if.*|tag someone.*|"
    r"follow (me )?for more.*|♻️.*)\s*$", re.I)
_URL = re.compile(r"https?://|www\.", re.I)
_HASHTAG = re.compile(r"(?<!\w)#[A-Za-z]\w+")
_SLOP_EMOJI = ("🚀", "👇", "🔥", "💯")


def linkedin_veto(draft):
    """The LinkedIn-specific refusals, as one reason or "".

    Deterministic, and fed back through the same refine loop as the critic's
    own vetoes, so a failing draft returns with the reason rather than being
    patched afterwards."""
    text = (draft or "").strip()
    if not text:
        return "empty draft"
    if _URL.search(text):
        return ("a link in the post body: LinkedIn shows fewer people a post "
                "with a link, so the source goes in the first comment instead")
    tags = _HASHTAG.findall(text)
    if len(tags) > 2:
        return f"{len(tags)} hashtags: two at most, and none is fine"
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]
    if _PITCH_OPEN.search(first):
        return f'opens with a pitch or an announcement ("{first[:50]}")'
    last = [l for l in text.splitlines() if l.strip()][-1]
    if _BAIT_CLOSE.search(last):
        return f'closes on engagement bait ("{last.strip()[:40]}")'
    for e in _SLOP_EMOJI:
        if e in text:
            return f"{e} reads as LinkedIn template copy"
    return ""


# --- the roster: people to credit, in the account holder's own words --------------


def load_roster(path):
    """Rows that can be credited. A row is skipped, never completed, when it has
    no full name, no known_for line or no public page for it: the line is a
    claim about a real person in the account holder's name, and only they can
    make it."""
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = " ".join((r.get("name") or "").split())
            line = " ".join((r.get("known_for") or "").split())
            url = (r.get("known_for_url") or "").strip()
            if len(name.split()) < 2 or not line or not url.startswith(("http://", "https://")):
                continue
            out.append({"name": name, "known_for": line, "url": url,
                        "relationship": " ".join((r.get("relationship") or "").split())})
    return out


def amplify_key(person):
    """One person and one line. Rewriting the line offers them again;
    re-reading an unchanged roster does not."""
    return f"{person['name'].lower()}|{person['known_for'].lower()}"


def roster_item(person, now):
    return {"source": "your roster", "link": person["url"], "published": now,
            "title": f"{person['name']}: {person['known_for']}",
            "summary": (f"{person['name']}. How they know each other: "
                        f"{person['relationship']}. Known for: {person['known_for']}."),
            "person": person}


def choose_to_credit(lane, roster, k):
    """Up to k people not yet credited for their current line, longest since
    last credited first, then in roster order."""
    rows = read_jsonl(lane.drafts_path)
    used = {r.get("amplify_key") for r in rows if r.get("amplify_key")}
    last = {}
    for r in rows:
        if r.get("amplify_key"):
            last[r["amplify_key"].split("|")[0]] = r.get("drafted_at", "")
    waiting = [p for p in roster if amplify_key(p) not in used]
    waiting.sort(key=lambda p: last.get(p["name"].lower(), ""))
    return waiting[:k]


def amplify_count(lane, n):
    """How many of this run's n drafts credit somebody, so that the ratio
    across every run so far stays as close to the mix as whole drafts allow.
    Three a week at 80/20 goes 2, 3, 2, ... rather than 2 forever (67%)."""
    if not lane.mix:
        return 0
    total = (lane.mix["amplify"] + lane.mix["own"]) or 1
    target = lane.mix["amplify"] / total
    rows = [r for r in read_jsonl(lane.drafts_path)
            if r.get("kind") in ("amplify", "own") and not r.get("revised_from")]
    a, t = sum(r["kind"] == "amplify" for r in rows), len(rows)
    return min(range(n + 1), key=lambda k: (abs((a + k) / (t + n) - target), -k))


def zone_hit(text, zones):
    """The first banned subject present in the text, or "". A stem of five or
    more letters reaches its inflections; a phrase is matched whole."""
    words = re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split()
    padded = " " + " ".join(words) + " "
    for z in zones:
        if " " in z:
            if f" {z} " in padded:
                return z
        elif z in words or (len(z) >= 5 and any(w.startswith(z) for w in words)):
            return z
    return ""


# --- feeling ------------------------------------------------------------------------

#: Delivered drafts whose feelings are cooling. Every permission this engine has
#: granted got grabbed and repeated (its own notes, 2026-10-17); the first two
#: LinkedIn drafts for one pilot both reached for "love".
FEELING_COOLDOWN = 2


def _stem(word):
    """love, loved and lovely are one feeling said three ways."""
    return word.lower()[:4]


def lane_feelings(text, lane):
    """The feeling words in a draft: the critic's list and VADER's reaction
    words, plus this person's own vocabulary for how they feel."""
    found = set(critic.feelings(text))
    low = (text or "").lower()
    for w in lane.feeling_words:
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low):
            found.add(w)
    return found


def feels(text, lane):
    """Whether the draft says how it landed for them.

    Deliberately not critic.has_feeling, which also counts an exclamation mark
    or an emoji. Those are what a draft reaches for when told to feel
    something, and for some voices they are exactly what must not appear."""
    if lane_feelings(text, lane):
        return True
    if lane.exclamation_is_feeling and "!" in (text or ""):
        return True
    try:
        return bool(critic._emotion_rescue(text))
    except Exception:
        return False


def recent_feelings(lane, n=FEELING_COOLDOWN):
    rows = [r for r in read_jsonl(lane.drafts_path) if r.get("draft_text")][-n:]
    out = set()
    for r in rows:
        out |= lane_feelings(r["draft_text"], lane)
    return out


def feeling_veto(text, lane, cooled=()):
    """The lane's feeling rules, as one reason or ""."""
    if lane.no_exclamation and "!" in (text or ""):
        return "an exclamation mark, which is not in their register"
    if lane.require_feeling and not feels(text, lane):
        return ("no feeling: say how this landed for them, in the register the "
                "voice card describes")
    stems = {_stem(w) for w in cooled}
    again = sorted(w for w in lane_feelings(text, lane) if _stem(w) in stems)
    if again:
        return (f'"{again[0]}" again: the last drafts already used that feeling, '
                "so say how it landed another way")
    return ""


def feeling_note(lane, cooled):
    parts = []
    if lane.require_feeling:
        marks = ("An emoji does not count; an exclamation mark does, where they "
                 "would really use one." if lane.exclamation_is_feeling else
                 "An exclamation mark or an emoji does not count.")
        parts.append("EVERY POST CARRIES A FEELING, and that is checked in code: how "
                     "this landed for them, in the register their voice card "
                     f"describes. {marks}")
    if lane.feeling_words:
        parts.append("Their own words for it include: " + ", ".join(lane.feeling_words) + ".")
    if cooled:
        parts.append("The last posts already used these feelings, so not again, "
                     "in any wording: " + ", ".join(sorted(cooled)) + ".")
    return "\n".join(parts)


# --- the model calls --------------------------------------------------------------


def _json_out(out):
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def pick(voice, author, lane, items, recent_topics, tenant_id=None):
    """One call: which item this person would actually have a position on, and
    what the position is. Returns (item, stance, lane_name, why) or None."""
    if not items:
        return None
    shown = items[:25]
    listing = "\n".join(
        f"[{i}] ({it['source']}, {it['published']:%a %d %b}) {it['title']}"
        f"\n    {it['summary'][:280]}" for i, it in enumerate(shown))
    lanes = lane.lanes or ["LinkedIn"]
    avoid = "\n".join(f"- {t}" for t in recent_topics[-8:]) or "- (none yet)"
    prompt = f"""You choose what {author} posts about on LinkedIn today. Their voice card is in your system prompt: it says what they know, what they may and may not claim, and what they care about.
Their space: {lane.space or "as the voice card describes"}.

Below are today's news items. Choose the ONE where {author} would have a genuine, specific position that their LinkedIn audience would care about, and that their voice card lets them hold without claiming anything they cannot claim. Prefer an item where they have direct experience over one where they would only be commenting. A funding announcement is only worth it if there is a real point about how founders or investors behave in it.
The position has to stand on this item's own facts plus what the voice card says they know first hand. Do not choose a position whose proof needs a fact the item does not give (a customer it does not name, a number it does not state): the post may only use the item's facts, so that position could never be written honestly.

Recently posted about, so choose something else:
{avoid}

Items:
{listing}

Reply with JSON only:
{{"index": <number, or null if none of these deserves a post from {author}>,
  "stance": "<their position in one sentence, in the shape their voice card says they argue in (a verdict, a question they actually have, or a reframe with its condition attached), and consistent with the item's own facts: what they think, not what happened>",
  "lane": "<one of: {' | '.join(lanes)}>",
  "why": "<why their audience would care, one short sentence>"}}"""
    out = critic.call_claude(prompt, system=voice, tenant=tenant_id, label="li-pick")
    data = _json_out(out)
    if not data or data.get("index") is None:
        return None
    try:
        item = shown[int(data["index"])]
    except (ValueError, IndexError, TypeError):
        return None
    lane_name = str(data.get("lane") or "").strip()
    if lane_name not in lanes:
        lane_name = lanes[0]
    return item, str(data.get("stance") or "").strip(), lane_name, str(data.get("why") or "").strip()


def brief(author, item, stance, ceiling, people="", taught="", palette="",
          cooling=(), stale_hedges=(), allow_em_dash=False, feedback=None,
          previous=None, instruction=None, presence_plural=False,
          allowed_constructions=(), feeling=""):
    """The maker's brief for a LinkedIn post. Shares the critic's CLEAN list and
    refused constructions with the X brief, so both are graded on rules they
    were shown."""
    allowed = tuple(allowed_constructions or ())
    clean_rules, clean_note = critic.clean_axis("analytical", allowed)
    refused = "\n".join(f"- {l}" for l in dict.fromkeys(l for _, l in critic.SLOP_PATTERNS)
                        if l not in allowed)
    person = (f'"We" (we, us, our) is part of how {author} writes. Use "I" and "we" '
              "exactly as the voice card says they do; never switch one for the "
              "other on your own.\n" if presence_plural else "")
    aim_lo, aim_hi = max(120, ceiling - 400), max(200, ceiling - 150)
    dash = "" if allow_em_dash else "No em-dashes or en-dashes.\n"
    extra = ""
    if cooling:
        extra += ("\nAlready used in recent posts, so refused here: "
                  + ", ".join(sorted(cooling)[:12]) + ".\n")
    if stale_hedges:
        extra += ("\nAlready used as the hedge in recent posts, so refused here: "
                  + ", ".join(f'"{w}"' for w in sorted(stale_hedges)) + ".\n")
    if palette:
        extra += f"\n{palette}\n"
    for block in (people, taught, feeling):
        if block:
            extra += f"\n{block}\n"
    if previous is not None:
        task = (f"REVISE this LinkedIn draft. {author} replied with what to change:\n"
                f'"{instruction}"\n\nThe draft:\n{previous}\n\nMake exactly that '
                "change, keep everything that already worked, and keep every "
                "rule below.")
    else:
        task = f"Write ONE original LinkedIn post in {author}'s voice."
    fb = (f"\nThe previous attempt was rejected. The single fix: {feedback}\n"
          "Fix exactly that. Keep what worked.\n" if feedback else "")
    return f"""{task} Their voice card is in your system prompt; obey every rule in it, especially what they can and cannot claim.

THE NEWS ITEM (the only facts you may state about it):
{item['source']}, {item['published']:%d %B %Y}: {item['title']}
{item['summary']}

THEIR POSITION ON IT: {stance}

The post exists to say that position, in their voice, to people in their field. A reader must be able to tell what {author} thinks. It can be held as a disagreement, an admission, or a question they genuinely have; it must not be a summary of the news, and it must not take the closed three-beat shape (fact, clever reframe, universal law). Bring in what {author} has seen first hand where the voice card supports it.

WRITTEN FOR LINKEDIN, NOT X:
- The first line has to make a stranger in their field want to keep reading: the position, or the moment they met the thing. No warm-up, no "In today's world".
- Short paragraphs of one to three sentences, a blank line between them. No bullet lists, no bold, no emoji bullets.
- Do not paste the link. It goes in the first comment, and the email handles that.
- Hashtags: none, or at most two that a person would actually search. Never a row of them.
- End on the point, a specific question you would really want answered, or an offer that is useful to the reader. Never "Agree?", "Thoughts?" or a request to like or repost.
- Name the company or person in the item where it matters, so the post stands alone for someone who has not read the news.

{person}HUMAN, NOT GENERATED: say how it landed for them, in their own words for liking, doubting, being surprised or unconvinced, the way the voice card shows they do. Contractions, one aside, their real hedges. Prose with no feeling in it is the main way these read as written by a machine.

DO NOT REUSE THEIR LINES. The voice card quotes their real sentences as the bar, to show the texture. Lifting one, or lightly rewording one, reads as a pastiche of the person and is the most common way these fail. Write new sentences that could sit beside those, never the ones already there.

Zero fabrication. State only facts that are in the item above, or that the voice card says they can claim. No invented numbers, quotes or dates. Where a fact you need is missing, leave it out.

LENGTH IS A HARD GATE: {ceiling} characters, counted. Aim for {aim_lo} to {aim_hi}. One point, made once. No closing line that restates it.

CLEAN is graded against this list:
{clean_rules}
{clean_note}
{dash}
These are refused deterministically:
{refused}
{extra}{fb}
Output ONLY the post text."""


def critic_context(item, stance, ceiling):
    """What the critic needs to grade a LinkedIn post, including the only text
    its facts may come from. It used to get the headline alone, so a fact the
    maker invented about the news could not be told from one it was given."""
    return (f"This is a LinkedIn post, not an X post: up to {ceiling} characters, "
            "short paragraphs are correct, and it is read by people in the "
            f"writer's professional field. The position it should carry: {stance}.\n"
            f"The news it responds to, which is the ONLY text facts about the news "
            f"may come from:\n{item['title']} ({item['source']})\n{item['summary']}\n"
            "Grade TAKE on whether that position is clear and theirs. Grade SPECIFIC "
            "against this text: any claim about the company, people or events in "
            "the news that this text does not state is a fabrication, so score "
            "SPECIFIC 1 and quote the claim in WHY. Claims the voice card says they "
            "can make about themselves are allowed.")


def approved_texts(tenant, lane):
    """What this person actually approved, as they posted it: LinkedIn posts
    they confirmed, then their approved X drafts.

    Never their past writing. Adi, 2026-10-19: Karamel learns from ongoing
    behaviour, not past activity, so a palette is built only from drafts the
    person chose to publish. Both platforms count, because how somebody sounds
    when pleased or unconvinced is theirs, not the platform's."""
    texts = [r["text"] for r in read_jsonl(lane.posts_path) if r.get("text")]
    try:
        import emotion
        texts += emotion.approved_texts(tenant.data_dir)
    except Exception:
        pass
    return texts


def palette_line(tenant, lane):
    """Their emotional range for the brief, or "" until there are enough
    approvals to measure one. The threshold is the X lane's, so both lanes
    start describing a person at the same point."""
    try:
        import emotion
        texts = approved_texts(tenant, lane)
        if len(texts) < getattr(emotion, "PALETTE_MIN_APPROVED", 5):
            return ""
        return emotion.describe(emotion.palette(texts), tenant.name)
    except Exception:
        return ""


def amplify_brief(author, person, ceiling, taught="", palette="", cooling=(),
                  stale_hedges=(), allow_em_dash=False, feedback=None,
                  previous=None, instruction=None, presence_plural=False,
                  allowed_constructions=(), feeling=""):
    """The brief for a post crediting somebody. Its facts are the account
    holder's own line about them and nothing the model remembers about a real
    person, because a detail from memory is the one failure this genre cannot
    be allowed."""
    allowed = tuple(allowed_constructions or ())
    clean_rules, clean_note = critic.clean_axis("analytical", allowed)
    refused = "\n".join(f"- {l}" for l in dict.fromkeys(l for _, l in critic.SLOP_PATTERNS)
                        if l not in allowed)
    dash = "" if allow_em_dash else "No em-dashes or en-dashes.\n"
    we = (f'"We" (we, us, our) is part of how {author} writes. Use "I" and "we" exactly '
          "as the voice card says they do.\n" if presence_plural else "")
    extra = ""
    if cooling:
        extra += ("\nAlready used in recent posts, so refused here: "
                  + ", ".join(sorted(cooling)[:12]) + ".\n")
    if stale_hedges:
        extra += ("\nAlready used as the hedge recently: "
                  + ", ".join(f'"{w}"' for w in sorted(stale_hedges)) + ".\n")
    for block in (palette, taught, feeling):
        if block:
            extra += f"\n{block}\n"
    if previous is not None:
        task = (f'REVISE this LinkedIn post. {author} replied with what to change:\n"{instruction}"'
                f"\n\nThe post:\n{previous}\n\nMake exactly that change and keep every rule below.")
    else:
        task = f"Write ONE LinkedIn post in {author}'s voice that credits {person['name']}."
    fb = (f"\nThe previous attempt was rejected. The single fix: {feedback}\n" if feedback else "")
    return f"""{task} Their voice card is in your system prompt; obey it.

WHO: {person['name']}.
HOW {author.upper()} KNOWS THEM: {person['relationship'] or "a colleague"}
WHAT THEY ARE KNOWN FOR, in {author}'s own words: {person['known_for']}

These are the ONLY facts about {person['name']} you may state. Do not add a title, an award, a date, a number, a recent event or anything else from memory: the post goes out under {author}'s name about a real person, and one invented detail is worse than no post.

The job: make people in {author}'s field want to look at {person['name']}'s work. Name them in full in the first two lines. Say the specific thing above and why it matters to the reader, from where {author} sits. Never "congratulations on the news" with nothing in it, never gushing, and never a turn back to {author}'s own work or anything for sale.

WRITTEN FOR LINKEDIN: short paragraphs with a blank line between them, no bullet lists, no emoji bullets. The link to their work goes in the first comment, not the post. No hashtags, or at most two. End on the point or a real question for the reader. Never "Agree?" or a request to like or repost.

{we}HUMAN, NOT GENERATED: say why {author} rates this work, in their register as the voice card shows it.

DO NOT REUSE THEIR LINES. The voice card's quoted sentences are the bar, not material.

LENGTH IS A HARD GATE: {ceiling} characters, counted. One point, made once.

CLEAN is graded against this list:
{clean_rules}
{clean_note}
{dash}
Refused deterministically:
{refused}
{extra}{fb}
Output ONLY the post text."""


def amplify_context(person, ceiling):
    return (f"This is a LinkedIn post, up to {ceiling} characters, crediting "
            f"{person['name']}. The ONLY facts about them it may state: they are "
            f"known for {person['known_for']}; the relationship is: "
            f"{person['relationship']}. Any other claim about {person['name']} (a "
            "title, award, date, number or recent event) is a fabrication: score "
            "SPECIFIC 1 and quote it in WHY. Grade TAKE on whether the post says "
            "specifically why this work matters to readers.")


def write(tenant, lane, voice, item, stance, people="", previous=None,
          instruction=None, log=True):
    """Draft, grade, refine. Returns the critic's result shape plus "final".

    An item carrying a "person" is a roster credit and gets that genre's brief
    and context; anything else is a take on the news."""
    person = item.get("person")
    author = tenant.name
    gen_log = lane.generated_path
    cooling = critic.topic_exempt(critic.recent_motifs(gen_log), item["title"])
    hedges = critic.recent_stance_words(gen_log)
    taught = lessons(lane.edits_path)
    allow = bool(getattr(tenant, "allow_em_dash", False))
    plural = bool(getattr(tenant, "presence_plural", False))
    cooled = recent_feelings(lane)
    felt = feeling_note(lane, cooled)
    allowed = tuple(getattr(tenant, "allowed_constructions", None) or ())
    palette = palette_line(tenant, lane)
    journey = []

    def regenerate(feedback):
        if person:
            prompt = amplify_brief(author, person, lane.max_chars, taught=taught,
                                   palette=palette, cooling=cooling, stale_hedges=hedges,
                                   allow_em_dash=allow, feedback=feedback,
                                   previous=previous, instruction=instruction,
                                   presence_plural=plural, allowed_constructions=allowed,
                                   feeling=felt)
        else:
            prompt = brief(author, item, stance, lane.max_chars, people=people, taught=taught,
                           palette=palette, cooling=cooling, stale_hedges=hedges,
                           allow_em_dash=allow, feedback=feedback, previous=previous,
                           instruction=instruction, presence_plural=plural,
                           allowed_constructions=allowed, feeling=felt)
        out = critic.call_claude(prompt, system=voice, tenant=tenant.id,
                                 label="li-maker").strip()
        return out if allow else EM_DASH.sub(", ", out).strip()

    def graded(d):
        ctx = (amplify_context(person, lane.max_chars) if person
               else critic_context(item, stance, lane.max_chars))
        v = critic.grade(d, voice, context=ctx,
                         register="analytical", author=author,
                         critiques_path=lane.critiques_path, log=log,
                         allow_em_dash=allow, tenant=tenant.id,
                         generated_path=gen_log, max_chars=lane.max_chars,
                         banned_motifs=cooling, palette=palette,
                         require_feeling=bool(getattr(tenant, "require_feeling", False)),
                         feeling_quota=bool(getattr(tenant, "feeling_quota", False)),
                         presence_plural=plural, allowed_constructions=allowed)
        veto = ((linkedin_veto(d) or feeling_veto(d, lane, cooled))
                if v.get("verdict") == "PASS" else "")
        if veto:
            v = dict(v, verdict="FAIL", override=veto, fix=veto)
        journey.append({"round": len(journey), "draft": d, "verdict": v["verdict"],
                        "why": v.get("why", ""), "fix": v.get("fix", ""),
                        "override": v.get("override", ""), "scores": v.get("scores", {})})
        return v

    final, verdict, rounds = critic.refine(regenerate, graded, MAX_ROUNDS)
    if verdict.get("verdict") == "PASS" and critic.verify_slots(final):
        verdict = dict(verdict, verdict="FAIL", override="unfilled blank survived")
    result = {"final": final, "verdict": verdict["verdict"], "rounds": rounds,
              "scores": verdict.get("scores", {}), "journey": journey,
              "topic": item["title"], "register": "linkedin",
              "tenant": tenant.id, "generated_at": now_iso()}
    if log:
        append_jsonl(gen_log, result)
    return result


# --- email -----------------------------------------------------------------------


def subject(kind, lane_name):
    """"[LinkedIn] Draft ready to post: Founder craft". The platform first, to
    match the X lane's "[X] ..." and so an inbox can be filtered on it."""
    return f"{SUBJECT_MARKER} {kind}: {lane_name}"


def compose_url(text):
    """LinkedIn's own composer, opened with the draft filled in. It posts
    nothing: the person reads it there and presses Post, or does not."""
    return "https://www.linkedin.com/feed/?shareActive=true&text=" + urllib.parse.quote(text)


def body(author, draft_id, text, item, stance, revised=False):
    head = ("Revised, with your change applied." if revised else
            f"A LinkedIn draft for {author}, ready when you are.")
    return f"""{head}

{text}

----
Why this one: {stance}

Source, for the first comment rather than the post:
{item['title']} ({item['source']})
{item['link']}

Open it in LinkedIn, filled in (nothing posts until you press Post):
{compose_url(text)}

Reply to this email with:
  posted, and the post's link once it is live, so we can track how it does
  skip, to drop it
  edited: followed by the version you actually posted
  or just say what to change ("shorter", "less formal", "lead with the robotics point") and a revised draft comes back

LinkedIn draft #L{draft_id}
"""


def send(lane, subj, text, in_reply_to=None):
    """Send one LinkedIn email through channels.send_email. Returns the Message-ID.

    Always email, whatever the person's X drafts arrive by, because these are
    asked for as separate emails. Addressed through a stand-in that carries
    only the lane's address, so a lane can deliver somewhere other than the
    tenant's own channel while reusing the shared sender's threading,
    credentials and self-addressed-unread fix."""
    import types
    import channels
    if not lane.email:
        raise ValueError(f"tenant {lane.tenant.id!r} has no email for the LinkedIn lane")
    to = types.SimpleNamespace(id=lane.tenant.id,
                               channel={"type": "email", "address": lane.email})
    return channels.send_email(to, text, subject=subj, in_reply_to=in_reply_to)


# --- one tenant, one due hour ---------------------------------------------------------


#: Items tried per slot. A draft that fails the gate is usually a bad choice
#: of item, not a bad slot, so the next item gets one chance before the slot
#: is given up.
ATTEMPTS_PER_SLOT = 2


def draft_one(tenant, lane, voice, items, recent, people, log=True, seen_cb=None):
    """Pick, write, gate, up to ATTEMPTS_PER_SLOT items. Returns
    (result, item, stance, lane_name, why, remaining_items, failures)."""
    failures = []
    for _ in range(ATTEMPTS_PER_SLOT):
        chosen = pick(voice, tenant.name, lane, items, recent, tenant_id=tenant.id)
        if not chosen:
            break
        item, stance, lane_name, why = chosen
        items = [it for it in items if it["link"] != item["link"]]
        if seen_cb:
            seen_cb(item)
        r = write(tenant, lane, voice, item, stance,
                  people=people_note(people_in(item, people), tenant.name), log=log)
        if r["verdict"] == "PASS":
            return r, item, stance, lane_name, why, items, failures
        failures.append((item, stance, r))
    return None, None, None, None, None, items, failures


def _report_failures(tenant_id, failures):
    for item, stance, r in failures:
        print(f"[{tenant_id}] linkedin: dropped after {r['rounds']} round(s): {item['title'][:70]}")
        for j in r["journey"]:
            reason = j.get("override") or j.get("why", "")
            print(f"    round {j['round']}: {j['verdict']} {reason[:200]}")


def news_items(lane, now, fetch=None):
    """Fresh, unused, summarised items from the lane's sources, with anything
    touching a banned subject removed before a model ever reads it."""
    seen = {r.get("link") for r in read_jsonl(lane.seen_path)}
    items = fresh(fetch_items(lane.sources, fetch=fetch), seen, now, lane.max_age_h)
    if lane.banned_zones:
        items = [it for it in items
                 if not zone_hit(f"{it['title']} {it['summary']}", lane.banned_zones)]
    return items


def _row(tenant, draft_id, kind, item, stance, lane_name, why, r, mid, **extra):
    row = {"draft_id": draft_id, "tenant": tenant.id, "platform": "linkedin",
           "kind": kind, "topic": item["title"],
           "item": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in item.items()},
           "stance": stance, "lane": lane_name, "why": why,
           "draft_text": r["final"], "scores": r["scores"], "rounds": r["rounds"],
           "status": "pending", "message_id": mid, "drafted_at": now_iso()}
    row.update(extra)
    return row


def digest_body(author, entries, short=0):
    """One email, every draft numbered, each with its own id and composer link."""
    n = len(entries)
    credits = sum(e["kind"] == "amplify" for e in entries)
    head = (f"Your LinkedIn drafts for this week, {author.split()[0]}: "
            f"{credits} crediting people you know, {n - credits} of your own.")
    parts = [head]
    for e in entries:
        item = e["item"]
        if e["kind"] == "amplify":
            source = f"Their work, for the first comment rather than the post:\n{item['link']}"
        else:
            source = (f"Source, for the first comment rather than the post:\n"
                      f"{item['title']} ({item['source']})\n{item['link']}")
        parts.append(
            f"--- Draft {e['position']} of {n}: {e['lane_name']} ---\n\n"
            f"{e['r']['final']}\n\n"
            f"Why this one: {e['stance']}\n\n{source}\n\n"
            f"Open it in LinkedIn, filled in (nothing posts until you press Post):\n"
            f"{compose_url(e['r']['final'])}\n\n"
            f"LinkedIn draft #L{e['draft_id']}")
    tail = ("----\nReply with the draft's number first, one line each:\n"
            "  2 posted, and the post's link once it is live\n"
            "  1 skip\n"
            "  3 shorter, and lead with the research\n"
            "  2 edited: the version you actually posted\n"
            "Each change comes back as its own revised draft.")
    if short:
        tail += (f"\n\nYour list had nothing new to credit for {short} of this week's "
                 f"slot{'s' if short > 1 else ''}, so {'they are' if short > 1 else 'it is'} "
                 "your own take instead. Add someone, or update what a person is known "
                 "for, and they will be credited next week.")
    parts.append(tail)
    return "\n\n".join(parts) + "\n"


def run_digest(tenant, lane, voice, dry=False, now=None, fetch=None):
    """One run, every draft in one email: roster credits first, in the
    proportion the mix asks for, then the person's own take on the news."""
    now = now or datetime.now(timezone.utc)
    n = lane.drafts_per_run
    k = amplify_count(lane, n)
    to_credit = choose_to_credit(lane, load_roster(lane.roster_path), k)
    short = k - len(to_credit)
    entries = []
    for person in to_credit:
        item = roster_item(person, now)
        stance = (f"you know {person['name']} ({person['relationship'] or 'a colleague'}), "
                  f"and their {person['known_for']} deserves a wider audience")
        r = write(tenant, lane, voice, item, stance, log=not dry)
        if r["verdict"] != "PASS":
            _report_failures(tenant.id, [(item, stance, r)])
            continue
        entries.append({"kind": "amplify", "item": item, "stance": stance,
                        "lane_name": f"Crediting {person['name']}", "why": "",
                        "r": r, "key": amplify_key(person)})
    want = n - len(entries)
    if want > 0:
        items = news_items(lane, now, fetch=fetch)
        recent = [r.get("topic", "") for r in read_jsonl(lane.drafts_path)]
        people = load_people(lane.people_path)

        def mark_seen(item):
            if not dry:
                append_jsonl(lane.seen_path, {"link": item["link"], "ts": now_iso()})
        for _ in range(want):
            r, item, stance, lane_name, why, items, failures = draft_one(
                tenant, lane, voice, items, recent, people, log=not dry, seen_cb=mark_seen)
            _report_failures(tenant.id, failures)
            if r is None:
                break
            entries.append({"kind": "own", "item": item, "stance": stance,
                            "lane_name": f"Your take: {lane_name}", "why": why,
                            "r": r, "key": None})
            recent.append(item["title"])
    if not entries:
        print(f"[{tenant.id}] linkedin: digest empty this week, nothing sent")
        return 0
    base = int(time.time() * 1000)
    for pos, e in enumerate(entries, 1):
        e["position"], e["draft_id"] = pos, base + pos
    text = digest_body(tenant.name, entries, short=short)
    subj = (f"{SUBJECT_MARKER} Your {len(entries)} drafts for this week"
            if len(entries) > 1 else f"{SUBJECT_MARKER} Your draft for this week")
    if dry:
        print(f"--- {subj}\n{text}")
        return len(entries)
    mid = send(lane, subj, text)
    for e in entries:
        append_jsonl(lane.drafts_path, _row(
            tenant, e["draft_id"], e["kind"], e["item"], e["stance"], e["lane_name"],
            e["why"], e["r"], mid, amplify_key=e["key"], digest=True,
            digest_mid=mid, position=e["position"]))
    print(f"[{tenant.id}] linkedin: digest of {len(entries)} delivered "
          f"({sum(e['kind'] == 'amplify' for e in entries)} credits)")
    return len(entries)


def run_tenant(tenant, dry=False, now=None, fetch=None):
    lane = Lane(tenant)
    if not lane.enabled:
        return 0
    if not lane.sources:
        print(f"[{tenant.id}] linkedin: no sources configured", file=sys.stderr)
        return 0
    if not lane.email and not dry:
        print(f"[{tenant.id}] linkedin: no email address to deliver to", file=sys.stderr)
        return 0
    card = tenant.voice_card_path
    if not card.exists():
        print(f"[{tenant.id}] linkedin: no voice card at {card}", file=sys.stderr)
        return 0
    voice = card.read_text()
    now = now or datetime.now(timezone.utc)

    if lane.digest:
        return run_digest(tenant, lane, voice, dry=dry, now=now, fetch=fetch)
    items = news_items(lane, now, fetch=fetch)
    recent = [r.get("topic", "") for r in read_jsonl(lane.drafts_path)]
    people = load_people(lane.people_path)

    def mark_seen(item):
        if not dry:
            append_jsonl(lane.seen_path, {"link": item["link"], "ts": now_iso()})

    delivered = 0
    for _ in range(lane.drafts_per_run):
        r, item, stance, lane_name, why, items, failures = draft_one(
            tenant, lane, voice, items, recent, people, log=not dry, seen_cb=mark_seen)
        _report_failures(tenant.id, failures)
        if r is None:
            print(f"[{tenant.id}] linkedin: nothing sent this slot "
                  f"({len(failures)} draft(s) failed the gate, {len(items)} item(s) left)")
            break
        draft_id = int(time.time() * 1000)
        text = body(tenant.name, draft_id, r["final"], item, stance)
        subj = subject("Draft ready to post", lane_name)
        if dry:
            print(f"--- {subj}\n{text}")
            delivered += 1
            continue
        mid = send(lane, subj, text)
        append_jsonl(lane.drafts_path, _row(tenant, draft_id, "own", item, stance,
                                            lane_name, why, r, mid))
        recent.append(item["title"])
        delivered += 1
    print(f"[{tenant.id}] linkedin: {delivered} draft(s) delivered")
    return delivered


# --- replies ------------------------------------------------------------------------


_POST_URL = re.compile(r"https?://(?:www\.)?linkedin\.com/(?:feed/update|posts)/[^\s>)\]]+", re.I)
_URN = re.compile(r"urn:li:(share|ugcPost|activity):(\d+)")


def parse_reply(text):
    """(intent, payload) from the person's own words, quoted original removed.

    skip / posted (payload: the post URL or "") / edited (payload: their text) /
    revise (payload: the instruction). Anything that is not one of the first
    three is a request for a change, because that is the only other thing a
    person replying to a draft is doing."""
    t = (text or "").strip()
    if not t:
        return None, ""
    low = t.lower()
    url = _POST_URL.search(t)
    if low.startswith(("skip", "❌", "no thanks", "pass")):
        return "skip", ""
    if low.startswith(("posted", "✅", "done", "published", "live")):
        return "posted", url.group(0) if url else ""
    if url and len(_POST_URL.sub("", t).split()) <= 3:
        return "posted", url.group(0)
    m = re.match(r"^\s*(edited|✏️)\s*:?\s*(.+)$", t, re.S | re.I)
    if m and m.group(2).strip():
        return "edited", m.group(2).strip()
    return "revise", " ".join(t.split())[:1000]


def post_urn(url):
    """The URN analytics can query, when the link carries one. A share or a
    ugcPost URN is queryable; an activity URN is not the same number and is
    kept only so the post can be found again."""
    m = _URN.search(urllib.parse.unquote(url or ""))
    if m:
        return f"urn:li:{m.group(1)}:{m.group(2)}"
    m = re.search(r"-activity-(\d+)-", url or "")
    return f"urn:li:activity:{m.group(1)}" if m else ""


def _locate(lane, in_reply_to, draft_id):
    rows = read_jsonl(lane.drafts_path)
    for i, r in enumerate(rows):
        if in_reply_to and r.get("message_id") == in_reply_to:
            return rows, i
    for i, r in enumerate(rows):
        if draft_id is not None and r.get("draft_id") == draft_id:
            return rows, i
    return rows, None


def apply_reply(tenant, msg, dry=False):
    import inbox
    lane = Lane(tenant)
    raw = inbox.body_text(msg)
    m = DRAFT_ID_RE.search(raw)
    rows, i = _locate(lane, inbox.header(msg, "In-Reply-To").strip() or None,
                      int(m.group(1)) if m else None)
    if i is None:
        return f"[{tenant.id}] linkedin: no matching draft"
    # A second reply to the first email is about the draft as it stands now,
    # not as it was first sent, so follow the revisions to the newest one.
    if not rows[i].get("digest"):
        i = _newest(rows, i)
    text = inbox.strip_quoted(raw)
    reply_mid = inbox.header(msg, "Message-ID")
    if rows[i].get("digest"):
        return apply_digest_reply(tenant, lane, rows, rows[i]["digest_mid"], text,
                                  reply_mid, dry=dry)
    draft = rows[i]
    intent, payload = parse_reply(text)
    if intent is None:
        return f"[{tenant.id}] linkedin: empty reply to #L{draft['draft_id']}"
    if dry:
        return f"[{tenant.id}] linkedin: would {intent} #L{draft['draft_id']}"
    return _apply_one(tenant, lane, rows, i, intent, payload, reply_mid)


_NUMBERED = re.compile(r"^\s*(?:draft\s*)?#?([1-9])\s*(?:[:.)\-]\s*|\s+)(.*)$", re.I)


def parse_digest_reply(text):
    """[(draft number, what they said about it)]. A line starting with a number
    opens that draft's part; lines after it, until the next number, belong to
    it, so an edited version can run over several lines."""
    out = []
    for line in (text or "").splitlines():
        m = _NUMBERED.match(line)
        if m:
            out.append([int(m.group(1)), m.group(2)])
        elif out and line.strip():
            out[-1][1] += "\n" + line
    return [(n, t.strip()) for n, t in out if t.strip()]


def _newest(rows, i):
    newer = {r.get("revised_from"): j for j, r in enumerate(rows) if r.get("revised_from")}
    while rows[i].get("draft_id") in newer:
        i = newer[rows[i]["draft_id"]]
    return i


def apply_digest_reply(tenant, lane, rows, digest_mid, text, reply_mid, dry=False):
    by_pos = {r["position"]: j for j, r in enumerate(rows)
              if r.get("digest_mid") == digest_mid and r.get("digest")}
    parts = parse_digest_reply(text)
    if not parts and len(by_pos) == 1:
        parts = [(next(iter(by_pos)), text.strip())]
    if not parts:
        if not dry and text.strip():
            send(lane, "Re: " + f"{SUBJECT_MARKER} Which draft?",
                 "Start each line with the draft's number, for example \"2 shorter\" "
                 "or \"1 skip\", so the change lands on the right one.",
                 in_reply_to=reply_mid or digest_mid)
        return f"[{tenant.id}] linkedin: digest reply with no draft number"
    out = []
    for pos, said in parts:
        if pos not in by_pos:
            out.append(f"no draft {pos} in that email")
            continue
        j = _newest(rows, by_pos[pos])
        intent, payload = parse_reply(said)
        if intent is None:
            continue
        if dry:
            out.append(f"would {intent} draft {pos}")
            continue
        out.append(_apply_one(tenant, lane, rows, j, intent, payload, reply_mid))
    return f"[{tenant.id}] linkedin: digest -> " + "; ".join(out)


def _apply_one(tenant, lane, rows, i, intent, payload, reply_mid):
    draft = rows[i]
    if intent == "skip":
        draft["status"] = "skipped"
    elif intent == "posted":
        draft["status"] = "posted_edited" if draft.get("edited_text") else "posted"
        draft["post_url"] = payload or draft.get("post_url", "")
        append_jsonl(lane.posts_path, {
            "draft_id": draft["draft_id"], "tenant": tenant.id, "platform": "linkedin",
            "posted_confirmed_at": now_iso(), "url": draft["post_url"],
            "urn": post_urn(draft["post_url"]),
            "text": draft.get("edited_text") or draft.get("draft_text"),
        })
    elif intent == "edited":
        draft["edited_text"] = payload
        record_edit(lane, draft, "edited", after=payload)
    else:
        return revise(tenant, lane, rows, i, payload, reply_mid)
    draft["replied_at"] = now_iso()
    write_jsonl_atomic(lane.drafts_path, rows)
    return f"[{tenant.id}] linkedin: #L{draft['draft_id']} -> {intent}"


def revise(tenant, lane, rows, i, instruction, reply_mid):
    """Their change, applied and gated, back in the same thread."""
    draft = rows[i]
    item = dict(draft.get("item") or {})
    item["published"] = _when(item.get("published")) or datetime.now(timezone.utc)
    voice = tenant.voice_card_path.read_text()
    current = draft.get("edited_text") or draft["draft_text"]
    r = write(tenant, lane, voice, item, draft.get("stance", ""), previous=current,
              instruction=instruction)
    record_edit(lane, draft, "revise", instruction=instruction,
                after=r["final"] if r["verdict"] == "PASS" else "")
    draft.setdefault("revisions", []).append(
        {"instruction": instruction, "verdict": r["verdict"], "at": now_iso()})
    if r["verdict"] != "PASS":
        write_jsonl_atomic(lane.drafts_path, rows)
        note = r["journey"][-1].get("override") or r["journey"][-1].get("why", "")
        send(lane, "Re: " + subject("Could not revise", draft.get("lane", "LinkedIn")),
             f'Your change ("{instruction}") could not be made without breaking a '
             f"rule the draft has to keep: {note}\n\nTry saying it another way, or "
             f"reply with edited: and your own version.\n\nLinkedIn draft #L{draft['draft_id']}",
             in_reply_to=reply_mid or draft.get("message_id"))
        return f"[{tenant.id}] linkedin: revision of #L{draft['draft_id']} failed the gate"
    new_id = int(time.time() * 1000)
    draft["status"] = "revised"
    mid = send(lane, "Re: " + subject("Revised draft", draft.get("lane", "LinkedIn")),
               body(tenant.name, new_id, r["final"], item, draft.get("stance", ""), revised=True),
               in_reply_to=reply_mid or draft.get("message_id"))
    rows.append(dict(draft, draft_id=new_id, draft_text=r["final"], status="pending",
                     message_id=mid, revised_from=draft["draft_id"], digest=False,
                     edited_text=None, revisions=[], drafted_at=now_iso()))
    write_jsonl_atomic(lane.drafts_path, rows)
    return f"[{tenant.id}] linkedin: #L{draft['draft_id']} revised -> #L{new_id}"


def _state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE)


def run_inbox(dry=False):
    """One IMAP pass over replies to LinkedIn drafts. Its own watermark, PEEK
    only, so it never changes what inbox.py or the person sees."""
    import channels
    import inbox
    if not lanes_with_linkedin():
        return 0
    cfg = channels.load_email_creds()
    host = cfg.get("imap_host") or cfg["smtp_host"].replace("smtp.", "imap.", 1)
    state = _state()
    with imaplib.IMAP4_SSL(host, int(cfg.get("imap_port", 993)), timeout=90) as im:
        im.login(cfg["username"], cfg["password"])
        im.select("INBOX")
        if "inbox_last_uid" not in state:
            state["inbox_last_uid"] = inbox.current_high_uid(im)
            if not dry:
                _save_state(state)
            print("linkedin inbox: first run, starting from now")
            return 0
        last = int(state["inbox_last_uid"])
        since = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
        typ, data = im.uid("search", None, "UID", f"{last + 1}:*",
                           "SUBJECT", f'"{SUBJECT_MARKER}"', "SINCE", since)
        uids = [u for u in (data[0] or b"").split() if int(u) > last] if typ == "OK" else []
        highest = last
        for uid in uids:
            typ, raw = im.uid("fetch", uid, "(BODY.PEEK[])")
            highest = max(highest, int(uid))
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            if inbox.is_our_own_outgoing(msg):
                continue
            t = inbox.tenant_for_sender(inbox.sender_address(msg))
            if t is None:
                lane_owner = [x for x in lanes_with_linkedin()
                              if Lane(x).email.lower() == (inbox.sender_address(msg) or "")]
                t = lane_owner[0] if lane_owner else None
            if t is None or not Lane(t).enabled:
                continue
            try:
                print(apply_reply(t, msg, dry=dry))
            except Exception as e:
                print(f"[{t.id}] linkedin: reply failed ({type(e).__name__}: {e})",
                      file=sys.stderr)
        if not dry:
            state["inbox_last_uid"] = highest
            _save_state(state)
    return 0


# --- the agent ---------------------------------------------------------------------


def tick(now=None):
    """Replies every time; drafts in a due hour, once; analytics once a day."""
    from karamel_common import is_paused, now_et
    # `./karamel stop` unloads every agent and sets a halt "so nothing runs
    # even if reloaded". This agent has to honour that like the others do, or a
    # reload after a stop would start emailing again.
    if is_paused():
        print("linkedin: paused or halted, nothing run")
        return 0
    et = now_et()
    state = _state()
    try:
        run_inbox()
    except Exception as e:
        print(f"linkedin inbox failed: {type(e).__name__}: {e}", file=sys.stderr)
    state = _state()
    done = state.setdefault("drafted_hour", {})
    for t in lanes_with_linkedin():
        lane = Lane(t)
        here = t.now()
        hour_key = here.strftime("%Y-%m-%dT%H")
        if not lane.due(here) or done.get(t.id) == hour_key or is_paused(t):
            continue
        done[t.id] = hour_key
        _save_state(state)
        try:
            run_tenant(t)
        except Exception as e:
            print(f"[{t.id}] linkedin draft failed: {type(e).__name__}: {e}", file=sys.stderr)
    day = et.strftime("%Y-%m-%d")
    if state.get("analytics_day") != day and et.hour >= 6:
        state["analytics_day"] = day
        _save_state(state)
        try:
            import linkedin_analytics
            linkedin_analytics.pull_all()
        except Exception as e:
            print(f"linkedin analytics failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


def enable(tenant_id, block, require_email=True):
    """Turn the lane on for one person by merging `block` into their record.

    Validated before it is written: a lane with no sources or nowhere to deliver
    would be switched on and then silently do nothing every three hours."""
    t = tenants.load_tenant(tenant_id)
    if t is None:
        raise SystemExit(f"no tenant {tenant_id!r}; see tenants.py --list")
    raw = t.to_dict()
    raw["linkedin"] = {**(raw.get("linkedin") or {}), **(block or {}), "enabled": True}
    fresh_t = tenants.Tenant(raw)
    lane = Lane(fresh_t)
    if not lane.sources:
        raise SystemExit("no sources: pass source_set or sources")
    if not lane.email and require_email:
        raise SystemExit("no email: their channel is not email, so pass an email")
    tenants.save_tenant(fresh_t)
    if not lane.email or not fresh_t.enabled:
        print(f"{tenant_id}: LinkedIn prepared and inert: it starts when the record "
              f"has an email channel{'' if fresh_t.enabled else ' and is enabled'}. "
              f"Hours on their clock ({fresh_t.timezone}): {lane.send_hours()}")
        return fresh_t
    print(f"{tenant_id}: LinkedIn on, every {lane.every_hours}h at {lane.send_hours()}, "
          f"{len(lane.sources)} sources, delivering to {lane.email}")
    return fresh_t


def load_agent(owner):
    """Write the plists and load the LinkedIn agent if it is not running.

    The updater writes a new agent's plist and deliberately leaves loading to a
    person, so this is that person's one command."""
    import os
    import subprocess
    from shared import PROJECT
    subprocess.run([sys.executable, str(PROJECT / "deploy" / "install.py"),
                    "--owner", owner], check=True, capture_output=True, text=True)
    uid = os.getuid()
    plist = Path.home() / "Library" / "LaunchAgents" / "com.karamel.linkedin.plist"
    loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/com.karamel.linkedin"],
                            capture_output=True, text=True).returncode == 0
    if loaded:
        print("linkedin agent already running")
        return 0
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True, text=True)
    print("linkedin agent loaded" if r.returncode == 0
          else f"linkedin agent failed to load: {r.stderr.strip()}")
    return r.returncode


def status():
    for t in tenants.list_tenants(include_disabled=True):
        lane = Lane(t)
        if not lane.enabled:
            continue
        drafts = read_jsonl(lane.drafts_path)
        posts = read_jsonl(lane.posts_path)
        when = (f"weekly {lane.schedule['weekday']} {lane.schedule['hour']:02d}:00"
                + (f", {lane.drafts_per_run} in one email" if lane.digest else "")
                if lane.schedule else f"every {lane.every_hours}h at {lane.send_hours()}")
        print(f"{t.id}: {when} "
              f"({len(lane.sources)} sources) -> {lane.email or 'NO EMAIL'}; "
              f"{len(drafts)} drafted, {len(posts)} posted, "
              f"{len(read_jsonl(lane.edits_path))} edits learned")
    return 0


# --- tests ----------------------------------------------------------------------------


def selftest():
    now = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
    rss = f"""<?xml version="1.0"?><rss><channel>
      <item><title>Acme raises $40M for warehouse robots</title>
        <link>https://example.com/a</link>
        <description>&lt;p&gt;Acme &amp;amp; friends, led by Jane Roe.&lt;/p&gt;</description>
        <pubDate>Mon, 19 Oct 2026 12:00:00 +0000</pubDate></item>
      <item><title>Old news</title><link>https://example.com/old</link>
        <pubDate>Mon, 05 Oct 2026 12:00:00 +0000</pubDate></item>
      <item><title>No date</title><link>https://example.com/nodate</link></item>
      <item><title>Not a link</title><link>javascript:x</link>
        <pubDate>Mon, 19 Oct 2026 12:00:00 +0000</pubDate></item>
    </channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Quantum chips</title>
      <link href="https://example.com/q"/><updated>2026-10-19T10:00:00Z</updated>
      <summary>Two labs disagree.</summary></entry></feed>"""
    items = parse_feed(rss, "Feed") + parse_feed(atom, "Atom")
    assert [i["title"] for i in items] == ["Acme raises $40M for warehouse robots",
                                           "Old news", "No date", "Quantum chips"], items
    assert items[0]["summary"] == "Acme & friends, led by Jane Roe.", items[0]["summary"]
    assert parse_feed("<rss><not-closed>", "x") == []
    keep = fresh(items, {"https://example.com/q"}, now)
    assert keep == [], "a 32-character summary is a headline, not something to write about"
    long_items = [dict(i, summary=i["summary"] + " " + "x" * MIN_SUMMARY_CHARS) for i in items]
    keep = fresh(long_items, {"https://example.com/q"}, now)
    assert [i["link"] for i in keep] == ["https://example.com/a"], keep
    hn = parse_feed("""<rss><channel><item><title>Show HN: a thing</title>
      <link>https://example.com/hn</link><pubDate>Mon, 19 Oct 2026 12:00:00 +0000</pubDate>
      <description>&lt;p&gt;Article URL: &lt;a href="https://x.test/a"&gt;https://x.test/a&lt;/a&gt;&lt;/p&gt;
      &lt;p&gt;Comments URL: https://news.ycombinator.com/item?id=1&lt;/p&gt;
      &lt;p&gt;Points: 412&lt;/p&gt;&lt;p&gt;# Comments: 87&lt;/p&gt;</description></item></channel></rss>""", "HN")
    assert hn[0]["summary"] == "", hn[0]["summary"]
    assert fresh(hn, set(), now) == []
    ctx = critic_context(long_items[0], "a stance", 700)
    assert "led by Jane Roe" in ctx and "fabrication" in ctx and "SPECIFIC 1" in ctx
    for name, url in sum(SOURCE_SETS.values(), []):
        assert "hnrss" not in url and "sifted" not in url and "github.blog" not in url, url

    # People are matched on the full name only.
    people = [{"name": "Jane Roe", "cares_about": "industrial automation"},
              {"name": "Roe"}, {"name": "John Smith"}]
    assert [p["name"] for p in people_in(items[0], people)] == ["Jane Roe"]
    note = people_note(people_in(items[0], people), "A Founder")
    assert "industrial automation" in note and "Never open with an ask" in note

    # LinkedIn's refusals.
    assert linkedin_veto("Good point.\n\nRead it here https://x.com/a").startswith("a link")
    assert linkedin_veto("One.\n#ai #vc #deeptech").startswith("3 hashtags")
    assert linkedin_veto("Two tags are fine. #robotics #founders") == ""
    assert "pitch" in linkedin_veto("I'm thrilled to announce our new cohort.\n\nMore soon.")
    assert "bait" in linkedin_veto("LOIs are not customers.\n\nAgree?")
    assert "bait" in linkedin_veto("LOIs are not customers.\n\nThoughts?")
    assert linkedin_veto("LOIs are not customers.\n\nWhat would make you sign one?") == ""
    assert "template" in linkedin_veto("Big week for robots 🚀")
    assert linkedin_veto("") == "empty draft"

    # Replies.
    assert parse_reply("skip") == ("skip", "")
    url = "https://www.linkedin.com/posts/a-founder_robots-activity-7404516245733068800-UCN-"
    assert parse_reply(f"posted {url}") == ("posted", url)
    assert parse_reply(url) == ("posted", url)
    assert parse_reply("posted") == ("posted", "")
    assert parse_reply("edited: LOIs still are not customers.") == (
        "edited", "LOIs still are not customers.")
    assert parse_reply("Shorter please, and lead with the robotics point") == (
        "revise", "Shorter please, and lead with the robotics point")
    assert parse_reply("   ") == (None, "")
    assert post_urn(url) == "urn:li:activity:7404516245733068800"
    assert post_urn("https://www.linkedin.com/feed/update/urn:li:share:7325786486870552578/") == \
        "urn:li:share:7325786486870552578"
    assert post_urn("https://www.linkedin.com/feed/update/urn%3Ali%3AugcPost%3A123456789012/") == \
        "urn:li:ugcPost:123456789012"

    # Draft ids must never look like X draft ids to inbox.py.
    import inbox
    assert not inbox.DRAFT_ID_RE.search("LinkedIn draft #L1729350000000")
    assert DRAFT_ID_RE.search("LinkedIn draft #L1729350000000").group(1) == "1729350000000"

    # The subject is the platform first and never the inbox.py marker.
    s = subject("Draft ready to post", "Founder craft")
    assert s == "[LinkedIn] Draft ready to post: Founder craft"
    assert inbox.SUBJECT_MARKER not in s

    # Lane config, cadence and paths.
    t = tenants.Tenant({"id": "lane-test", "name": "A Founder",
                        "channel": {"type": "email", "address": "k@example.com"},
                        "posting_window": {"start": 8, "end": 23},
                        "linkedin": {"enabled": True, "source_set": "startups_vc_deeptech",
                                     "lanes": ["Founder craft", "Deep tech"]}})
    lane = Lane(t)
    assert lane.enabled and lane.every_hours == 3 and lane.max_chars == 700
    assert lane.email == "k@example.com" and len(lane.sources) == 6
    assert lane.send_hours() == [8, 11, 14, 17, 20], lane.send_hours()
    assert lane.drafts_path.parts[-2:] == ("linkedin", "drafts.jsonl")
    assert not Lane(tenants.Tenant({"id": "x"})).enabled
    off = tenants.Tenant({"id": "y", "linkedin": {"enabled": False}})
    assert not Lane(off).enabled

    # The composer link carries the text and posts nothing by itself.
    u = compose_url("LOIs & customers")
    assert u.startswith("https://www.linkedin.com/feed/?shareActive=true&text=")
    assert "LOIs%20%26%20customers" in u

    # The email body: draft, source for the first comment, reply grammar, id.
    b = body("A Founder", 1729350000000, "The post.", {**items[0]}, "LOIs are not customers.")
    assert "The post." in b and "https://example.com/a" in b and "#L1729350000000" in b
    assert "skip" in b and "edited:" in b and "first comment" in b

    # Learning: verbatim, newest first, deduplicated.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "edits.jsonl"
        for row in ({"kind": "revise", "instruction": "shorter"},
                    {"kind": "revise", "instruction": "less salesy"},
                    {"kind": "revise", "instruction": "Shorter"},
                    {"kind": "edited", "after": "My own version."}):
            append_jsonl(p, row)
        got = lessons(p)
        assert got.index("My own version") < got.index("Shorter") < got.index("less salesy"), got
        assert got.count('"shorter"') + got.count('"Shorter"') == 1, got
        assert lessons(Path(d) / "none.jsonl") == ""

    # The brief: the item, the position, the LinkedIn rules, the lessons.
    br = brief("A Founder", items[0], "Warehouse robots are a sales problem now.", 700,
               taught="HOW THEY HAVE EDITED x", people="PEOPLE IN THIS ITEM y")
    for must in ("Acme raises $40M", "Warehouse robots are a sales problem now.",
                 "DO NOT REUSE THEIR LINES",
                 "first comment", "700 characters", "HOW THEY HAVE EDITED",
                 "PEOPLE IN THIS ITEM", "three-beat"):
        assert must in br, must
    rv = brief("A Founder", items[0], "s", 700, previous="Old draft.", instruction="shorter")
    assert rv.startswith("REVISE") and "Old draft." in rv and '"shorter"' in rv

    # Enabling merges into the record, refuses a lane that could not deliver,
    # and leaves every other field alone.
    with tempfile.TemporaryDirectory() as d:
        real_dir = tenants.TENANT_DIR
        tenants.TENANT_DIR = Path(d)
        try:
            tenants.save_tenant(tenants.Tenant({
                "id": "en-test", "name": "Test",
                "channel": {"type": "email", "address": "t@example.com"},
                "cadence": "hourly", "every_hours": 3}))
            enable("en-test", {"source_set": "founders_ai", "lanes": ["A"]})
            back = tenants.load_tenant("en-test")
            assert Lane(back).enabled and Lane(back).lanes == ["A"]
            assert back.cadence == "hourly" and back.every_hours == 3
            assert back.channel["address"] == "t@example.com"
            tenants.save_tenant(tenants.Tenant({"id": "tg-test",
                                                "channel": {"type": "telegram", "chat_id": "1"}}))
            try:
                enable("tg-test", {"source_set": "founders_ai"})
                raise AssertionError("a lane with nowhere to deliver must be refused")
            except SystemExit:
                pass
            try:
                enable("en-test", {"source_set": "no-such-set", "sources": []})
                raise AssertionError("a lane with no sources must be refused")
            except SystemExit:
                pass
        finally:
            tenants.TENANT_DIR = real_dir

    # The reply loop end to end, model and mail replaced. A change request comes
    # back revised in the same thread and is remembered; "posted" records the
    # post for analytics; "skip" closes the draft.
    from email.message import EmailMessage
    g = globals()
    real_write, real_send = g["write"], g["send"]
    sent = []
    g["write"] = lambda *a, **k: {"final": "Shorter version.", "verdict": "PASS",
                                  "rounds": 0, "scores": {}, "journey": []}
    g["send"] = lambda lane, subj, text, in_reply_to=None: (
        sent.append((subj, text, in_reply_to)) or "<new@karamel>")
    with tempfile.TemporaryDirectory() as d:
        card = Path(d) / "card.md"
        card.write_text("card")
        t = tenants.Tenant({"id": "loop-test", "name": "A Founder", "voice_card": str(card),
                            "channel": {"type": "email", "address": "k@example.com"},
                            "linkedin": {"enabled": True, "source_set": "founders_ai"}})
        _orig_dd = type(t).data_dir
        type(t).data_dir = property(lambda self: Path(d))
        try:
            lane = Lane(t)
            append_jsonl(lane.drafts_path, {
                "draft_id": 1729350000000, "draft_text": "A long first version.",
                "message_id": "<orig@karamel>", "status": "pending", "lane": "Founder craft",
                "stance": "LOIs are not customers.",
                "item": {"title": "T", "source": "S", "link": "https://e.com/a",
                         "summary": "", "published": "2026-10-19T12:00:00+00:00"}})

            def reply(text, to="<orig@karamel>"):
                m = EmailMessage()
                m["Subject"] = "Re: [LinkedIn] Draft ready to post: Founder craft"
                m["From"] = "k@example.com"
                m["In-Reply-To"] = to
                m["Message-ID"] = "<reply@client>"
                m.set_content(text + "\n\nOn Mon wrote:\n> LinkedIn draft #L1729350000000")
                return m

            out = apply_reply(t, reply("Shorter, and lead with the robotics point"))
            assert "revised" in out, out
            rows = read_jsonl(lane.drafts_path)
            assert rows[0]["status"] == "revised" and rows[1]["revised_from"] == 1729350000000
            assert rows[1]["draft_text"] == "Shorter version." and rows[1]["status"] == "pending"
            assert sent[-1][2] == "<reply@client>", "the revision must thread under the reply"
            assert sent[-1][0].startswith("Re: [LinkedIn] Revised draft")
            assert "Shorter version." in sent[-1][1]
            ed = read_jsonl(lane.edits_path)
            assert ed[-1]["kind"] == "revise" and "robotics" in ed[-1]["instruction"]
            assert "robotics point" in lessons(lane.edits_path)

            url = "https://www.linkedin.com/feed/update/urn:li:share:7325786486870552578/"
            out = apply_reply(t, reply(f"posted {url}", to="<new@karamel>"))
            assert "posted" in out, out
            post = read_jsonl(lane.posts_path)[-1]
            assert post["urn"] == "urn:li:share:7325786486870552578"
            assert post["text"] == "Shorter version."

            append_jsonl(lane.drafts_path, {"draft_id": 1729350000999, "draft_text": "x",
                                            "message_id": "<third@karamel>", "status": "pending"})
            assert "skip" in apply_reply(t, reply("skip", to="<third@karamel>"))
            assert read_jsonl(lane.drafts_path)[-1]["status"] == "skipped"
            # A second change sent as a reply to the FIRST email revises the
            # newest version, not the text that email carried.
            g["write"] = lambda *a, **k: (sent.append(("write", k.get("previous"), None))
                                          or {"final": "Third.", "verdict": "PASS",
                                              "rounds": 0, "scores": {}, "journey": []})
            apply_reply(t, reply("even shorter", to="<orig@karamel>"))
            assert ("write", "Shorter version.", None) in sent, sent
            rows = read_jsonl(lane.drafts_path)
            assert rows[-1]["draft_text"] == "Third." and rows[-1]["revised_from"] == rows[1]["draft_id"]
        finally:
            type(t).data_dir = _orig_dd
            g["write"], g["send"] = real_write, real_send

    # The palette is built from approvals only, never from past writing, and
    # stays empty until there are enough of them.
    with tempfile.TemporaryDirectory() as d:
        t = tenants.Tenant({"id": "pal-test", "name": "A Founder",
                            "linkedin": {"enabled": True, "source_set": "founders_ai"}})
        _orig_dd = type(t).data_dir
        type(t).data_dir = property(lambda self: Path(d))
        try:
            lane = Lane(t)
            (Path(d) / "samples.jsonl").write_text('{"text": "old writing"}\n')
            assert approved_texts(t, lane) == [], "past writing must never count"
            append_jsonl(lane.posts_path, {"text": "A post they confirmed."})
            assert approved_texts(t, lane) == ["A post they confirmed."]
            assert palette_line(t, lane) == "", "one approval is not a palette"
        finally:
            type(t).data_dir = _orig_dd

    # Their clock, not New York's: 08:05 Berlin is due, 09:05 Berlin is not,
    # 02:05 New York is 08:05 Berlin and so is due, 03:05 New York is not.
    from zoneinfo import ZoneInfo
    berlin = tenants.Tenant({"id": "tz-test", "timezone": "Europe/Berlin",
                             "posting_window": {"start": 8, "end": 23, "days": "all"},
                             "linkedin": {"enabled": True, "source_set": "devtools_ai"}})
    bl = Lane(berlin)
    assert bl.due(datetime(2026, 10, 20, 8, 5, tzinfo=ZoneInfo("Europe/Berlin")))
    assert not bl.due(datetime(2026, 10, 20, 9, 5, tzinfo=ZoneInfo("Europe/Berlin")))
    assert bl.due(datetime(2026, 10, 20, 2, 5, tzinfo=ZoneInfo("America/New_York")))
    assert not bl.due(datetime(2026, 10, 20, 3, 5, tzinfo=ZoneInfo("America/New_York")))
    assert len(bl.sources) == 7

    we = brief("A Founder", items[0], "s", 700, presence_plural=True,
               allowed_constructions=["not-X-it's-Y contrast"])
    assert '"We" (we, us, our) is part of how' in we and 'never "I"' not in we
    assert "- not-X-it's-Y contrast" not in we
    assert '"We" (we, us, our)' not in brief("A Founder", items[0], "s", 700)
    cheer = Lane(tenants.Tenant({"id": "ch", "linkedin": {
        "enabled": True, "require_feeling": True, "exclamation_is_feeling": True}}))
    assert feels("Huge week for the program!", cheer)
    assert "an exclamation mark does" in feeling_note(cheer, ())
    both = Lane(tenants.Tenant({"id": "b2", "linkedin": {
        "no_exclamation": True, "exclamation_is_feeling": True}}))
    assert not both.exclamation_is_feeling, "a refused mark can never also count"
    assert len(SOURCE_SETS["management_comms"]) == 8

    # A lane can be prepared before there is anywhere to send it.
    with tempfile.TemporaryDirectory() as d:
        real_dir = tenants.TENANT_DIR
        tenants.TENANT_DIR = Path(d)
        try:
            tenants.save_tenant(tenants.Tenant({"id": "prep-test", "enabled": False,
                                                "timezone": "Europe/Berlin"}))
            enable("prep-test", {"source_set": "devtools_ai"}, require_email=False)
            back = tenants.load_tenant("prep-test")
            assert Lane(back).enabled and not Lane(back).email and not back.enabled
            assert "prep-test" not in [t.id for t in lanes_with_linkedin()]
        finally:
            tenants.TENANT_DIR = real_dir

    # Feeling: "!" and emoji never count, a person's own words do, a required
    # feeling is a hard gate, and a feeling cools for the next two drafts.
    with tempfile.TemporaryDirectory() as d:
        t = tenants.Tenant({"id": "feel-test", "linkedin": {
            "enabled": True, "source_set": "devtools_ai", "require_feeling": True,
            "no_exclamation": True, "feeling_words": ["optimistic", "realistically"]}})
        _orig_dd = type(t).data_dir
        type(t).data_dir = property(lambda self: Path(d))
        try:
            lane = Lane(t)
            assert not feels("Agents shipped a release today 🎉", lane)
            assert feels("That makes us optimistic. Realistically, not yet.", lane)
            assert "exclamation" in feeling_veto("We love this!", lane)
            assert feeling_veto("Agents shipped a release today.", lane).startswith("no feeling")
            assert feeling_veto("That makes us optimistic, with a condition.", lane) == ""
            append_jsonl(lane.drafts_path, {"draft_text": "We loved reading that."})
            cooled = recent_feelings(lane)
            assert "loved" in cooled
            assert '"love" again' in feeling_veto("We love the honesty here.", lane, cooled)
            assert feeling_veto("We were suspicious at first, and still optimistic.",
                                lane, cooled) == ""
            note = feeling_note(lane, cooled)
            assert "EVERY POST CARRIES A FEELING" in note and "optimistic" in note
            assert "loved" in note
            off = Lane(tenants.Tenant({"id": "off", "linkedin": {"enabled": True}}))
            assert feeling_veto("A plain sentence.", off) == ""
        finally:
            type(t).data_dir = _orig_dd

    # ---- the weekly digest ------------------------------------------------
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
    wk = Lane(tenants.Tenant({"id": "wk", "linkedin": {
        "enabled": True, "schedule": {"weekday": "sun", "hour": 9}, "digest": True,
        "drafts_per_run": 3, "mix": {"amplify": 80, "own": 20}, "source_set": "founders_ai",
        "banned_zones": ["politic", "white house"]}}))
    assert wk.due(datetime(2026, 10, 25, 9, 30, tzinfo=NY)), "Sunday 09:30"
    assert not wk.due(datetime(2026, 10, 25, 10, 0, tzinfo=NY)), "Sunday 10:00"
    assert not wk.due(datetime(2026, 10, 26, 9, 0, tzinfo=NY)), "Monday 09:00"
    assert wk.send_hours() == [9] and wk.max_age_h == 168
    try:
        Lane(tenants.Tenant({"id": "x", "linkedin": {"schedule": {"weekday": "someday"}}}))
        raise AssertionError("an unknown weekday must fail at load")
    except ValueError:
        pass

    assert zone_hit("A Political Turn at Work", ["politic"]) == "politic"
    assert zone_hit("Briefing at the White House", ["white house"]) == "white house"
    assert zone_hit("Housework and white space", ["white house"]) == ""
    assert zone_hit("A new paper on feedback", ["politic"]) == ""

    assert parse_digest_reply("1 skip\n3 shorter, and lead with the research") == [
        (1, "skip"), (3, "shorter, and lead with the research")]
    assert parse_digest_reply("2. posted https://www.linkedin.com/posts/x-activity-123-a") == [
        (2, "posted https://www.linkedin.com/posts/x-activity-123-a")]
    assert parse_digest_reply("2 edited: first line\nsecond line") == [
        (2, "edited: first line\nsecond line")]
    assert parse_digest_reply("Draft 3: skip") == [(3, "skip")]
    assert parse_digest_reply("looks good to me") == []

    with tempfile.TemporaryDirectory() as d:
        roster = Path(d) / "roster.csv"
        roster.write_text(
            "name,kind,relationship,url,known_for,known_for_url,image_url\n"
            "Ana Colleague,person,Department colleague,u,the course she built,https://x.test/a,\n"
            "Ben Colleague,person,Former student,u,his book on feedback,https://x.test/b,\n"
            "Cee Blank,person,Collaborator,u,,,\n"
            "Prince,person,Friend,u,a line,https://x.test/p,\n"
            "Dee Nolink,person,Friend,u,a line,not-a-url,\n")
        assert [p["name"] for p in load_roster(roster)] == ["Ana Colleague", "Ben Colleague"]

        card = Path(d) / "card.md"
        card.write_text("card")
        t = tenants.Tenant({"id": "dig-test", "name": "Ann Example", "voice_card": str(card),
                            "channel": {"type": "email", "address": "b@example.com"},
                            "linkedin": {"enabled": True, "digest": True, "drafts_per_run": 3,
                                         "schedule": {"weekday": "sun", "hour": 9},
                                         "mix": {"amplify": 80, "own": 20},
                                         "source_set": "founders_ai", "roster": str(roster)}})
        _orig_dd = type(t).data_dir
        type(t).data_dir = property(lambda self: Path(d))
        g = globals()
        real = {k: g[k] for k in ("write", "send", "draft_one", "news_items")}
        sent, n_own = [], [0]

        def fake_write(tenant, lane, voice, item, stance, **k):
            who = (item.get("person") or {}).get("name", "news")
            text = (f"Revised for {who}." if k.get("previous") else f"A post about {who}.")
            return {"final": text, "verdict": "PASS", "rounds": 0, "scores": {}, "journey": []}

        def fake_draft_one(tenant, lane, voice, items, recent, people, log=True, seen_cb=None):
            n_own[0] += 1
            item = {"source": "Feed", "title": f"Story {n_own[0]}", "link": f"https://e.com/{n_own[0]}",
                    "summary": "s" * 200, "published": datetime.now(timezone.utc)}
            r = {"final": f"My own take {n_own[0]}.", "verdict": "PASS", "rounds": 0,
                 "scores": {}, "journey": []}
            return r, item, "a stance", "Founder craft", "why", items, []

        g["write"], g["draft_one"] = fake_write, fake_draft_one
        g["news_items"] = lambda lane, now, fetch=None: []
        g["send"] = lambda lane, subj, text, in_reply_to=None: (
            sent.append((subj, text, in_reply_to)) or f"<m{len(sent)}@k>")
        try:
            lane = Lane(t)
            assert amplify_count(lane, 3) == 2
            assert run_digest(t, lane, "card") == 3
            assert len(sent) == 1, "three drafts, one email"
            subj, body1, _ = sent[0]
            assert subj == "[LinkedIn] Your 3 drafts for this week"
            for must in ("Draft 1 of 3: Crediting Ana Colleague", "Draft 2 of 3: Crediting Ben Colleague",
                         "Draft 3 of 3: Your take: Founder craft", "2 crediting people you know, 1 of your own",
                         "Reply with the draft's number first", "https://x.test/a"):
                assert must in body1, must
            assert body1.count("LinkedIn draft #L") == 3
            rows = read_jsonl(lane.drafts_path)
            assert [r["kind"] for r in rows] == ["amplify", "amplify", "own"]
            assert all(r["digest"] and r["digest_mid"] == "<m1@k>" for r in rows)
            assert [r["position"] for r in rows] == [1, 2, 3]

            # Week two: the ratio asks for three credits, the roster has none
            # new, so all three are his own take and the email says why.
            assert amplify_count(lane, 3) == 3
            assert choose_to_credit(lane, load_roster(roster), 3) == []
            run_digest(t, lane, "card")
            assert "nothing new to credit for 3 of this week's slots" in sent[-1][1]
            # Rewriting a line offers that person again.
            roster.write_text(roster.read_text().replace("the course she built",
                                                         "the new course she built"))
            assert [p["name"] for p in choose_to_credit(lane, load_roster(roster), 3)] == ["Ana Colleague"]

            # Replies by number, inside the first digest.
            from email.message import EmailMessage
            m = EmailMessage()
            m["Subject"] = "Re: [LinkedIn] Your 3 drafts for this week"
            m["From"] = "b@example.com"
            m["In-Reply-To"] = "<m1@k>"
            m["Message-ID"] = "<reply@client>"
            m.set_content("1 skip\n3 shorter, and lead with the research\n\nOn Sun wrote:\n> " + body1[:80])
            out = apply_reply(t, m)
            assert "skip" in out and "revised" in out, out
            rows = read_jsonl(lane.drafts_path)
            assert rows[0]["status"] == "skipped"
            third = [r for r in rows if r.get("position") == 3 and r.get("digest_mid") == "<m1@k>"]
            assert third[0]["status"] == "revised"
            new = [r for r in rows if r.get("revised_from") == third[0]["draft_id"]][0]
            assert new["draft_text"] == "Revised for news." and new["digest"] is False
            assert sent[-1][2] == "<reply@client>" and "Revised" in sent[-1][0]

            # A reply that names no draft gets one question back, nothing changed.
            m2 = EmailMessage()
            m2["Subject"], m2["From"], m2["In-Reply-To"] = "Re: x", "b@example.com", "<m1@k>"
            m2["Message-ID"] = "<reply2@client>"
            m2.set_content("make them warmer")
            before = read_jsonl(lane.drafts_path)
            assert "no draft number" in apply_reply(t, m2)
            assert read_jsonl(lane.drafts_path) == before
            assert sent[-1][0].endswith("Which draft?")
        finally:
            type(t).data_dir = _orig_dd
            g.update(real)

    # A halt stops the whole agent: no replies read, no drafts written.
    import karamel_common
    real_paused, calls = karamel_common.is_paused, []
    real_inbox, real_run = g["run_inbox"], g["run_tenant"]
    karamel_common.is_paused = lambda tenant=None: True
    g["run_inbox"] = lambda *a, **k: calls.append("inbox")
    g["run_tenant"] = lambda *a, **k: calls.append("draft")
    try:
        assert tick() == 0 and calls == [], calls
    finally:
        karamel_common.is_paused = real_paused
        g["run_inbox"], g["run_tenant"] = real_inbox, real_run

    # The stance is asked for in the person's own shape. "Stated plainly"
    # pushed every one toward a closed verdict, which is the shape that failed
    # TAKE three rounds running on a card whose move is the conditional reframe.
    import inspect
    src = inspect.getsource(pick)
    assert "stated plainly" not in src and "shape their voice card" in src

    print("linkedin selftest: ok")
    return 0


def _sample(path):
    """Draft once for a tenant record on disk, print it, send and write nothing."""
    data = json.loads(Path(path).read_text())
    t = tenants.Tenant(data)
    lane = Lane(t)
    voice = t.voice_card_path.read_text()
    items = fresh(fetch_items(lane.sources), set(), datetime.now(timezone.utc))
    r, item, stance, lane_name, why, _, failures = draft_one(
        t, lane, voice, items, [], load_people(lane.people_path), log=False)
    for f_item, f_stance, f_r in failures:
        print(f"DROPPED: {f_item['title']} ({f_item['source']})\n  stance: {f_stance}")
        for j in f_r["journey"]:
            print(f"  round {j['round']}: {j['verdict']} "
                  f"{(j.get('override') or j.get('why', ''))[:220]}")
    if r is None:
        print("nothing passed the gate this slot")
        return 1
    print(f"SUBJECT: {subject('Draft ready to post', lane_name)}")
    print(f"ITEM: {item['title']} ({item['source']}, {item['published']:%d %b})")
    print(f"STANCE: {stance}")
    print(f"VERDICT: {r['verdict']} after {r['rounds']} round(s); scores {r['scores']}")
    for j in r["journey"]:
        print(f"  round {j['round']}: {j['verdict']} "
              f"{(j.get('override') or j.get('why', ''))[:220]}")
    print(f"CHARS: {len(r['final'])}\n\n{r['final']}")
    return 0 if r["verdict"] == "PASS" else 2


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--tick" in a:
        return tick()
    if "--inbox" in a:
        return run_inbox(dry="--dry-run" in a)
    if "--status" in a:
        return status()
    if "--enable" in a:
        i = a.index("--enable")
        enable(a[i + 1], json.loads(a[i + 2]) if len(a) > i + 2 else {})
        return 0
    if "--person" in a:
        # --person ID "Full Name" "what they care about" ["how they know them"]
        i = a.index("--person")
        t = tenants.load_tenant(a[i + 1])
        if t is None or len(a) < i + 4 or len(a[i + 2].split()) < 2:
            print("usage: --person ID \"Full Name\" \"what they care about\" "
                  "[\"how they know them\"] (a full name, two words or more)",
                  file=sys.stderr)
            return 2
        row = {"name": a[i + 2], "cares_about": a[i + 3],
               "relationship": a[i + 4] if len(a) > i + 4 else "",
               "source": "given by the account holder", "added": now_iso()}
        append_jsonl(Lane(t).people_path, row)
        print(f"{t.id}: will contextualise drafts that mention {row['name']}")
        return 0
    if "--prepare" in a:
        i = a.index("--prepare")
        enable(a[i + 1], json.loads(a[i + 2]) if len(a) > i + 2 else {},
               require_email=False)
        return 0
    if "--load-agent" in a:
        return load_agent(tenants.LEGACY_TENANT)
    if "--sample" in a:
        return _sample(a[a.index("--sample") + 1])
    if "--run" in a:
        t = tenants.load_tenant(a[a.index("--run") + 1])
        if t is None:
            print("no such tenant", file=sys.stderr)
            return 2
        return 0 if run_tenant(t, dry="--dry-run" in a) >= 0 else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
