"""DISCOVER — who exists, and how visible they are.

The only stage that costs money, so it is the one with a provider menu, a
mandatory cost estimate, and a budget check inside the loop.

Two shapes of discovery, and the difference matters:

* **Listings** ask "which businesses of this type are in this place?" and come
  back with a phone, a category, a rating and — crucially — whether there is a
  website at all. For local SMB verticals this is the primary path.
* **SERP** asks "who ranks for this search?" and comes back with a rank
  position. You are buying the *signal*, not the list: positions 11-30 are
  businesses visible enough to be trying and not winning, which is the most
  useful prospecting signal there is.

Cost discipline lives here, not in the database. A table can only record spend
after the fact; only the collector can decline to spend it. Every run
estimates first, checks a budget between batches, reads the kill switch, and
flushes observations per batch — a long run that dies must not lose what it
already learned.
"""

import asyncio
import inspect
import os
import time
import base64
import logging
import math
import re
from html import unescape
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from sellbird.models import Company

logger = logging.getLogger("sellbird.collectors.discover")

TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)


# ── Junk filtering ────────────────────────────────────────────────────
# Discovery fills a database with directories, hospital systems, media and
# lead-gen middlemen unless you filter hard. Match PATTERNS, not a literal
# list: a literal list is exactly how `whatclinic.com` and `ufhealth.org` get
# through on the first run.

# Anchored on a dot boundary, not the string start: SERP results arrive as
# `en.wikipedia.org` and `m.facebook.com` at least as often as bare domains,
# and `^(www\.)?` catches neither.
JUNK_DOMAIN_PATTERNS = tuple(re.compile(p, re.I) for p in (
    # directories & marketplaces
    r"(^|\.)(yelp|yellowpages|superpages|manta|bbb|angi|angieslist|homeadvisor)\.",
    r"(^|\.)(thumbtack|houzz|porch|buildzoom|networx|modernize|nextdoor)\.",
    r"(^|\.)(facebook|instagram|twitter|x|linkedin|youtube|tiktok|pinterest)\.",
    r"(^|\.)(meetup|eventbrite)\.",
    r"(^|\.)(indeed|glassdoor|ziprecruiter|craigslist|zillow|realtor)\.",
    r"(^|\.)(mapquest|foursquare|tripadvisor|trustpilot|birdeye|expertise)\.",
    r"(directory|listings?|find-?a-|top\d+|best-?of)\.",
    # Job boards, directories, and marketplaces.
    r"(^|\.)(naukri|jobrapido|jobsora|empleos|shine|foundit|timesjobs|freelancer|upwork)\.",
    # Indian directories & marketplaces
    r"(^|\.)(indiamart|justdial|tradeindia|exportersindia|sulekha|urbanpro|yet5)\.",
    r"(^|\.)(olx|quikr|flipkart|snapdeal|myntra|shopclues|etsy|ebay|alibaba|aliexpress)\.",
    # hobby communities & model repositories — never a prospect
    r"(^|\.)(boardgamegeek|team-bhp|hackaday|instructables|thingiverse|printables|cults3d|thangs|grabcad)\.",
    # media & reference
    r"(^|\.)(wikipedia|reddit|quora|medium|substack|forbes|inc|entrepreneur)\.",
    r"(news|magazine|journal|gazette|tribune|herald)\.(com|org|net)$",
    # institutional
    r"\.(edu|gov|mil)$",
    r"(hospital|healthsystem|medicalcenter)\.",
    r"health\.org$",
    # startup databases, launch sites and tech press (the YC preset's noise)
    r"(^|\.)(ycombinator|workatastartup|startupschool|yclist|ycdb|seedtable)\.",
    r"(^|\.)(crunchbase|techcrunch|wellfound|angel|tracxn|pitchbook)\.",
    r"(^|\.)(cbinsights|producthunt|f6s|dealroom|growjo|owler|zoominfo)\.",
    r"(^|\.)github\.com$",
    # hosted job boards: the company is whoever's board it is, not the host
    r"(^|\.)(job-)?boards\.(eu\.)?greenhouse\.io$",
    r"(^|\.)jobs\.(eu\.)?lever\.co$",
    r"(^|\.)jobs\.ashbyhq\.com$",
    r"(^|\.)(apply|jobs)\.workable\.com$",
    r"(^|\.)(jobs|careers)\.smartrecruiters\.com$",
    r"(^|\.)jobs\.jobvite\.com$",
    r"(^|\.)ats\.rippling\.com$",
    r"(^|\.)(myworkdayjobs|applytojob)\.com$",
    # a company's board on these lives on a subdomain; the bare domain is
    # the vendor, which may be someone's prospect
    r"\.(icims|bamboohr|recruitee|teamtailor)\.com$",
    r"\.breezy\.hr$",
    r"\.jobs\.personio\.(de|com)$",
    # job sites named for the job: jobs.com, careers.co.uk
    r"^(jobs|careers)\.((co|com|org|net|ac)\.)?[a-z]{2,}$",
    # accelerators' own sites (the accelerator preset's noise)
    r"(^|\.)(techstars|antler|joinef|southparkcommons|aigrant|a16z|pioneer|plugandplaytechcenter|plugandplay)\.",
    r"(^|\.)(500\.co|neo\.com)$",
    # funding news, wires and round trackers (the funding preset's noise)
    r"(^|\.)(prnewswire|businesswire|globenewswire|accesswire|einpresswire|newswire|finsmes|axios)\.",
    r"(^|\.)(venturebeat|siliconangle|eu-startups|sifted|geekwire|thesaasnews|fortune|bloomberg)\.",
    r"(^|\.)(reuters|wsj|cnbc|businessinsider|privco|recentfunding|pulse2|startupfundraising)\.",
    r"(^|\.)(startupintros|openvc|siliconcanals|startupdaily|e27|similarweb|vcbacked|roundos)\.",
    r"(^|\.)(techfundingnews|startups\.gallery)",
    r"(^|\.)tech\.eu$",
    # infrastructure that is never a prospect
    r"(^|\.)(google|bing|yahoo|duckduckgo|amazon|apple|microsoft)\.",
    r"(wordpress|wix|squarespace|shopify|godaddy|weebly|webflow)\.com$",
))

# Names that mean the row is a category page or an aggregator, not a business.
JUNK_NAME_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"^\s*(top|best|\d+)\s+\d*\s*(best\s+)?\w+.*\b(in|near|of)\b",
    r"\b(directory|listings?|reviews? of|compare|find a)\b",
    # "Ohio HOA Management Company List", "List of Plumbers in Austin"
    r"\b(compan(y|ies)|firms?|contractors?|businesses|providers?)\s+list\b",
    r"\blist of\b",
))


def normalize_domain(url_or_domain: str) -> str:
    """A bare, lowercase, www-less hostname — the entity-resolution key.

    Entity resolution decides whether any of this is real. Get it wrong and
    every trend built on top is garbage.
    """
    if not url_or_domain:
        return ""
    raw = url_or_domain.strip().lower()
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


# ── Business names ────────────────────────────────────────────────────
# SERP providers return a page TITLE, not a business name: "Denver, CO | HOA
# Management Firm", "Home Page - NVCM | Community Association Management".
# Stored as-is it lands in an email as "...at Denver, CO | HOA Management
# Firm". The business is the title segment that shares a DISTINCTIVE word with
# the domain — "management" matches managementharbor.com but says nothing.

_TITLE_SEPARATORS = re.compile(r"\s+[|\-–—·•]\s+|\s*[|•·]\s*|:\s+")
_GENERIC_NAME_WORDS = frozenset("""
home page welcome official site website hoa hoas management manager managers
mgmt mgt company companies co inc llc ltd corp firm group services service
property properties community communities association associations condo
condominium residential homeowners owners real estate the and of in for best
top leading trusted local full arizona phoenix az texas tx florida fl
california ca nevada nv colorado co georgia ga carolina nc usa us america
roofing roof roofers roofer plumbing plumber plumbers contact about
""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w]


def _domain_stem(domain: str) -> str:
    host = normalize_domain(domain)
    return re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0]) if host else ""


def _segment_matches_domain(segment: str, stem: str) -> bool:
    # Navigation and service/location labels can share an incidental word
    # with a domain without identifying the business.
    if re.fullmatch(r"(?i)(contact(?: us)?|about(?: us)?|home(?: page)?)", segment.strip()):
        return False
    if re.search(r"(?i)\bservices?\s+(?:in|near)\s+\S", segment):
        return False
    words = _words(segment)
    distinctive = [w for w in words if w not in _GENERIC_NAME_WORDS]
    if any(len(w) >= 3 and w in stem for w in distinctive):
        return True
    # "Northvale Association of Community Managers" ↔ nacm.org
    initials = "".join(w[0] for w in words if w not in ("of", "the", "and", "&"))
    return len(initials) >= 3 and stem.startswith(initials)


def _segment_domain_evidence(segment: str, stem: str) -> int:
    """Amount of distinctive domain evidence, shared by SERP and homepage names."""
    if not _segment_matches_domain(segment, stem):
        return 0
    return sum(len(w) for w in set(_words(segment))
               if w not in _GENERIC_NAME_WORDS and len(w) >= 3 and w in stem)


_SPLIT_WORDS = sorted((w for w in _GENERIC_NAME_WORDS | {"with", "your"}
                       if len(w) >= 4), key=len, reverse=True)


def _split_stem(stem: str) -> list[str]:
    """managementharbor → [management, harbor]; kqresidential → [kq, residential]."""
    # Find every occurrence of every split word in one sweep; scanning the
    # words longest-first keeps the longest word for each start position.
    starts: dict[int, str] = {}
    for w in _SPLIT_WORDS:
        i = stem.find(w)
        while i >= 0:
            starts.setdefault(i, w)
            i = stem.find(w, i + 1)
    parts, start = [], 0
    for i in sorted(starts):
        if i >= start:
            if start < i:
                parts.append(stem[start:i])
            word = starts[i]
            parts.append(word)
            start = i + len(word)
    if start < len(stem):
        parts.append(stem[start:])
    return parts


def name_from_domain(domain: str) -> str:
    stem = normalize_domain(domain).rsplit(".", 1)[0]
    words = [w for chunk in re.split(r"[-_.]+", stem) if chunk for w in _split_stem(chunk)]
    # Short vowel-less leftovers are initials: "kq" → "KQ".
    return " ".join(w.upper() if len(w) <= 3 and not re.search(r"[aeiouy]", w)
                    else w.capitalize() for w in words)


def name_matches_domain(name: str, domain: str) -> bool:
    """True when the stored name demonstrably belongs to this domain."""
    stem = _domain_stem(domain)
    return bool(stem and name and _segment_matches_domain(name, stem))


def site_name_from_html(html: str, domain: str) -> str:
    """The business name a homepage gives itself (og:site_name, then <title>),
    or "" when neither names it in a way the domain backs up."""
    stem = _domain_stem(domain)
    candidates = []
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:site_name',
                r"(?is)<title[^>]*>(.*?)</title>"):
        m = re.search(pat, html or "", re.I)
        if m:
            candidates.append(unescape(re.sub(r"\s+", " ", m.group(1))).strip())
    # Distinctive brand evidence wins; equally backed names prefer the fullest
    # form: "The Management Harbor" over og's "Management Harbor".
    matches = [seg for text in candidates
               for seg in (x.strip(" ,.-") for x in _TITLE_SEPARATORS.split(text))
               if seg and len(seg) <= 60 and _segment_matches_domain(seg, stem)]
    return max(matches, key=lambda seg: (_segment_domain_evidence(seg, stem), len(seg)),
               default="")


def business_name(title: str, domain: str) -> str:
    """The business's name from a SERP title, or one derived from its domain."""
    stem = _domain_stem(domain)
    title = re.sub(r"\s*(\.\.\.|…)\s*$", "", (title or "").strip())
    if stem and title:
        segments = [segment.strip(" ,.-") for segment in _TITLE_SEPARATORS.split(title)]
        matches = [seg for seg in segments
                   if seg and len(seg) <= 60 and _segment_matches_domain(seg, stem)]
        # Prefer the segment with the most distinctive domain evidence rather
        # than the first incidental substring. Stable ties preserve title order.
        if matches:
            return max(matches, key=lambda seg: _segment_domain_evidence(seg, stem))
    return name_from_domain(domain) if stem else title


def is_junk(domain: str, name: str = "") -> bool:
    """True when this row would pollute the database rather than fill it."""
    if domain:
        if any(p.search(domain) for p in JUNK_DOMAIN_PATTERNS):
            return True
        # A bare TLD or an IP address is never a business we can profile.
        if "." not in domain or re.fullmatch(r"[\d.]+", domain):
            return True
    if name and any(p.search(name) for p in JUNK_NAME_PATTERNS):
        return True
    return False


# ── Geocoding ─────────────────────────────────────────────────────────
# Listings providers search a radius, not a place name, so "Denver, CO" has to
# become coordinates. Nominatim allows exactly this (single place lookups at
# <=1 req/s) and forbids bulk enumeration, so every result is cached forever in
# the settings table: a given city is looked up once, ever.

NOMINATIM = "https://nominatim.openstreetmap.org/search"
GEOCODE_UA = "Sellbird/0.1 (open-source sales agent; github.com/ethanplusai/sellbird)"


async def geocode_city(state, city: str, radius_km: int = 40,
                       client: httpx.AsyncClient | None = None) -> str:
    """"Denver, CO" -> "39.7392,-104.9903,40". Cached permanently."""
    if not city:
        return ""
    cache_key = f"geo:{city.strip().lower()}"
    cached = await state.get_setting(cache_key)
    if cached:
        # A previous miss is cached as "-" so we don't re-ask every run.
        return "" if cached == "-" else f"{cached},{radius_km}"

    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await client.get(
            NOMINATIM,
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": GEOCODE_UA},
        )
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        logger.warning("geocode failed for %r: %s", city, e)
        return ""
    finally:
        if owned:
            await client.aclose()

    if not hits:
        await state.set_setting(cache_key, "-")
        logger.warning("geocode: no match for %r", city)
        return ""

    point = f"{float(hits[0]['lat']):.4f},{float(hits[0]['lon']):.4f}"
    await state.set_setting(cache_key, point)
    return f"{point},{radius_km}"


async def resolve_coordinates(state, queries: list["DiscoveryQuery"],
                              radius_km: int = 40,
                              client: httpx.AsyncClient | None = None
                              ) -> list["DiscoveryQuery"]:
    """Fill in coordinates for any query that lacks them.

    Configured ``icp.geo_coordinates`` always wins; this only covers what the
    user did not spell out. Distinct cities are geocoded once each, however
    many queries share them.
    """
    needed = {q.location for q in queries if q.location and not q.coordinate}
    points: dict[str, str] = {}
    for i, city in enumerate(sorted(needed)):
        if i:
            await asyncio.sleep(1.1)     # Nominatim: max 1 request/second
        points[city] = await geocode_city(state, city, radius_km, client)

    for query in queries:
        if not query.coordinate:
            query.coordinate = points.get(query.location, "")
    return queries


# ── The record every provider returns ─────────────────────────────────


@dataclass
class Business:
    """One discovered business, normalised across providers."""

    name: str = ""
    domain: str = ""
    website: str = ""
    phone: str = ""
    address: str = ""
    location: str = ""
    category: str = ""
    rating: float | None = None
    review_count: int | None = None
    is_claimed: bool | None = None
    has_website: bool | None = None
    rank: int | None = None          # organic rank, when the provider gives one
    local_pack_rank: int | None = None   # slot in Google's local 3-pack (ads excluded)
    maps_rank: int | None = None     # position in Google Maps results
    keyword: str = ""                # the search that produced the ranks above
    external_id: str = ""            # "<provider>:<stable id>"
    alt_ids: tuple[str, ...] = ()    # other ids the same business was stored under
    source_url: str = ""
    provider: str = ""
    raw_title: str = ""             # SERP evidence for junk filtering before normalization

    def identity(self) -> str:
        return self.domain or self.external_id


@dataclass
class DiscoveryQuery:
    """What to look for, and where."""

    term: str                        # "roofing contractor"
    location: str = ""               # "Denver, CO"
    category: str = ""               # provider-native category slug, optional
    coordinate: str = ""             # "lat,lng,radius_km" for listings providers
    depth: int = 30                  # SERP depth; ignored by listings providers
    limit: int = 100                 # max records to take from one query
    preset: str = ""                 # PRESETS key that built it, if any

    def keyword(self) -> str:
        return f"{self.term} {self.location}".strip()


# DataForSEO names places "Denver,Colorado,United States" — full state name,
# no spaces after commas. Sellbird's geography is written the way people write
# it ("Denver, CO"), so US state abbreviations are expanded here. Anything
# else passes through with its commas tightened; a name DataForSEO doesn't
# know comes back as a task error, which the run reports.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_US_STATE_NAMES = {v.lower(): v for v in US_STATES.values()}

# DataForSEO requires a location on every SERP task. A search with no place
# at all (the non-local YC preset) is a United States search.
DEFAULT_LOCATION_CODE = 2840


def dataforseo_location_name(place: str) -> str:
    """"Denver, CO" -> "Denver,Colorado,United States"."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if len(parts) == 2:
        state = US_STATES.get(parts[1].upper()) or _US_STATE_NAMES.get(parts[1].lower())
        if state:
            return f"{parts[0]},{state},United States"
    return ",".join(parts)


def _dfs_location(query: "DiscoveryQuery", point_suffix: str) -> dict:
    """The location fields for a DataForSEO SERP task.

    A geocoded point wins: rank depends on where the searcher stands, and a
    city-centre point is the reproducible version of "searching from Denver".
    The stored coordinate's third part is the listings radius in km, which
    means nothing here, so only lat,lng are kept.
    """
    if query.coordinate:
        lat, lng = (query.coordinate.split(",") + [""])[:2]
        if lat.strip() and lng.strip():
            return {"location_coordinate": f"{lat.strip()},{lng.strip()},{point_suffix}"}
    if query.location:
        return {"location_name": dataforseo_location_name(query.location)}
    return {"location_code": DEFAULT_LOCATION_CODE}


# ── Provider adapters ─────────────────────────────────────────────────


# Match whole placeholder values, never substrings of opaque credentials.
_PLACEHOLDER_RE = re.compile(
    r"(?i)(placeholder|change[-_]?me|your[_-](?:api[_-])?key(?:[_-]here)?"
    r"|paste[_-](?:api[_-])?key(?:[_-]here)?|x{4,}|<[a-z_ -]+>"
    r"|todo|fixme|example[_-](?:api[_-])?key)"
)


class ProviderExhausted(RuntimeError):
    """This provider can take no more searches in this run: its allowance or
    rate limit is used up, or it refused the key. The run stops and says why,
    rather than failing every remaining phrase the same way."""


class DiscoveryProvider:
    """One way of answering "who exists".

    Subclasses implement ``fetch``. Everything else — the menu metadata the
    dashboard renders, cost estimation, configuration checks — lives here so a
    new provider is a small class rather than a wiring exercise.
    """

    key: str = ""
    label: str = ""
    kind: str = "listings"           # listings | serp
    blurb: str = ""
    cost_note: str = ""
    free_tier: str = ""
    signup_url: str = ""
    env_keys: tuple[str, ...] = ()
    caveat: str = ""
    # Geocode cities before the run (listings search a radius; the
    # DataForSEO rank providers search from a point).
    needs_coordinates: bool = False
    # Rank signals this provider measures. For each one, a search that no
    # longer finds a business it found before records that, per phrase.
    rank_codes: tuple[str, ...] = ()

    def missing_keys(self, env: dict) -> list[str]:
        """Env keys that are absent, empty, or still the template placeholder.

        An empty ``SERPER_API_KEY=`` line used to pass the truthiness check,
        the run then "succeeded" with zero queries, and the process exited 0
        — a silent no-op that reads as success. Name the bad keys here,
        before any money or time is spent.
        """
        def _missing(value) -> bool:
            if not isinstance(value, str):
                return not value
            value = value.strip()
            return (not value) or bool(_PLACEHOLDER_RE.fullmatch(value))

        return [k for k in self.env_keys if _missing(env.get(k))]

    def configured(self, env: dict) -> bool:
        return not self.missing_keys(env)

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        """Projected spend, in dollars, before anything is called."""
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        raise NotImplementedError

    # Shared helper: providers report their own per-query spend so the run log
    # records what actually happened, not what was projected.
    last_cost: float = 0.0
    last_error: str = ""
    stop_reason: str = ""
    complete: bool = True
    remaining_budget: float = math.inf


class DataForSEOListings(DiscoveryProvider):
    """Google Business Profile data as a queryable database, not a scrape.

    The cheapest way to enumerate local businesses anywhere, and the only one
    with a filter for "has no website" — which for anyone selling websites is
    the highest-intent query available.
    """

    key = "dataforseo_listings"
    label = "DataForSEO Business Listings"
    kind = "listings"
    needs_coordinates = True
    blurb = ("Enumerate every business of a type within a radius, with phone, "
             "domain, rating and whether the listing is claimed. Best choice "
             "for local trades, clinics and contractors.")
    cost_note = "$0.372 per 1,000 businesses ($0.012/query + $0.00036 each)"
    free_tier = "$1 signup credit; $50 minimum deposit to go live"
    signup_url = "https://app.dataforseo.com/api-access"
    env_keys = ("dataforseo_login", "dataforseo_password")
    caveat = "No email addresses — Sellbird resolves those in a later stage."

    TASK_COST = 0.012
    ITEM_COST = 0.00036

    def estimate(self, queries):
        return sum(self.TASK_COST + self.ITEM_COST * q.limit for q in queries)

    def _auth(self, env: dict) -> str:
        raw = f"{env['dataforseo_login']}:{env['dataforseo_password']}"
        return base64.b64encode(raw.encode()).decode()

    def base_url(self, env: dict) -> str:
        # The sandbox mirrors the response schema exactly and is never
        # charged — the right target for a first run and for the test suite.
        return ("https://sandbox.dataforseo.com/v3"
                if env.get("dataforseo_sandbox") else
                "https://api.dataforseo.com/v3")

    async def fetch(self, client, env, query):
        payload = {
            "limit": min(query.limit, 1000),
            "order_by": ["rating.votes_count,desc"],
        }
        if query.category:
            payload["categories"] = [query.category]
        else:
            payload["title"] = query.term
        if query.coordinate:
            payload["location_coordinate"] = query.coordinate
        elif query.location:
            # Without coordinates the API can only match on the title, so say
            # so rather than silently returning nationwide results.
            payload["description"] = query.location

        r = await client.post(
            f"{self.base_url(env)}/business_data/business_listings/search/live",
            headers={"Authorization": f"Basic {self._auth(env)}",
                     "Content-Type": "application/json"},
            json=[payload],
        )
        r.raise_for_status()
        data = r.json()
        self.last_cost = float(data.get("cost") or 0.0)
        return [self._to_business(item) for item in _dfs_items(data)]

    def _to_business(self, item: dict) -> Business:
        url = item.get("url") or ""
        rating = (item.get("rating") or {})
        addr = item.get("address_info") or {}
        city = ", ".join(x for x in (addr.get("city"), addr.get("region")) if x)
        return Business(
            name=item.get("title") or "",
            domain=normalize_domain(item.get("domain") or url),
            website=url,
            phone=item.get("phone") or "",
            address=item.get("address") or "",
            location=city,
            category=item.get("category") or "",
            rating=rating.get("value"),
            review_count=rating.get("votes_count"),
            is_claimed=item.get("is_claimed"),
            has_website=bool(url),
            external_id=_gbp_ids(item)[0],
            alt_ids=_gbp_ids(item)[1],
            source_url=item.get("check_url") or "",
            provider=self.key,
        )


class DataForSEOSerp(DiscoveryProvider):
    """Organic rank — and the local 3-pack above it — as the buying signal.

    Buy the rank, not the list. A business sitting at 11-30 for its own core
    search is visible enough to have tried and is losing — the single most
    useful thing you can know before writing to them. The same response
    carries the local pack, so who holds those three slots comes free.
    """

    key = "dataforseo_serp"
    label = "DataForSEO SERP (organic rank)"
    kind = "serp"
    needs_coordinates = True
    blurb = ("Search '<service> <city>' from the city centre and record where "
             "each business ranks, plus who holds the local 3-pack. Discovers "
             "businesses you didn't have AND scores the ones you did.")
    cost_note = "$0.002 per 10 results (Live) — depth 30 costs $0.006 a search"
    free_tier = "$1 signup credit; free sandbox; $50 minimum deposit to go live"
    signup_url = "https://app.dataforseo.com/api-access"
    env_keys = ("dataforseo_login", "dataforseo_password")
    caveat = ("Google removed 100-results-per-page in September 2025, so depth "
              "100 now costs 10x depth 10. Depth 20-30 is the sweet spot; "
              "below rank 30 it is mostly directories anyway. Rank depends on "
              "where the searcher stands — this is the view from the city centre.")

    # Live mode. The standard queue is $0.0006 but asynchronous (post a task,
    # poll for it minutes later); the run loop checks the spend cap between
    # synchronous calls, so Live keeps that check honest.
    PAGE_COST = 0.002
    # The radius of the search point. DataForSEO documents it in mm
    # (199-199999), so 2000 is 2 m: a pinpoint at the city centre, which is
    # what "when we searched from downtown Denver" means. Chosen so that if
    # the unit were metres it would still be 2 km, still downtown.
    POINT_RADIUS = "2000"
    rank_codes = ("SERP_RANK", "IN_LOCAL_PACK")

    def estimate(self, queries):
        # Billing is base price x number of pages, 10 results to a page.
        return sum(self.PAGE_COST * max(1, -(-q.depth // 10)) for q in queries)

    _auth = DataForSEOListings._auth
    base_url = DataForSEOListings.base_url

    async def _post(self, client, env, endpoint, task):
        r = await client.post(
            f"{self.base_url(env)}/{endpoint}",
            headers={"Authorization": f"Basic {self._auth(env)}",
                     "Content-Type": "application/json"},
            json=[task],
        )
        r.raise_for_status()
        data = r.json()
        self.last_cost = float(data.get("cost") or 0.0)
        return _dfs_items(data)

    async def fetch(self, client, env, query):
        keyword = query.keyword()
        items = await self._post(client, env, "serp/google/organic/live/advanced", {
            "keyword": keyword, "language_code": "en", "depth": query.depth,
            "device": "desktop", **_dfs_location(query, self.POINT_RADIUS)})

        # One Business per identity: a company in the pack AND at organic 14
        # is one prospect with two facts, not two prospects.
        found: dict[str, Business] = {}
        for item in items:
            kind = item.get("type")
            if kind == "organic":
                domain = normalize_domain(item.get("domain") or item.get("url") or "")
                if not domain:
                    continue
                biz = found.setdefault(domain, Business(
                    name=business_name(item.get("title") or "", domain),
                    raw_title=item.get("title") or "",
                    domain=domain, website=item.get("url") or "",
                    location=query.location, has_website=True,
                    source_url=item.get("url") or "",
                    provider=self.key, keyword=keyword))
                # rank_group is position within organic results.
                # `position` in this payload means left/right column, NOT rank.
                biz.rank = _best(biz.rank, item.get("rank_group"))
            elif kind == "local_pack" and not item.get("is_paid"):
                domain = normalize_domain(item.get("domain") or item.get("url") or "")
                cid = item.get("cid") or ""
                ident = domain or (f"dataforseo:cid:{cid}" if cid else "")
                if not ident:
                    continue
                rating = item.get("rating") or {}
                biz = found.get(ident)
                if biz is None:
                    biz = found[ident] = Business(
                        name=item.get("title") or "", raw_title=item.get("title") or "",
                        domain=domain, website=item.get("url") or "",
                        location=query.location,
                        # No website button in the pack is not proof there
                        # is no website, so this stays unknown.
                        has_website=True if domain else None,
                        external_id="" if domain else ident,
                        source_url=item.get("url") or "",
                        provider=self.key, keyword=keyword)
                biz.phone = biz.phone or item.get("phone") or ""
                if rating.get("value") is not None:
                    biz.rating = rating.get("value")
                    biz.review_count = rating.get("votes_count")
                biz.local_pack_rank = _best(biz.local_pack_rank, item.get("rank_group"))
        return list(found.values())


class DataForSEOMaps(DataForSEOSerp):
    """Google Maps rank — the map results a local searcher actually scrolls.

    Every result is a Google Business Profile, so it carries a phone, a
    category, a rating, whether the listing is claimed and whether there is a
    website at all. Businesses with no site are kept by their place_id.
    """

    key = "dataforseo_maps"
    label = "DataForSEO Google Maps (map rank)"
    kind = "serp"
    blurb = ("Search Google Maps for '<service> <city>' and record each "
             "business's map position, rating, category and claimed status — "
             "including businesses with no website at all.")
    cost_note = "$0.002 per search of up to 100 results (Live)"
    caveat = ("Map rank depends heavily on where the searcher stands; Sellbird "
              "searches from the city centre at city zoom. Treat it as 'when we "
              "searched from downtown', not an absolute position.")

    SERP_COST = 0.002     # per SERP of up to 100 results
    rank_codes = ("MAPS_RANK",)
    # City-wide zoom; DataForSEO accepts 3z-21z and defaults to 17z (a few
    # streets), which would rank only whoever is closest to the pin.
    ZOOM = "12z"

    def estimate(self, queries):
        return sum(self.SERP_COST * max(1, -(-q.depth // 100)) for q in queries)

    async def fetch(self, client, env, query):
        keyword = query.keyword()
        items = await self._post(client, env, "serp/google/maps/live/advanced", {
            "keyword": keyword, "language_code": "en", "depth": query.depth,
            **_dfs_location(query, self.ZOOM)})
        out = []
        for item in items:
            # maps_paid_item is an ad; only organic map results have a rank.
            if item.get("type") != "maps_search":
                continue
            url = item.get("url") or ""
            domain = normalize_domain(item.get("domain") or url)
            external, alt_ids = _gbp_ids(item)
            if not (domain or external):
                continue
            rating = item.get("rating") or {}
            addr = item.get("address_info") or {}
            out.append(Business(
                name=item.get("title") or "", raw_title=item.get("title") or "",
                domain=domain, website=url, phone=item.get("phone") or "",
                address=item.get("address") or "",
                location=", ".join(x for x in (addr.get("city"), addr.get("region")) if x)
                         or query.location,
                category=item.get("category") or "",
                rating=rating.get("value"), review_count=rating.get("votes_count"),
                is_claimed=item.get("is_claimed"),
                has_website=bool(url or domain),
                maps_rank=item.get("rank_group"),
                external_id=external, alt_ids=alt_ids, source_url=url,
                provider=self.key, keyword=keyword,
            ))
        return out


def _gbp_ids(item: dict) -> tuple[str, tuple[str, ...]]:
    """(external_id, aliases) for a Google Business Profile result.

    Keyed on cid: it is the one id the organic local pack carries too, so a
    websiteless business seen in the pack and then on Maps is one company.
    place_id stays an alias, because companies were stored by it before.
    """
    place = item.get("place_id") or ""
    cid = str(item.get("cid") or "")
    if cid:
        return f"dataforseo:cid:{cid}", ((f"dataforseo:{place}",) if place else ())
    return (f"dataforseo:{place}" if place else ""), ()


def _best(current: int | None, new) -> int | None:
    """The better (lower) of two ranks, ignoring missing ones."""
    if not isinstance(new, int):
        return current
    return new if current is None else min(current, new)


class Serper(DiscoveryProvider):
    """Google search with the friendliest free tier and the best depth pricing.

    The one provider that did not go to 10x for depth 100 when Google removed
    100-results-per-page, and 2,500 free queries with no card.
    """

    key = "serper"
    label = "Serper"
    kind = "serp"
    rank_codes = ("SERP_RANK",)
    blurb = ("Fast Google search with rank positions. The easiest one to try: "
             "2,500 free queries, no credit card, one API key.")
    cost_note = "~$0.30-$1.00 per 1,000 searches depending on plan"
    free_tier = "2,500 queries free, no card required"
    signup_url = "https://serper.dev"
    env_keys = ("serper_api_key",)
    caveat = "Returns at most 10 results per query, including when a greater depth is requested."

    def estimate(self, queries):
        # One request per query, capped at ten results for free-account compatibility.
        return 0.001 * len(queries)

    async def fetch(self, client, env, query):
        r = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": env["serper_api_key"],
                     "Content-Type": "application/json"},
            # Free accounts reject num > 10 (HTTP 400 "Query pattern not
            # allowed for free accounts"), verified 2026-10-26.
            json={"q": query.keyword(), "num": max(1, min(query.depth, 10))},
        )
        r.raise_for_status()
        data = r.json()
        out = []
        for item in data.get("organic") or []:
            domain = normalize_domain(item.get("link") or "")
            if not domain:
                continue
            out.append(Business(
                name=business_name(item.get("title") or "", domain),
                raw_title=item.get("title") or "",
                domain=domain,
                website=item.get("link") or "",
                location=query.location,
                rank=item.get("position"),
                has_website=True,
                source_url=item.get("link") or "",
                provider=self.key,
            ))
        return out


class TinyFish(DiscoveryProvider):
    """TinyFish web search: free with a key, at 30 searches a minute and
    500 an hour. Results are TinyFish's own ordering for the phrase, from
    no particular place, so they find businesses but are not recorded as a
    Google rank (SERP_RANK means a Google position searched from a point).
    """
    key = "tinyfish"
    label = "TinyFish"
    kind = "serp"
    rank_codes = ()
    blurb = ("Free web search with a key. Finds businesses by phrase; it "
             "records no search rank, since its ordering isn't Google's.")
    cost_note = "free"
    free_tier = "free with a key, no card: 30 searches a minute, 500 an hour"
    signup_url = "https://agent.tinyfish.ai/api-keys"
    env_keys = ("tinyfish_api_key",)
    caveat = ("Needs TINYFISH_API_KEY in .env. Sellbird paces itself to 30 searches "
              "a minute; a depth-30 phrase is 3 searches, so about 160 phrases an "
              "hour fit in the 500. No SERP_RANK: use Serper or DataForSEO SERP "
              "for rank signals.")

    ENDPOINT = "https://api.search.tinyfish.ai"
    # 30 a minute, with a little room, and a little under 500 an hour.
    MIN_INTERVAL = 2.1
    HOURLY_LIMIT = 490

    def __init__(self):
        self._last_call = 0.0
        self._calls: list[float] = []      # this hour's, oldest first
        self._lock: tuple | None = None     # (event loop, its lock)

    async def _pace(self):
        """Wait for the next free slot, or stop before the hour's allowance
        is spent. Per process: a CLI run and a dashboard run at the same
        time each keep their own count."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock[0] is not loop:
            # One shared instance serves every run; a lock belongs to one loop.
            self._lock = (loop, asyncio.Lock())
        async with self._lock[1]:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 3600]
            if len(self._calls) >= self.HOURLY_LIMIT:
                minutes = int((3600 - (now - self._calls[0])) // 60) + 1
                raise ProviderExhausted(
                    f"TinyFish allows 500 searches an hour and this hour's are used up. "
                    f"Run again in about {minutes} minutes.")
            wait = self._last_call + self.MIN_INTERVAL - now
            if self._last_call and wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            self._calls.append(self._last_call)

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        api_key = env.get("tinyfish_api_key", "").strip()
        if not api_key:
            raise RuntimeError("TinyFish needs TINYFISH_API_KEY in .env")

        headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
        }

        # TinyFish returns up to 10 results per page, with page index 0..10.
        # depth 30 = 3 pages (pages 0, 1, 2). Max supported page is 10.
        pages_needed = max(1, min(11, -(-query.depth // 10)))
        out: list[Business] = []

        for page in range(pages_needed):

            params = {
                "query": query.keyword(),
                "page": page,
            }

            r = None
            for attempt in range(2):
                await self._pace()
                try:
                    r = await client.get(self.ENDPOINT, headers=headers, params=params)
                except httpx.HTTPError as e:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    raise RuntimeError(f"TinyFish search unreachable: {e}") from e

                if r.status_code == 429:
                    retry_after = 2.0
                    try:
                        retry_after = float(r.headers.get("Retry-After", "2"))
                    except (ValueError, TypeError):
                        pass
                    if attempt == 0:
                        await asyncio.sleep(min(60.0, max(1.0, retry_after)))
                        continue
                    raise ProviderExhausted(
                        "TinyFish: rate limited (it allows 30 searches a minute and 500 an "
                        "hour). Try again later, or with fewer phrases.")
                break

            if r is None:
                raise RuntimeError("TinyFish: Request failed.")

            if r.status_code == 401:
                raise ProviderExhausted(
                    "TinyFish: Invalid or missing API key (HTTP 401). "
                    "Check TINYFISH_API_KEY in .env or get a free key at https://agent.tinyfish.ai/api-keys")
            if r.status_code == 402:
                err_msg = ""
                try:
                    err_data = r.json()
                    err = err_data.get("error")
                    if isinstance(err, dict):
                        err_msg = err.get("message", "")
                    elif isinstance(err, str):
                        err_msg = err
                except Exception:
                    pass
                raise ProviderExhausted(
                    f"TinyFish refused the search (HTTP 402). {err_msg}".strip())
            r.raise_for_status()

            try:
                data = r.json()
            except ValueError as e:
                raise RuntimeError("TinyFish sent an unexpected answer (not JSON)") from e
            if not isinstance(data, dict):
                raise RuntimeError("TinyFish sent an unexpected answer (not an object)")
            items = data.get("results") or []
            if not isinstance(items, list) or not items:
                break

            keyword = query.keyword()
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or ""
                domain = normalize_domain(url) if isinstance(url, str) else ""
                if not domain:
                    continue
                title = item.get("title") or ""
                out.append(Business(
                    name=business_name(title, domain),
                    raw_title=title,
                    domain=domain,
                    website=url,
                    location=query.location,
                    rank=None,
                    has_website=True,
                    source_url=url,
                    provider=self.key,
                    keyword=keyword,
                ))

            # If this page returned fewer than 10 results, no more pages exist.
            if len(items) < 10:
                break

        return out


class OpenStreetMap(DiscoveryProvider):
    """Genuinely free, no account, no key. Honest about what that costs you.

    Good enough to try the whole pipeline end to end without signing up for
    anything, and a useful cross-reference layer afterwards. Not good enough
    to be your only source: OSM maps physical premises, so service-area
    businesses (roofers, HVAC, plumbers) are heavily under-represented.
    """

    key = "osm"
    label = "OpenStreetMap (Overpass)"
    kind = "listings"
    needs_coordinates = True
    blurb = ("Free public map data. No signup, no key, no card. Try the whole "
             "pipeline for nothing before you pay anyone.")
    cost_note = "free"
    free_tier = "unlimited, within community rate etiquette"
    signup_url = "https://www.openstreetmap.org"
    env_keys = ()
    caveat = ("Coverage is thin for any trade without a storefront — OSM maps "
              "premises, so it holds under 2,000 roofers for the entire US. "
              "Great for a free trial run or as a cross-reference; not a "
              "complete list.")

    # Several public Overpass instances run the same API. The main one
    # throttles readily, so try the mirrors before giving up — a free source
    # that fails half the time is not usable as a default.
    # Two entries, two conditions, both measured — see
    # scripts/diagnose_overpass.py, which re-checks them.
    #
    # Each must be its OWN instance: overpass.private.coffee sat here and
    # resolved to the same address as overpass.kumi.systems, so a 502 on the
    # first mirror bought two attempts at one machine and none at a second.
    #
    # Each must also hold the WHOLE planet. The community instances answer in
    # a fraction of a second and are independent, which makes them tempting:
    # overpass.osm.ch tallied ZERO cafes within a kilometre of downtown Denver
    # in 0.21s, so it serves a regional extract, and overpass.osm.jp is the
    # same kind of instance. A mirror like that answers "roofers in Denver"
    # with an empty list, and an empty list here is indistinguishable from a
    # market with nobody in it — the one failure this provider must never
    # produce. Vet a candidate with
    # `python scripts/diagnose_overpass.py --coverage <url>` before adding it;
    # independence at the DNS level is not enough.
    ENDPOINTS = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )
    # Overpass etiquette for a shipped tool: modest volume, no parallelism.
    MIN_INTERVAL = 2.0
    BACKOFF = 5.0
    # A free instance queues before it computes: node(1) — the smallest query
    # Overpass accepts — measured 38s of pure waiting on a busy afternoon. The
    # module-wide 45s read budget leaves nothing for a real query over a 40km
    # radius, so Overpass gets its own and asks the server for a matching
    # slice of execution time. Slow beats empty-handed, and a run that cannot
    # afford the wait stops on MAX_PROSPECTS_PER_CYCLE, not on a timeout.
    HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0,
                                 pool=10.0)
    SERVER_TIMEOUT = 90
    # Statuses that say "this mirror, right now" rather than "this query":
    # throttling, a gateway that lost its backend, an overloaded instance, a
    # timed-out one. Every one of them is another mirror's problem to solve, so
    # walk on. A 400 (bad query) or 404 is ours and fails here and now.
    RETRY_STATUSES = frozenset({406, 429, 500, 502, 503, 504})

    # A friendly term maps to the OSM tags that actually carry those businesses.
    TAG_MAP = {
        "roof": ['craft=roofer', 'shop=roofing'],
        "plumb": ['craft=plumber'],
        "hvac": ['craft=hvac'],
        "electric": ['craft=electrician'],
        "dentist": ['amenity=dentist'],
        "dental": ['amenity=dentist'],
        "doctor": ['amenity=doctors', 'amenity=clinic'],
        "medical": ['amenity=clinic', 'amenity=doctors'],
        "chiro": ['healthcare=chiropractor', 'amenity=doctors'],
        "vet": ['amenity=veterinary'],
        "lawyer": ['office=lawyer'],
        "attorney": ['office=lawyer'],
        "contractor": ['craft=builder', 'office=construction_company'],
        "landscap": ['craft=gardener', 'shop=garden_centre'],
        "auto repair": ['shop=car_repair'],
        "mechanic": ['shop=car_repair'],
        "salon": ['shop=hairdresser', 'shop=beauty'],
        "barber": ['shop=hairdresser'],
        "spa": ['shop=beauty', 'leisure=spa'],
        "gym": ['leisure=fitness_centre'],
        "fitness": ['leisure=fitness_centre'],
        "real estate": ['office=estate_agent'],
        "realtor": ['office=estate_agent'],
        "insurance": ['office=insurance'],
        "accountant": ['office=accountant'],
        "cpa": ['office=accountant'],
        "restaurant": ['amenity=restaurant'],
        "cafe": ['amenity=cafe'],
        "cleaning": ['shop=dry_cleaning', 'craft=cleaning'],
        "solar": ['craft=photovoltaic'],
        "locksmith": ['craft=locksmith'],
        "pest": ['craft=pest_control'],
        # Spanish terms
        "ferreter": ['shop=hardware', 'shop=doityourself'],
        "supermercado": ['shop=supermarket'],
        "abasto": ['shop=convenience', 'shop=supermarket'],
        "distribuidora": ['shop=wholesale'],
        "al mayor": ['shop=wholesale'],
        "por mayor": ['shop=wholesale'],
        "mayorista": ['shop=wholesale'],
        "contad": ['office=accountant'],
        "contable": ['office=accountant'],
        "farmacia": ['amenity=pharmacy'],
        "repuesto": ['shop=car_parts'],
        "electrodom": ['shop=electronics', 'shop=appliance'],
        "tienda de ropa": ['shop=clothes'],
        "panader": ['shop=bakery'],
        "clinica": ['amenity=clinic'],
        "clínica": ['amenity=clinic'],
    }

    def _tags(self, term: str) -> list[str]:
        """The OSM tags that actually carry this kind of business, or none.

        There is deliberately no catch-all fallback. An earlier version fell
        back to `office=company`, which answers "roofers in Denver" with every
        registered office in Denver — coffee roasters, churches, photography
        studios. A search this source cannot answer has to say so; filling the
        database with plausible-looking junk is far worse than returning
        nothing.
        """
        low = term.lower()
        for needle, tags in self.TAG_MAP.items():
            # Word-start boundary prevents short stems like "spa" matching inside "despacho"
            if re.search(rf"\b{re.escape(needle)}", low):
                return tags
        return []

    async def fetch(self, client, env, query):
        if not query.coordinate:
            logger.warning("osm: no coordinate for %r — Overpass needs a "
                           "lat,lng,radius to search", query.keyword())
            return []
        tags = self._tags(query.term)
        if not tags:
            raise RuntimeError(
                f"OpenStreetMap has no category for {query.term!r}. It can only "
                f"search mapped trades ({', '.join(sorted(self.TAG_MAP))}). Use a "
                f"listings or SERP provider for this one.")
        lat_s, lng_s, radius_km_s = (query.coordinate.split(",") + ["", "", ""])[:3]
        try:
            lat_f = float(lat_s)
            lng_f = float(lng_s)
            radius_f = float(radius_km_s or 25)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid coordinate for OpenStreetMap: {query.coordinate!r}") from exc
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0 and 0.1 <= radius_f <= 500.0):
            raise ValueError(f"Out-of-range coordinate for OpenStreetMap: {query.coordinate!r}")
        radius_m = int(radius_f * 1000)
        clauses = "".join(
            f'node[{tag.split("=")[0]}={tag.split("=")[1]}](around:{radius_m},{lat_f:.6f},{lng_f:.6f});'
            f'way[{tag.split("=")[0]}={tag.split("=")[1]}](around:{radius_m},{lat_f:.6f},{lng_f:.6f});'
            for tag in tags
        )
        body = (f"[out:json][timeout:{self.SERVER_TIMEOUT}];({clauses});"
                f"out center {query.limit};")

        # Overpass is a free volunteer service and throttles, 502s and times
        # out readily under load. Walk the mirrors, then RAISE: returning []
        # would make a throttled run look exactly like a market with no
        # businesses in it.
        headers = {
            "User-Agent": "VeloReach/0.2 (open-source outreach agent)",
            "Accept": "application/json, */*",
        }
        r = None
        for i, endpoint in enumerate(self.ENDPOINTS):
            if i:
                await asyncio.sleep(self.BACKOFF)
            try:
                r = await client.post(endpoint, content=body.encode(),
                                      headers=headers,
                                      timeout=self.HTTP_TIMEOUT)
            except httpx.HTTPError as e:
                logger.warning("osm: %s unreachable (%s)", endpoint, e)
                continue
            if r.status_code not in self.RETRY_STATUSES:
                break
            logger.warning("osm: %s is throttling or down (%s) — trying the "
                           "next mirror", endpoint, r.status_code)
        else:
            raise RuntimeError(
                "Every public Overpass mirror is unavailable or throttling "
                "right now. This is free volunteer infrastructure — wait a few "
                "minutes and re-run, or use a paid provider for bulk work.")
        if r is None:
            raise RuntimeError("Could not reach any Overpass mirror.")
        r.raise_for_status()

        out = []
        for el in r.json().get("elements") or []:
            tags = el.get("tags") or {}
            name = tags.get("name") or ""
            if not name:
                continue
            site = tags.get("website") or tags.get("contact:website") or ""
            city = ", ".join(x for x in (tags.get("addr:city"),
                                         tags.get("addr:state")) if x)
            out.append(Business(
                name=name,
                domain=normalize_domain(site),
                website=site,
                phone=tags.get("phone") or tags.get("contact:phone") or "",
                address=" ".join(x for x in (tags.get("addr:housenumber"),
                                             tags.get("addr:street")) if x),
                location=city or query.location,
                category=tags.get("craft") or tags.get("shop") or tags.get("office") or "",
                has_website=bool(site),
                external_id=f"osm:{el.get('type')}/{el.get('id')}",
                source_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
                provider=self.key,
            ))
        return out


class ScavioProvider(DiscoveryProvider):
    """Shared plumbing for the two Scavio adapters: one key, one credit price.

    Scavio bills 1 credit per page. A page that fails after earlier pages
    succeeded ends the query with what it already has, the same rule the run
    loop applies between queries. A failure on the first page raises:
    returning [] would make an expired key look like an empty market.
    """

    signup_url = "https://scavio.dev"
    env_keys = ("scavio_api_key",)
    free_tier = "50 credits on signup, no card"

    API = "https://api.scavio.dev/api/v2/google"
    # Conservative pay-as-you-go price; subscription credits can cost less.
    CREDIT_PRICE = 0.01
    MIN_INTERVAL = 1.05  # free accounts allow one request per second

    def __init__(self):
        self._last_call = 0.0

    async def _post(self, client, env, path: str, body: dict) -> dict:
        if self.last_cost + self.CREDIT_PRICE > self.remaining_budget + 1e-12:
            raise ProviderExhausted("Scavio: budget reached before the next page")
        wait = self._last_call + self.MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()
        try:
            r = await client.post(
                f"{self.API}{path}",
                headers={"Authorization": f"Bearer {env['scavio_api_key']}",
                         "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError:
            raise RuntimeError("Scavio: request failed; try again later") from None
        # Never put response bodies in logs: upstream errors may echo secrets.
        if r.status_code in (401, 402, 403, 429):
            reasons = {401: "invalid API key", 402: "insufficient credits",
                       403: "access refused", 429: "rate limit reached"}
            raise ProviderExhausted(f"Scavio {r.status_code}: {reasons[r.status_code]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Scavio {r.status_code}: request failed")
        # A successful response is billable even if its body is malformed.
        self.last_cost += self.CREDIT_PRICE
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("Scavio: invalid JSON response") from None
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError("Scavio: invalid response")
        credits = data.get("credits_used", 1)
        if (isinstance(credits, (int, float)) and not isinstance(credits, bool)
                and math.isfinite(credits) and credits >= 0):
            self.last_cost += (credits - 1) * self.CREDIT_PRICE
        return data

    async def _pages(self, client, env, query, path: str, body: dict,
                     results_key: str, page_size: int, pages: int
                     ) -> list[tuple[int, dict]]:
        """Every result as (offset of its page, item), page by page."""
        self.last_cost = 0.0
        self.last_error = self.stop_reason = ""
        self.complete = True
        rows: list[tuple[int, dict]] = []
        for page in range(pages):
            start = page * page_size
            try:
                data = await self._post(client, env, path, dict(body, start=start))
                items = data.get(results_key)
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise RuntimeError("Scavio: invalid results list")
                for item in items:
                    for field in ("title", "website", "link", "phone", "address", "type", "place_id"):
                        if item.get(field) is not None and not isinstance(item[field], str):
                            raise RuntimeError("Scavio: invalid result field")
                    for field in ("rating", "reviews", "position"):
                        value = item.get(field)
                        if value is not None and (isinstance(value, bool)
                                or not isinstance(value, (int, float))
                                or not math.isfinite(value) or value < 0):
                            raise RuntimeError("Scavio: invalid numeric result field")
                last_page = self._last_page(data, items, page_size)
            except Exception as e:
                self.complete = False
                self.last_error = str(e)
                if isinstance(e, ProviderExhausted):
                    self.stop_reason = str(e)
                if not rows:
                    raise
                logger.warning("%s: page %d of %r failed, keeping %d results: %s",
                               self.key, page + 1, query.keyword(), len(rows), e)
                break
            rows.extend((start, item) for item in items)
            if last_page:
                break
        return rows

    def _last_page(self, data: dict, items: list, page_size: int) -> bool:
        return len(items) < page_size


class ScavioMaps(ScavioProvider):
    """Google Maps listings, the source issue #28 asked for.

    Every place Google Maps shows for a search around a point, with phone,
    website, category, rating and review count. Businesses with no website
    are kept, like the other listings providers.
    """

    key = "scavio_maps"
    needs_coordinates = True
    label = "Scavio Google Maps"
    kind = "listings"
    blurb = ("Google Maps results around each city: phone, website, category, "
             "rating and review count for every place. Up to 120 businesses "
             "per search.")
    cost_note = "1 credit per page (up to 20 listings); estimates use $0.01/credit"
    caveat = ("Google Maps does not say whether a listing is claimed, so the "
              "unclaimed-listing signal stays empty. Same key as Scavio Google "
              "Search.")

    PAGE_SIZE = 20
    MAX_PAGES = 6        # the API accepts start up to 100

    def _page_count(self, query: DiscoveryQuery) -> int:
        return max(1, min(self.MAX_PAGES, -(-query.limit // self.PAGE_SIZE)))

    def estimate(self, queries):
        return sum(self.CREDIT_PRICE * self._page_count(q) for q in queries)

    @staticmethod
    def map_center(coordinate: str) -> str:
        """"39.7392,-104.9903,40" -> "@39.7392,-104.9903,10z".

        Google Maps takes a center and a zoom, not a radius. A zoom-z view
        spans roughly 40,000 km / 2^z, so a 40 km radius is zoom 10 (one
        metro area) and 5 km is zoom 13.
        """
        parts = [p.strip() for p in coordinate.split(",")]
        if len(parts) < 2 or not all(parts[:2]):
            return ""
        try:
            lat, lng = float(parts[0]), float(parts[1])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return ""
        except ValueError:
            return ""
        try:
            radius_km = float(parts[2]) if len(parts) > 2 and parts[2] else 40.0
        except ValueError:
            radius_km = 40.0
        if not math.isfinite(radius_km) or radius_km <= 0:
            radius_km = 40.0
        zoom = round(math.log2(40000 / max(radius_km, 0.5)))
        return f"@{parts[0]},{parts[1]},{max(3, min(zoom, 16))}z"

    async def fetch(self, client, env, query):
        # The map center picks the area, as it does in the Maps app. Without
        # one the API falls back to a default city for the country, so the
        # place name has to ride in the query instead.
        center = self.map_center(query.coordinate) if query.coordinate else ""
        body = {"query": query.term, "ll": center} if center else {"query": query.keyword()}

        out = []
        for _, item in await self._pages(
                client, env, query, "/maps/search", body, "local_results",
                self.PAGE_SIZE, self._page_count(query)):
            name = item.get("title") or ""
            if not name:
                continue
            site = item.get("website") or ""
            place_id = item.get("place_id") or ""
            out.append(Business(
                name=name,
                domain=normalize_domain(site),
                website=site,
                phone=item.get("phone") or "",
                address=item.get("address") or "",
                location=query.location,
                category=item.get("type") or "",
                rating=item.get("rating"),
                review_count=item.get("reviews"),
                has_website=bool(site),
                # A Maps list position is not an organic rank, so rank stays
                # None and no SERP_RANK is recorded for it.
                external_id=f"gmaps:{place_id}" if place_id else "",
                source_url=(f"https://www.google.com/maps/place/?q=place_id:{place_id}"
                            if place_id else site),
                provider=self.key,
            ))
        return out


class ScavioSearch(ScavioProvider):
    """Google organic results with rank, from the same key as Scavio Maps."""

    key = "scavio_search"
    rank_codes = ("SERP_RANK",)
    label = "Scavio Google Search"
    kind = "serp"
    blurb = ("Google organic results with rank positions. One Scavio key "
             "covers this and Google Maps listings.")
    cost_note = "1 credit per page (up to 10 results); depth 30 estimated at $0.03"
    caveat = ("Results come from google.com in English (US) unless the search "
              "names another place. Depth 20-30 is the sweet spot.")

    PAGE_SIZE = 10

    def estimate(self, queries):
        return sum(self.CREDIT_PRICE * max(1, -(-q.depth // self.PAGE_SIZE))
                   for q in queries)

    def _last_page(self, data, items, page_size):
        # Google often shows fewer than 10 organic results on a page (a local
        # pack or videos take the space), so a short page is not the end.
        # The end is when Google offers no next page.
        pagination = data.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise RuntimeError("Scavio: invalid pagination")
        return not items or not pagination.get("next")

    async def fetch(self, client, env, query):
        out = []
        for start, item in await self._pages(
                client, env, query, "", {"query": query.keyword()},
                "organic_results", self.PAGE_SIZE,
                max(1, -(-query.depth // self.PAGE_SIZE))):
            domain = normalize_domain(item.get("link") or "")
            if not domain:
                continue
            position = item.get("position")
            out.append(Business(
                name=business_name(item.get("title") or "", domain),
                raw_title=item.get("title") or "",
                domain=domain,
                website=item.get("link") or "",
                location=query.location,
                # `position` restarts at 1 on every page, so page 2's first
                # result is rank 11, not rank 1.
                rank=start + position if isinstance(position, int) and position > 0 else None,
                has_website=True,
                source_url=item.get("link") or "",
                provider=self.key,
            ))
        return out


class DuckDuckGoSearch(DiscoveryProvider):
    """Keyless, zero-cost web search via DuckDuckGo (ddgs).

    Works out of the box for B2B SaaS, agencies, startups, and global companies
    without any API key, credit card, or signup.
    """

    key = "ddgs"
    label = "DuckDuckGo Search (ddgs)"
    kind = "serp"
    rank_codes = ()
    blurb = ("Free keyless web search for B2B SaaS, agencies, startups and "
             "global companies — zero API keys or setup required.")
    cost_note = "Free (keyless DuckDuckGo search via ddgs)"
    free_tier = "Unlimited free searches with polite pacing; no account or API key"
    signup_url = "https://pypi.org/project/ddgs/"
    env_keys = ()
    caveat = ("Uses DuckDuckGo organic web search rather than Google, so "
              "SERP_RANK is not recorded; ideal for zero-cost B2B discovery.")

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    @staticmethod
    def _run_ddgs(keyword: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = ddgs.text(keyword, max_results=max_results)
            return list(results or [])

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        keyword = query.keyword()
        max_results = max(1, min(query.depth, 50))
        items = await asyncio.to_thread(self._run_ddgs, keyword, max_results)
        out: list[Business] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
            domain = normalize_domain(url)
            if not domain:
                continue
            title = str(item.get("title") or "").strip()
            out.append(Business(
                name=business_name(title, domain),
                raw_title=title,
                domain=domain,
                website=url,
                location=query.location,
                rank=None,
                has_website=True,
                source_url=url,
                provider=self.key,
                keyword=keyword,
            ))
        return out


class SearXNGSearch(DiscoveryProvider):
    """Self-hosted SearXNG metasearch instance."""

    key = "searxng"
    label = "SearXNG (Self-Hosted)"
    kind = "serp"
    rank_codes = ()
    blurb = ("Query your own self-hosted SearXNG metasearch instance across "
             "Google, Bing, Brave and DuckDuckGo with zero per-query fees.")
    cost_note = "Free (self-hosted SearXNG instance)"
    free_tier = "Unlimited on your own SearXNG server"
    signup_url = "https://docs.searxng.org"
    env_keys = ("searxng_url",)
    caveat = "Requires SEARXNG_URL pointing to an instance with JSON format enabled."

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        base = str(env.get("searxng_url") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("SearXNG needs SEARXNG_URL in .env")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base

        keyword = query.keyword()
        r = await client.get(
            f"{base}/search",
            params={"q": keyword, "format": "json"},
            headers={"Accept": "application/json"},
        )
        if r.status_code in (401, 403, 429):
            raise ProviderExhausted(f"SearXNG {r.status_code}: request refused or rate limited")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("SearXNG returned non-object JSON")

        out: list[Business] = []
        for item in (data.get("results") or [])[:max(1, min(query.depth, 50))]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            domain = normalize_domain(url)
            if not domain:
                continue
            title = str(item.get("title") or "").strip()
            out.append(Business(
                name=business_name(title, domain),
                raw_title=title,
                domain=domain,
                website=url,
                location=query.location,
                rank=None,
                has_website=True,
                source_url=url,
                provider=self.key,
                keyword=keyword,
            ))
        return out


class TavilySearch(DiscoveryProvider):
    """Tavily AI-native web search API."""

    key = "tavily"
    label = "Tavily AI Search"
    kind = "serp"
    rank_codes = ()
    blurb = ("AI-optimized web search built for agents, returning high-signal "
             "B2B company pages and clean snippets.")
    cost_note = "$0.008 per search after free tier (1,000 free credits/month)"
    free_tier = "1,000 searches/month free, no credit card required"
    signup_url = "https://tavily.com"
    env_keys = ("tavily_api_key",)
    caveat = "Returns up to 20 relevance-ranked results per query; does not measure Google SERP_RANK."

    COST_PER_QUERY = 0.008

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return self.COST_PER_QUERY * len(queries)

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        api_key = str(env.get("tavily_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("Tavily needs TAVILY_API_KEY in .env")

        keyword = query.keyword()
        r = await client.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "api_key": api_key,
                "query": keyword,
                "max_results": max(1, min(query.depth, 20)),
                "search_depth": "basic",
            },
        )
        if r.status_code in (401, 402, 403, 429):
            raise ProviderExhausted(f"Tavily {r.status_code}: invalid key, quota exhausted, or rate limited")
        r.raise_for_status()
        self.last_cost = self.COST_PER_QUERY
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Tavily returned non-object JSON")

        out: list[Business] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            domain = normalize_domain(url)
            if not domain:
                continue
            title = str(item.get("title") or "").strip()
            out.append(Business(
                name=business_name(title, domain),
                raw_title=title,
                domain=domain,
                website=url,
                location=query.location,
                rank=None,
                has_website=True,
                source_url=url,
                provider=self.key,
                keyword=keyword,
            ))
        return out


class FirecrawlSearch(DiscoveryProvider):
    """Firecrawl v2 cloud web search endpoint."""

    key = "firecrawl_search"
    label = "Firecrawl Search"
    kind = "serp"
    rank_codes = ()
    blurb = ("Cloud web search via Firecrawl v2 that discovers businesses and "
             "pairs seamlessly with Firecrawl page scraping.")
    cost_note = "~$0.002 per search credit"
    free_tier = "500 free credits on signup"
    signup_url = "https://www.firecrawl.dev"
    env_keys = ("firecrawl_api_key",)
    caveat = "Uses Firecrawl v2 search endpoint; does not measure Google SERP_RANK."

    COST_PER_QUERY = 0.002

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return self.COST_PER_QUERY * len(queries)

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        api_key = str(env.get("firecrawl_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("Firecrawl Search needs FIRECRAWL_API_KEY in .env")
        base_url = str(
            env.get("firecrawl_api_url")
            or os.getenv("FIRECRAWL_API_URL")
            or "https://api.firecrawl.dev"
        ).strip().rstrip("/")

        keyword = query.keyword()
        r = await client.post(
            f"{base_url}/v2/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": keyword,
                "limit": max(1, min(query.depth, 20)),
            },
        )
        if r.status_code in (401, 402, 403, 429):
            raise ProviderExhausted(f"Firecrawl Search {r.status_code}: invalid key, credits exhausted, or rate limited")
        r.raise_for_status()
        self.last_cost = self.COST_PER_QUERY
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("Firecrawl Search returned non-object JSON")

        raw_data = data.get("data")
        if isinstance(raw_data, dict):
            items = raw_data.get("web") or raw_data.get("results") or []
        elif isinstance(raw_data, list):
            items = raw_data
        else:
            items = data.get("results") or []

        out: list[Business] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            domain = normalize_domain(url)
            if not domain:
                continue
            title = str(item.get("title") or "").strip()
            out.append(Business(
                name=business_name(title, domain),
                raw_title=title,
                domain=domain,
                website=url,
                location=query.location,
                rank=None,
                has_website=True,
                source_url=url,
                provider=self.key,
                keyword=keyword,
            ))
        return out


class OsmPlacesProvider(DiscoveryProvider):
    """Keyless live local-business search via OpenStreetMap Photon + Nominatim.

    Unlike Overpass API (which frequently throttles with 406/429), Photon
    (komoot.io) and Nominatim accept free public text searches reliably
    with zero API keys or lat/lng config.
    """

    key = "osm_places"
    label = "OSM Live Places (Photon + Nominatim)"
    kind = "listings"
    rank_codes = ()
    blurb = ("Free keyless local business lookup using Photon (Komoot) and "
             "OpenStreetMap Nominatim with automatic failover when Overpass "
             "throttles.")
    cost_note = "Free (OSM Photon + Nominatim public APIs)"
    free_tier = "Unlimited free public searches with polite pacing; no API key"
    signup_url = "https://photon.komoot.io"
    env_keys = ()
    caveat = ("Covers named OpenStreetMap businesses without requiring Overpass "
              "mirrors; some listings may lack websites until enrichment runs.")

    PHOTON_URL = "https://photon.komoot.io/api/"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    HEADERS = {"User-Agent": "VeloReach/1.0 (+https://github.com/alietariq4-ctrl/sellbird)"}

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    @staticmethod
    def _clean_tags(props: dict) -> tuple[str, str, str, str]:
        extra = props.get("extratags") if isinstance(props.get("extratags"), dict) else {}
        site = str(
            props.get("website")
            or extra.get("website")
            or extra.get("contact:website")
            or extra.get("url")
            or ""
        ).strip()
        phone = str(
            props.get("phone")
            or extra.get("phone")
            or extra.get("contact:phone")
            or ""
        ).strip()
        category = str(
            props.get("osm_value")
            or props.get("type")
            or props.get("category")
            or extra.get("amenity")
            or extra.get("shop")
            or ""
        ).strip()
        city = str(props.get("city") or props.get("town") or props.get("county") or "").strip()
        state = str(props.get("state") or "").strip()
        loc = ", ".join(x for x in (city, state) if x)
        return site, phone, category, loc

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        q = query.keyword() or query.term or query.location
        limit = max(1, min(query.limit or 20, 50))
        out: list[Business] = []
        by_key: dict[str, Business] = {}

        def _append_biz(b: Business) -> None:
            if not b.name:
                return
            keys = [k for k in (b.domain, b.external_id) if k] or [b.name.lower()]
            existing = next((by_key[k] for k in keys if k in by_key), None)
            if existing is not None:
                if b.domain and not existing.domain:
                    existing.domain = b.domain
                if b.website and not existing.website:
                    existing.website = b.website
                    existing.has_website = True
                if b.phone and not existing.phone:
                    existing.phone = b.phone
                if b.address and not existing.address:
                    existing.address = b.address
                if b.location and not existing.location:
                    existing.location = b.location
                if b.category and not existing.category:
                    existing.category = b.category
                if b.external_id and not existing.external_id:
                    existing.external_id = b.external_id
                for k in (existing.domain, existing.external_id):
                    if k:
                        by_key[k] = existing
                return
            out.append(b)
            for k in keys:
                by_key[k] = b

        # 1. Primary fast geo-search: Photon (Komoot)
        try:
            r = await client.get(
                self.PHOTON_URL,
                params={"q": q, "limit": limit},
                headers=self.HEADERS,
            )
            if r.status_code == 200:
                data = r.json() if r.content else {}
                for feat in (data.get("features") or []):
                    if not isinstance(feat, dict):
                        continue
                    props = feat.get("properties") or {}
                    name = str(props.get("name") or "").strip()
                    if not name:
                        continue
                    site, phone, category, loc = self._clean_tags(props)
                    osm_type = str(props.get("osm_type") or "N").lower()
                    osm_type_full = {"n": "node", "w": "way", "r": "relation"}.get(osm_type, "node")
                    osm_id = props.get("osm_id") or ""
                    street = " ".join(
                        str(x) for x in (props.get("housenumber"), props.get("street")) if x
                    )
                    ext_id = f"osm:{osm_type_full}/{osm_id}" if osm_id else ""
                    src_url = f"https://www.openstreetmap.org/{osm_type_full}/{osm_id}" if osm_id else ""
                    _append_biz(Business(
                        name=name,
                        domain=normalize_domain(site),
                        website=site,
                        phone=phone,
                        address=street,
                        location=loc or query.location,
                        category=category,
                        has_website=bool(site),
                        external_id=ext_id,
                        source_url=src_url,
                        provider=self.key,
                        keyword=q,
                    ))
        except Exception as exc:
            logger.debug("osm_places photon failed for %r: %s", q, exc)

        # 2. Fallback / supplement: Nominatim structured extratags (carries websites & phones)
        if len(out) < limit:
            try:
                r = await client.get(
                    self.NOMINATIM_URL,
                    params={
                        "q": q,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "extratags": 1,
                        "limit": limit,
                    },
                    headers=self.HEADERS,
                )
                if r.status_code == 200:
                    items = r.json() if r.content else []
                    if isinstance(items, list):
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            addr = item.get("address") if isinstance(item.get("address"), dict) else {}
                            name = str(
                                item.get("name")
                                or (item.get("display_name") or "").split(",")[0]
                            ).strip()
                            if not name:
                                continue
                            site, phone, category, _ = self._clean_tags(item)
                            city = str(
                                addr.get("city") or addr.get("town") or addr.get("village") or ""
                            ).strip()
                            state = str(addr.get("state") or "").strip()
                            loc = ", ".join(x for x in (city, state) if x)
                            street = " ".join(
                                str(x) for x in (addr.get("house_number"), addr.get("road")) if x
                            )
                            osm_type = str(item.get("osm_type") or "node")
                            osm_id = item.get("osm_id") or ""
                            ext_id = f"osm:{osm_type}/{osm_id}" if osm_id else ""
                            src_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_id else ""
                            _append_biz(Business(
                                name=name,
                                domain=normalize_domain(site),
                                website=site,
                                phone=phone,
                                address=street,
                                location=loc or query.location,
                                category=category,
                                has_website=bool(site),
                                external_id=ext_id,
                                source_url=src_url,
                                provider=self.key,
                                keyword=q,
                            ))
            except Exception as exc:
                logger.debug("osm_places nominatim failed for %r: %s", q, exc)

        return out[:limit]


class YcDirectoryProvider(DiscoveryProvider):
    """Keyless B2B startup & SaaS directory from yc-oss open JSON feeds."""

    key = "yc_oss"
    label = "YC Startups Directory (yc-oss)"
    kind = "serp"
    rank_codes = ()
    blurb = ("Free keyless B2B database of Y Combinator startups and SaaS "
             "companies via yc-oss open JSON — ideal for SaaS, AI, fintech, "
             "and software outreach.")
    cost_note = "Free (yc-oss open public JSON)"
    free_tier = "Unlimited free lookups; no account or API key required"
    signup_url = "https://github.com/yc-oss/api"
    env_keys = ()
    caveat = ("Covers YC-backed technology/SaaS companies rather than local "
              "offline storefronts like plumbers or clinics.")

    TOP_URL = "https://yc-oss.github.io/api/companies/top.json"

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        r = await client.get(self.TOP_URL)
        r.raise_for_status()
        items = r.json() if r.content else []
        if not isinstance(items, list):
            return []

        tokens = [t.lower() for t in re.split(r"[\s,;/]+", query.term or "") if len(t) > 1]
        loc_tokens = [t.lower() for t in re.split(r"[\s,;/]+", query.location or "") if len(t) > 2]
        limit = max(1, min(query.limit or 20, 50))
        scored: list[tuple[int, dict]] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            website = str(item.get("website") or "").strip()
            domain = normalize_domain(website)
            if not domain:
                continue
            hay = " ".join([
                str(item.get("name") or ""),
                str(item.get("one_liner") or ""),
                str(item.get("long_description") or ""),
                str(item.get("industry") or ""),
                str(item.get("subindustry") or ""),
                " ".join(str(x) for x in (item.get("tags") or [])),
            ]).lower()
            term_hits = sum(1 for tok in tokens if tok in hay) if tokens else 1
            if tokens and term_hits == 0:
                continue
            loc_hay = str(item.get("all_locations") or "").lower()
            loc_hits = sum(1 for lt in loc_tokens if lt in loc_hay)
            scored.append((term_hits * 10 + loc_hits, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[Business] = []
        for _, item in scored[:limit]:
            website = str(item.get("website") or "").strip()
            domain = normalize_domain(website)
            name = str(item.get("name") or domain).strip()
            loc = str(item.get("all_locations") or query.location or "").split(";")[0].strip()
            cat = str(item.get("subindustry") or item.get("industry") or "Startup").strip()
            out.append(Business(
                name=name,
                raw_title=str(item.get("one_liner") or name),
                domain=domain,
                website=website,
                location=loc,
                category=cat,
                has_website=True,
                external_id=f"yc:{item.get('slug') or item.get('id') or domain}",
                source_url=str(item.get("url") or website),
                provider=self.key,
                keyword=query.keyword(),
            ))
        return out


class FreeMultiSourceProvider(DiscoveryProvider):
    """Combines Overpass maps, OSM Places, and DuckDuckGo web search for $0."""

    key = "free_auto"
    label = "Smart Free Discovery (Maps + Web)"
    kind = "serp"
    needs_coordinates = True
    rank_codes = ()
    blurb = ("Combines OpenStreetMap Overpass, OSM Live Places, and DuckDuckGo web search "
             "at $0 so local trades, contractors, and B2B companies come back with live websites.")
    cost_note = "free ($0 keyless multi-source)"
    free_tier = "unlimited keyless searches across Overpass, Photon/Nominatim & DuckDuckGo"
    signup_url = "https://www.openstreetmap.org"
    env_keys = ()
    caveat = ("Uses free public Overpass, Nominatim, and DuckDuckGo endpoints — volume may "
              "pace or throttle under rapid bursts.")

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        limit = max(1, min(query.limit or 50, 100))
        merged: list[Business] = []
        by_key: dict[str, Business] = {}

        def _merge(items: list[Business]) -> None:
            for b in items:
                if not b.name and not b.domain:
                    continue
                if is_junk(b.domain, b.raw_title or b.name):
                    continue
                keys = [k for k in (b.domain, b.external_id) if k] or [b.name.lower()]
                existing = next((by_key[k] for k in keys if k in by_key), None)
                if existing is not None:
                    if b.domain and not existing.domain:
                        existing.domain = b.domain
                    if b.website and not existing.website:
                        existing.website = b.website
                        existing.has_website = True
                    if b.phone and not existing.phone:
                        existing.phone = b.phone
                    if b.address and not existing.address:
                        existing.address = b.address
                    if b.location and not existing.location:
                        existing.location = b.location
                    if b.category and not existing.category:
                        existing.category = b.category
                    for k in (existing.domain, existing.external_id):
                        if k:
                            by_key[k] = existing
                    continue
                merged.append(b)
                for k in keys:
                    by_key[k] = b

        # Presets (yc, accelerator, funding, hiring) use web search (+ yc-oss when preset=='yc')
        if query.preset:
            if query.preset == "yc":
                try:
                    _merge(await YcDirectoryProvider().fetch(client, env, query))
                except Exception as exc:
                    logger.debug("free_auto yc_oss failed: %s", exc)
            try:
                _merge(await DuckDuckGoSearch().fetch(client, env, query))
            except Exception as exc:
                logger.debug("free_auto ddgs preset failed: %s", exc)
            for b in merged:
                b.provider = self.key
            return merged[:limit]

        async def _run_osm():
            osm = OpenStreetMap()
            if query.coordinate and osm._tags(query.term):
                return await asyncio.wait_for(osm.fetch(client, env, query), timeout=38.0)
            return []

        async def _run_places():
            return await asyncio.wait_for(OsmPlacesProvider().fetch(client, env, query), timeout=15.0)

        async def _run_ddg(q: DiscoveryQuery):
            return await asyncio.wait_for(DuckDuckGoSearch().fetch(client, env, q), timeout=15.0)

        r_osm, r_places, r_ddg = await asyncio.gather(
            _run_osm(),
            _run_places(),
            _run_ddg(query),
            return_exceptions=True,
        )
        for label, res in (("osm", r_osm), ("osm_places", r_places), ("ddgs", r_ddg)):
            if isinstance(res, Exception):
                logger.debug("free_auto %s failed for %r: %s", label, query.keyword(), res)
            elif res:
                _merge(res)

        with_domains = sum(1 for b in merged if b.domain)
        if with_domains < min(limit, 20) and query.term and query.location:
            alt_queries = [
                replace(query, term=f"{query.term} services", depth=min(query.depth or 30, 25)),
                replace(query, term=f"{query.term} company -site:yelp.com -site:angi.com -site:bbb.org", depth=min(query.depth or 30, 25)),
            ]
            alt_results = await asyncio.gather(
                *(_run_ddg(alt) for alt in alt_queries),
                return_exceptions=True,
            )
            for alt, res in zip(alt_queries, alt_results):
                if isinstance(res, Exception):
                    logger.debug("free_auto ddgs secondary failed for %r: %s", alt.keyword(), res)
                elif res:
                    _merge(res)

        # Put domain-bearing businesses ahead of domainless map pins
        merged.sort(key=lambda b: (0 if b.domain else 1))
        for b in merged:
            b.provider = self.key
        return merged[:limit]


PROVIDERS: dict[str, DiscoveryProvider] = {
    p.key: p for p in (
        FreeMultiSourceProvider(),
        OpenStreetMap(),
        OsmPlacesProvider(),
        YcDirectoryProvider(),
        DuckDuckGoSearch(),
        SearXNGSearch(),
        TinyFish(),
        DataForSEOListings(),
        DataForSEOSerp(),
        DataForSEOMaps(),
        Serper(),
        TavilySearch(),
        FirecrawlSearch(),
        ScavioMaps(),
        ScavioSearch(),
    )
}

DEFAULT_PROVIDER = "free_auto"


NO_SEARCH_RESULTS = 40102


def _dfs_items(payload: dict) -> list[dict]:
    """Pull result items out of a DataForSEO envelope, surfacing errors.

    A bare status code costs three wrong guesses; the message in the body
    usually makes the problem obvious immediately.
    """
    if payload.get("status_code") not in (20000, None):
        raise RuntimeError(
            f"DataForSEO {payload.get('status_code')}: "
            f"{payload.get('status_message')}")
    tasks = payload.get("tasks") or []
    if not tasks:
        return []
    task = tasks[0]
    if task.get("status_code") == NO_SEARCH_RESULTS:
        # A real answer — nobody ranks for this search here — not a failure.
        return []
    if task.get("status_code") not in (20000, None):
        raise RuntimeError(
            f"DataForSEO task {task.get('status_code')}: "
            f"{task.get('status_message')}")
    results = task.get("result") or []
    if not results or not results[0]:
        return []
    return results[0].get("items") or []


# ── The run ───────────────────────────────────────────────────────────


@dataclass
class DiscoveryReport:
    """What a run did — the honest version, including what it skipped."""

    provider: str = ""
    queries: int = 0
    queries_ran: int = 0     # attempted provider requests
    queries_succeeded: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    found: int = 0
    junk: int = 0
    # Counted apart from `junk` on purpose: junk is a judgement about whether
    # a result is a business at all, this is a company the operator has
    # forbidden. Folding them together would hide a do-not-contact list that
    # has grown too broad inside a number nobody questions.
    excluded: int = 0
    new_companies: int = 0
    known_companies: int = 0
    observations: int = 0
    run_id: str = ""
    errors: list[str] = field(default_factory=list)
    stopped: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "queries": self.queries,
            "queries_ran": self.queries_ran,
            "queries_succeeded": self.queries_succeeded,
            "estimated_cost": round(self.estimated_cost, 4),
            "actual_cost": round(self.actual_cost, 4),
            "found": self.found, "junk": self.junk,
            "excluded": self.excluded,
            "new_companies": self.new_companies,
            "known_companies": self.known_companies,
            "observations": self.observations,
            "run_id": self.run_id, "errors": self.errors,
            "stopped": self.stopped,
        }


# Built-in searches beyond "your ICP". Each says how to turn an industry into
# a search, and which kind of provider can run it.
PRESETS = {
    # Startups that say on their own site they're YC-backed. Never
    # ycombinator.com itself, whose terms forbid scraping and harvesting
    # contacts; its pages and the startup databases are excluded too.
    "yc": {
        "label": "YC startups",
        "kinds": ("serp",),
        "term": ('"backed by Y Combinator" {industry} -site:ycombinator.com '
                 "-site:workatastartup.com -site:startupschool.org "
                 "-site:linkedin.com -site:crunchbase.com"),
        "local": False,
    },
    # Companies hiring for one of your hiring_signals roles (else ICP titles),
    # found by their OWN careers pages. The hosted boards are excluded: a
    # boards.greenhouse.io hit names a board, not a website to profile.
    # Profiling then reads the board their site links to and records the
    # role (HIRING_ROLE); the search hit alone records nothing.
    "hiring": {
        "label": "Companies hiring for your roles",
        "kinds": ("serp",),
        "term": ('"{role}" careers {industry} -site:greenhouse.io -site:lever.co '
                 "-site:ashbyhq.com -site:workable.com -site:smartrecruiters.com "
                 "-site:jobvite.com -site:icims.com -site:bamboohr.com -site:recruitee.com "
                 "-site:teamtailor.com -site:personio.de -site:breezy.hr -site:applytojob.com "
                 "-site:myworkdayjobs.com -site:ats.rippling.com -site:linkedin.com "
                 "-site:indeed.com -site:glassdoor.com -site:ziprecruiter.com"),
        "local": False,
    },
    # Startups that say on their own site an accelerator backed them. Two
    # searches per industry, because Google reads only the first 32 words.
    "accelerator": {
        "label": "Accelerator startups",
        "kinds": ("serp",),
        "terms": (
            '{industry} ("backed by Techstars" OR "backed by Antler" OR "backed by Entrepreneur First" '
            'OR "backed by South Park Commons" OR "backed by 500 Global") '
            "-site:linkedin.com -site:crunchbase.com",
            # Not "backed by Pioneer": it finds Pioneer Square Labs and Pioneer
            # Fund (the profile still reads a Pioneer claim on the page).
            '{industry} ("backed by Neo" OR "backed by AI Grant" OR "a16z speedrun" '
            'OR "backed by Plug and Play") '
            "-site:linkedin.com -site:crunchbase.com -site:a16z.com -site:neo.com",
        ),
        "local": False,
    },
    # Companies announcing their own raise, from the last ~18 months. The
    # news sites and databases are excluded here and filtered as junk.
    "funding": {
        "label": "Recently funded",
        "kinds": ("serp",),
        "term": ('{industry} ("we raised" OR "we\'ve raised" OR announces OR raises) '
                 '("seed round" OR "Series A" OR "Series B") after:{since} '
                 "-site:techcrunch.com -site:crunchbase.com -site:pitchbook.com -site:linkedin.com "
                 "-site:prnewswire.com -site:businesswire.com -site:globenewswire.com"),
        "local": False,
    },
}
FUNDING_LOOKBACK_DAYS = 548       # ~18 months: older raises aren't news


def preset_templates(spec: dict) -> tuple[str, ...]:
    """A preset's search templates: one ``term``, or several ``terms``."""
    return tuple(spec.get("terms") or (spec["term"],))

# A hit on careers.acme.com is acme.com's careers page.
_CAREERS_HOST = re.compile(r"^(careers|jobs|join|work|apply|hiring)\.", re.I)
_SECOND_LEVEL_SUFFIX = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"}


def company_host(domain: str) -> str:
    """careers.acme.com → acme.com. Never down to a public suffix: jobs.co.uk
    stays jobs.co.uk (and is junk), not co.uk."""
    stripped = _CAREERS_HOST.sub("", domain)
    labels = stripped.split(".")
    if stripped == domain or len(labels) < 2:
        return domain
    if len(labels) == 2 and labels[0] in _SECOND_LEVEL_SUFFIX and len(labels[1]) == 2:
        return domain
    return stripped


def preset_problem(preset: str, provider_key: str, config=None) -> str | None:
    """Why ``preset`` can't run on ``provider_key`` (with ``config``), or None."""
    if not preset:
        return None
    spec = PRESETS.get(preset)
    if spec is None:
        return f"Unknown preset {preset!r}. Presets: {', '.join(PRESETS)}."
    provider = PROVIDERS.get(provider_key)
    if provider is None or provider.kind not in spec["kinds"]:
        names = ", ".join(k for k, p in PROVIDERS.items() if p.kind in spec["kinds"])
        return (f"The {spec['label']} preset is a web search, so it needs a search "
                f"provider ({names}), not {provider_key}.")
    if any("{role}" in tpl for tpl in preset_templates(spec)) and config is not None:
        from sellbird.collectors.ats import role_keywords
        if not role_keywords(getattr(config, "icp", None)):
            return (f"The {spec['label']} preset searches for roles, and sellbird.yaml has "
                    "none: set icp.hiring_signals (or icp.titles).")
    return None


def build_queries(config, cities: list[str] | None = None,
                  depth: int = 30, limit: int = 100, preset: str = "",
                  terms: list[str] | None = None) -> list[DiscoveryQuery]:
    """Turn the configured ICP (or explicit query terms) into a concrete query list.

    One query per (industry or keyword variant x city). Coordinates come from
    ``icp.geo_coordinates`` when present, because listings providers need a
    radius, not a place name. A ``preset`` (PRESETS) rewrites each industry
    into its search; a non-local one only uses cities you pass.
    """
    icp = config.icp
    custom_terms = [str(t).strip() for t in (terms or []) if str(t).strip()]
    base_terms = custom_terms or list(icp.industries) or [config.product.name]
    spec = PRESETS.get(preset) if preset else None
    if spec:
        templates = preset_templates(spec)
        since = (date.today() - timedelta(days=FUNDING_LOOKBACK_DAYS)).isoformat()
        from sellbird.collectors.ats import role_keywords
        roles = role_keywords(icp) if any("{role}" in tpl for tpl in templates) else [""]
        terms = [tpl.format(industry=t, role=r, since=since)
                 for r in roles for t in base_terms for tpl in templates]
        places = cities if cities is not None else ([""] if not spec["local"] else list(icp.geography) or [""])
    else:
        # "roofing contractor" is one way to search for a roofer. Each
        # configured variant is another search in every city — ranks differ
        # by phrasing, and the email should quote the phrase that was used.
        variants = {} if custom_terms else (getattr(icp, "keyword_variants", {}) or {})
        terms = list(dict.fromkeys(
            v.strip() for t in base_terms for v in [t, *variants.get(t, [])] if v.strip()))
        places = cities if cities is not None else list(icp.geography) or [""]
    coords = getattr(icp, "geo_coordinates", {}) or {}

    return [
        DiscoveryQuery(term=term, location=place, depth=depth, limit=limit,
                       coordinate=coords.get(place, ""), preset=preset if spec else "")
        for term in terms
        for place in places
    ]


def estimate_cost(provider_key: str, queries: list[DiscoveryQuery]) -> float:
    """Projected spend. Always call this before a run — and print it."""
    provider = PROVIDERS.get(provider_key)
    return provider.estimate(queries) if provider else 0.0


async def run_discovery(
    state,
    config,
    provider_key: str = DEFAULT_PROVIDER,
    queries: list[DiscoveryQuery] | None = None,
    max_spend: float = 1.0,
    dry_run: bool = False,
) -> DiscoveryReport:
    """Discover businesses and record what was learned about them.

    ``dry_run`` estimates and returns without calling anything. Run it first,
    every time: the first small trial is what exposes the data-quality
    problems that would otherwise poison every market you scale to.
    """
    from sellbird.config import live_do_not_contact, load_env
    from sellbird.gate import do_not_contact_domain_match
    from sellbird.signals import seed_signal_catalog

    provider = PROVIDERS.get(provider_key)
    if provider is None:
        raise ValueError(f"unknown discovery provider: {provider_key!r}")

    if queries is None:
        queries = build_queries(config)
    if provider.needs_coordinates:
        queries = await resolve_coordinates(state, queries)
    report = DiscoveryReport(provider=provider_key, queries=len(queries))
    report.estimated_cost = provider.estimate(queries)

    if dry_run:
        return report

    env = _hydrate_env(load_env().model_dump())
    missing = provider.missing_keys(env)
    if missing:
        names = ", ".join(k.upper() for k in missing)
        report.errors.append(
            f"{provider.label} is not ready: {names} is empty or still the "
            f"template placeholder in .env")
        report.stopped = "not configured"
        return report

    if report.estimated_cost > max_spend:
        report.errors.append(
            f"estimated ${report.estimated_cost:.2f} exceeds the ${max_spend:.2f} "
            f"cap — raise the cap or run fewer queries")
        report.stopped = "over budget"
        return report

    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes()
    if not confirmed:
        report.errors.append(
            "no signals confirmed — confirm signals first (dashboard → Signals, "
            "or `sellbird signals --confirm free`) or discovery has nothing to record")
        report.stopped = "no confirmed signals"
        return report

    report.run_id = await state.start_run(
        "discover", provider=provider_key,
        params={"queries": len(queries), "estimate": report.estimated_cost},
    )

    # Resolved once per run, not per result: the list may live in a file, and
    # re-reading it a few hundred times would turn a config lookup into I/O
    # inside the hot loop.
    do_not_contact = live_do_not_contact(config)

    # Each business's base facts are written once per run; its ranks once per
    # (business, keyword), and again only if a repeat search ranks it better.
    seen: set[str] = set()
    best_ranks: dict[tuple[str, str], tuple] = {}
    company_ids: dict[str, str] = {}
    ranked_in_run: dict[tuple[str, str], set[str]] = {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for i, query in enumerate(queries):
                # The kill switch is read between batches, not once at the
                # top: a long run has to be stoppable while it is running.
                if await state.get_setting("discovery_paused"):
                    report.stopped = "paused"
                    break
                if report.actual_cost > max_spend:
                    report.stopped = "budget reached"
                    break
                if i and provider.key == "osm":
                    await asyncio.sleep(OpenStreetMap.MIN_INTERVAL)

                try:
                    report.queries_ran += 1
                    provider.last_cost = 0.0
                    provider.last_error = provider.stop_reason = ""
                    provider.complete = True
                    provider.remaining_budget = max(0.0, max_spend - report.actual_cost)
                    found = await provider.fetch(client, env, query)
                    report.queries_succeeded += 1
                except ProviderExhausted as e:
                    logger.warning("discover: %s stopped: %s", provider_key, e)
                    report.stopped = str(e)
                    break
                except Exception as e:
                    # A failure is an observation about the run, not a reason
                    # to lose the queries that already succeeded.
                    logger.warning("discover: %s failed on %r: %s",
                                   provider_key, query.keyword(), e)
                    report.errors.append(f"{query.keyword()}: {e}")
                    continue
                finally:
                    report.actual_cost += provider.last_cost

                if provider.last_error:
                    report.errors.append(f"{query.keyword()}: {provider.last_error}")
                report.found += len(found)
                suppressed_domains: set[str] = set()
                suppressed_among = getattr(state, "suppressed_among", None)
                is_suppressed = getattr(state, "is_suppressed", None)
                if callable(suppressed_among):
                    probes = [
                        f"__domain_check__@{normalize_domain(b.domain)}"
                        for b in found
                        if getattr(b, "domain", "")
                    ]
                    if probes:
                        try:
                            res = suppressed_among(probes)
                            if inspect.isawaitable(res):
                                res = await res
                            suppressed_domains = {
                                k.split("@", 1)[1] for k in (res or {}) if "@" in k
                            }
                        except Exception as e:
                            logger.debug("discover suppressed_among failed: %s", e)

                batch = []
                for biz in found:
                    if query.preset == "hiring" and biz.domain and not is_junk(biz.domain):
                        # Judged on the host as found first: jobs.lever.co
                        # stripped would be lever.co, a vendor, not junk.
                        biz.domain = company_host(biz.domain)
                    if is_junk(biz.domain, biz.raw_title or biz.name):
                        report.junk += 1
                        continue
                    # Before the upsert, so a forbidden company never becomes a
                    # row and never reaches profiling. Checking it at send time
                    # alone still lets one through discovery, where it costs
                    # credits to enrich and sits in every cohort until somebody
                    # notices it by eye.
                    if do_not_contact_domain_match(biz.domain, do_not_contact):
                        report.excluded += 1
                        continue
                    norm_dom = normalize_domain(biz.domain)
                    if norm_dom and norm_dom in suppressed_domains:
                        report.excluded += 1
                        continue
                    if norm_dom and not callable(suppressed_among) and callable(is_suppressed):
                        try:
                            res = is_suppressed(f"__domain_check__@{norm_dom}")
                            if inspect.isawaitable(res):
                                res = await res
                            if res:
                                report.excluded += 1
                                continue
                        except Exception:
                            pass
                    ident = biz.identity()
                    if not ident:
                        continue
                    first = ident not in seen
                    if first:
                        seen.add(ident)
                        company_id, is_new = await _upsert(state, biz, query)
                        if is_new:
                            report.new_companies += 1
                        else:
                            report.known_companies += 1
                        company_ids[ident] = company_id
                    company_id = company_ids[ident]
                    phrase = biz.keyword or query.keyword()
                    for code, value in zip(RANK_CODES, _rank_tuple(biz)):
                        if value is not None:
                            ranked_in_run.setdefault((code, phrase), set()).add(company_id)

                    rank_key = (ident, phrase.lower())
                    ranks = _rank_tuple(biz)
                    previous = best_ranks.get(rank_key)
                    improved = previous is None or any(
                        new is not None and (old is None or new < old)
                        for new, old in zip(ranks, previous))
                    if not first and not improved:
                        continue
                    if previous is not None:
                        # A repeat of the same search: record only the ranks
                        # it improved, so a worse one never becomes "latest".
                        biz = replace(biz, **{
                            name: (new if new is not None and (old is None or new < old)
                                   else None)
                            for name, new, old in zip(RANK_FIELDS, ranks, previous)})
                    if improved:
                        best_ranks[rank_key] = tuple(
                            _best(old, new) for new, old in
                            zip(ranks, previous or (None,) * len(ranks)))
                    obs_rows = _observations(biz, company_id, confirmed,
                                             report.run_id, query=query,
                                             ranks_only=not first)
                    batch.extend(obs_rows)
                    await _sync_discovery_company_signals(state, company_id, obs_rows)

                # A business this phrase ranked before, and that no search for
                # it this run has found, is no longer ranked (within depth) or
                # no longer in the pack. Say so, so the latest reading is true.
                phrase = query.keyword()
                for code in provider.rank_codes if provider.complete else ():
                    if code not in confirmed:
                        continue
                    found_now = ranked_in_run.setdefault((code, phrase), set())
                    for gone in await state.ranked_company_ids(code, phrase) - found_now:
                        batch.append({
                            "company_id": gone, "signal_code": code,
                            "collector": "discover", "value_num": 0.0,
                            "value_text": phrase, "confidence": 1.0,
                            "evidence_url": "", "run_id": report.run_id,
                        })

                # Flush every batch. The observations are the asset; buffering
                # them until some threshold means a run that dies loses them.
                if batch:
                    report.observations += await state.add_observations(
                        batch, run_id=report.run_id)
                if provider.stop_reason:
                    report.stopped = provider.stop_reason
                    break

        await state.finish_run(report.run_id, status="completed",
                               records=report.new_companies + report.known_companies,
                               cost_usd=report.actual_cost)
    except Exception as e:
        logger.exception("discover run failed")
        report.errors.append(str(e))
        await state.finish_run(report.run_id, status="failed",
                               records=report.new_companies,
                               cost_usd=report.actual_cost, error=str(e))
    return report


async def _upsert(state, biz: Business, query: DiscoveryQuery) -> tuple[str, bool]:
    """Record the business, returning (id, was_new)."""
    # Look before inserting, so "already knew this one" is a fact we report
    # rather than something inferred from an insert's return value.
    existing = None
    if biz.domain:
        existing = await state.get_company_by_domain(biz.domain)
    for external in (biz.external_id, *biz.alt_ids):
        if existing is None and external:
            existing = await state.get_company_by_external_id(external)
    if existing:
        fill_fn = getattr(state, "fill_company_fields", None)
        if callable(fill_fn):
            await fill_fn(
                existing.id,
                domain=biz.domain,
                website=biz.website,
                phone=biz.phone,
                location=biz.location or query.location,
            )
        return existing.id, False

    company = Company(
        name=biz.name,
        domain=biz.domain,
        website=biz.website,
        phone=biz.phone,
        industry=biz.category or query.term,
        location=biz.location or query.location,
        source=f"discover:{biz.provider}",
        source_url=biz.source_url,
        external_id=biz.external_id,
    )
    return await state.add_company(company), True


RANK_FIELDS = ("rank", "local_pack_rank", "maps_rank")
RANK_CODES = ("SERP_RANK", "IN_LOCAL_PACK", "MAPS_RANK")      # same order


def _rank_tuple(biz: Business) -> tuple:
    return tuple(getattr(biz, name) for name in RANK_FIELDS)


def _observations(biz: Business, company_id: str, confirmed: set[str],
                  run_id: str, query: DiscoveryQuery | None = None,
                  ranks_only: bool = False) -> list[dict]:
    """Every fact this business gave us, as rows — and only the confirmed ones.

    Rank rows carry the search phrase in ``value_text``: "#14 for 'roofer
    Denver, CO'" is a fact; "#14" on its own isn't. ``ranks_only`` is for a
    business already recorded this run, found again by another search.
    """
    rows: list[dict] = []

    def add(code, *, num=None, text="", conf=1.0):
        if code in confirmed:
            rows.append({
                "company_id": company_id, "signal_code": code,
                "collector": "discover", "value_num": num, "value_text": text,
                "confidence": conf, "evidence_url": biz.source_url,
                "run_id": run_id,
            })

    keyword = biz.keyword or (query.keyword() if query is not None else "")
    if biz.rank is not None:
        add("SERP_RANK", num=float(biz.rank), text=keyword)
    if biz.local_pack_rank is not None:
        add("IN_LOCAL_PACK", num=float(biz.local_pack_rank), text=keyword)
    if biz.maps_rank is not None:
        add("MAPS_RANK", num=float(biz.maps_rank), text=keyword)
    if ranks_only:
        return rows

    add("FOUND_IN_SERP", text=biz.provider)
    if biz.has_website is False:
        add("NO_WEBSITE", num=1.0)
    if biz.is_claimed is False:
        add("UNCLAIMED_LISTING", num=1.0)
    if biz.rating is not None:
        add("REVIEW_RATING", num=float(biz.rating))
    if biz.review_count is not None:
        add("REVIEW_COUNT", num=float(biz.review_count))
    if query is not None and query.preset == "yc":
        # The search matched "backed by Y Combinator" on a page of their own
        # site (third-party sites are filtered out above). Short of reading
        # that page ourselves, so below the homepage's own claim.
        add("YC_BACKED", num=1.0, conf=0.7)
    if query is not None and query.preset == "accelerator":
        # Same, for the other programs; which one isn't known until the
        # profile reads their pages. A funding hit isn't recorded: the
        # raise's date matters, so the profile reads the hit page itself.
        add("ACCELERATOR", num=1.0, conf=0.6)
    return rows


def _hydrate_env(env: dict) -> dict:
    """Fill new open-source provider keys from os.environ when absent from EnvConfig."""
    out = dict(env)
    for extra_key in ("searxng_url", "tavily_api_key", "firecrawl_api_key", "firecrawl_api_url"):
        if extra_key not in out:
            out[extra_key] = os.getenv(extra_key.upper(), "")
    return out


async def _sync_discovery_company_signals(state, company_id: str, obs_rows: list[dict]) -> None:
    """Mirror confirmed discovery observations onto companies.signals for Writer."""
    updater = getattr(state, "update_company_signals", None)
    if updater is None or not company_id or not obs_rows:
        return
    today = date.today().isoformat()
    signals: list[dict] = []
    rating_val = None
    review_cnt = None
    for row in obs_rows:
        code = row.get("signal_code")
        num = row.get("value_num")
        txt = row.get("value_text") or ""
        if code == "SERP_RANK" and num and num > 0:
            detail = f"#{int(num)} on Google" + (f" for '{txt}'" if txt else "")
            signals.append({"type": "serp_rank", "detail": detail, "found_at": today})
        elif code == "IN_LOCAL_PACK" and num and num > 0:
            detail = f"#{int(num)} in Google local 3-pack" + (f" for '{txt}'" if txt else "")
            signals.append({"type": "local_pack", "detail": detail, "found_at": today})
        elif code == "MAPS_RANK" and num and num > 0:
            detail = f"#{int(num)} on Google Maps" + (f" for '{txt}'" if txt else "")
            signals.append({"type": "maps_rank", "detail": detail, "found_at": today})
        elif code == "NO_WEBSITE" and num == 1:
            signals.append({"type": "no_website", "detail": "No website listed on business profile", "found_at": today})
        elif code == "UNCLAIMED_LISTING" and num == 1:
            signals.append({"type": "unclaimed_listing", "detail": "Unclaimed Google Business Profile", "found_at": today})
        elif code == "REVIEW_RATING" and num is not None:
            rating_val = num
        elif code == "REVIEW_COUNT" and num is not None:
            review_cnt = int(num)
    if rating_val is not None or review_cnt is not None:
        parts = []
        if rating_val is not None:
            parts.append(f"{rating_val:g} stars")
        if review_cnt is not None:
            parts.append(f"{review_cnt} reviews")
        signals.append({"type": "reviews", "detail": ", ".join(parts), "found_at": today})
    if signals:
        try:
            await updater(company_id, new_signals=signals)
        except Exception as exc:
            logger.debug("update_company_signals failed for %s: %s", company_id, exc)


def provider_menu(env: dict | None = None) -> list[dict]:
    """The menu the dashboard renders: what each provider does, and its cost.

    The CLI calls this bare; without a load, every keyed provider would show
    "needs setup" even with a working key in .env.
    """
    if env is None:
        from sellbird.config import load_env
        env = _hydrate_env(load_env().model_dump())
    return [
        {
            "key": p.key, "label": p.label, "kind": p.kind, "blurb": p.blurb,
            "cost_note": p.cost_note, "free_tier": p.free_tier,
            "signup_url": p.signup_url, "caveat": p.caveat,
            "env_keys": [k.upper() for k in p.env_keys],
            "configured": p.configured(env),
            "needs_key": bool(p.env_keys),
        }
        for p in PROVIDERS.values()
    ]

