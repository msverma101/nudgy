"""Potential events: public events across Europe, your home city first, ranked by how well they fit you.

Only public sources that are meant to be read, no logins:
- Luma calendar subscriptions (the iCal "Subscribe" link every public Luma calendar offers)
- Luma city pages (the public "popular events" list for each city)
- Meetup group calendars (each group's public iCal link)
- dev.events (its public RSS feed)

LinkedIn and Eventbrite are left out: neither offers a public way to search events without signing in,
and scraping them breaks their terms. Many of their events are also listed on Luma or dev.events.

Off by default. Turn it on by copying examples/discover.json to ~/.config/nudgy/discover.json, then edit
the sources to fit you. They are fetched at most every 6 hours and cached.
"""
from __future__ import annotations

import logging
import json
import re
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from . import luma
from .model import Item, config_dir, data_dir

log = logging.getLogger(__name__)

AGENT = "Nudgy event finder (personal, read-only)"
REFRESH = timedelta(hours=6)
DAYS_AHEAD = 150
KEEP = 30           # most events shown
TOP = 6             # shown even in Important-only view
MIN_FIT = 45

# Off until ~/.config/nudgy/discover.json exists. examples/discover.json is a small starter;
# copy it there, set your home city and add the sources you follow.
DEFAULTS = {
    "luma_calendars": {},
    "organiser_boost": {},
    "luma_cities": [],
    "meetup_groups": [],
    "rss": [],
    "watchlist": [],
    "home": "",
}

# --- where -------------------------------------------------------------------------------------
GERMANY = (r"germany|deutschland|munich|münchen|muenchen|hamburg|frankfurt|cologne|köln|stuttgart|dresden|"
           r"leipzig|potsdam|düsseldorf|heidelberg|karlsruhe|hannover|hanover|aachen|darmstadt|tübingen")
EUROPE = (r"united kingdom|\buk\b|england|scotland|london|oxford|edinburgh|manchester|bristol|france|paris|lyon|"
          r"palaiseau|netherlands|amsterdam|rotterdam|delft|eindhoven|the hague|switzerland|schweiz|suisse|zurich|"
          r"zürich|geneva|lausanne|basel|st\.? gallen|leukerbad|austria|österreich|vienna|wien|italy|italia|milan|"
          r"milano|rome|roma|venice|turin|spain|españa|madrid|barcelona|valencia|portugal|lisbon|lisboa|porto|"
          r"belgium|brussels|bruxelles|antwerp|ghent|denmark|copenhagen|sweden|stockholm|gothenburg|norway|oslo|"
          r"finland|helsinki|espoo|poland|warsaw|kraków|krakow|wroclaw|czech|prague|praha|ireland|dublin|estonia|"
          r"tallinn|latvia|riga|lithuania|vilnius|luxembourg|greece|athens|romania|bucharest|bulgaria|sofia|hungary|"
          r"budapest|croatia|zagreb|slovenia|ljubljana|slovakia|bratislava|serbia|belgrade|ukraine|kyiv|iceland")
ONLINE = r"\bonline\b|virtual|zoom\.us|livestream|google meet|remote event"
REGION_BONUS = {"Home": 20, "Germany": 12, "Europe": 6, "Online": 4}
HOME_KM = 40                         # events this close to your home city count as home
CITIES = {"Berlin": (52.52, 13.405), "Potsdam": (52.39, 13.06), "Munich": (48.137, 11.575), "Hamburg": (53.551, 9.993),
          "Frankfurt": (50.11, 8.682), "Cologne": (50.938, 6.96), "Stuttgart": (48.776, 9.183),
          "Dresden": (51.05, 13.738), "Leipzig": (51.34, 12.375), "Karlsruhe": (49.007, 8.404),
          "Düsseldorf": (51.227, 6.773), "Hannover": (52.375, 9.732), "Heidelberg": (49.399, 8.672),
          "London": (51.507, -0.128), "Cambridge UK": (52.205, 0.119), "Oxford": (51.752, -1.258),
          "Paris": (48.857, 2.352), "Amsterdam": (52.37, 4.895), "Zurich": (47.377, 8.541),
          "Lausanne": (46.52, 6.633), "Geneva": (46.204, 6.143), "St. Gallen": (47.424, 9.377),
          "Vienna": (48.208, 16.373), "Milan": (45.464, 9.19), "Rome": (41.903, 12.496), "Venice": (45.44, 12.316),
          "Madrid": (40.417, -3.704), "Barcelona": (41.385, 2.173), "Lisbon": (38.722, -9.139),
          "Brussels": (50.85, 4.352), "Copenhagen": (55.676, 12.568), "Stockholm": (59.329, 18.069),
          "Oslo": (59.914, 10.752), "Helsinki": (60.17, 24.94), "Espoo": (60.205, 24.656), "Warsaw": (52.23, 21.012),
          "Prague": (50.076, 14.438), "Dublin": (53.35, -6.26), "Tallinn": (59.437, 24.754),
          "Edinburgh": (55.953, -3.188), "Manchester": (53.48, -2.242), "Luxembourg": (49.611, 6.13),
          "Athens": (37.984, 23.728), "Budapest": (47.498, 19.04), "Bucharest": (44.427, 26.103)}


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def place_from_geo(lat: float, lon: float) -> tuple[str, str] | None:
    """(region, city) for a point, or None outside Europe."""
    if not (34 <= lat <= 72 and -25 <= lon <= 45):
        return None
    city, distance = min(((name, _km((lat, lon), spot)) for name, spot in CITIES.items()), key=lambda c: c[1])
    name = city if distance <= 60 else ""
    spot = CITIES.get(home())
    if spot and _km((lat, lon), spot) <= HOME_KM:
        return "Home", name or home()
    in_germany = 47.2 <= lat <= 55.1 and 5.8 <= lon <= 15.1 and not (lon < 7.5 and lat < 47.8)
    return ("Germany" if in_germany else "Europe"), name

# --- what --------------------------------------------------------------------------------------
KIND = [(r"hackathon|makeathon|buildathon|\bhacks?\b|research sprint|\bsprint\b|\bchallenge\b|game jam|"
         r"\bjam\b|build day", 25, "hackathon"),
        (r"neurips|icml|iclr|cvpr|conference|summit|symposium|\bdemo day\b", 15, "conference"),
        (r"career fair|job fair|hiring|recruit|talent|internship|fellowship|residency", 15, "jobs")]
SAFETY = (r"apart research|bluedot|ai safety|alignment|interpretab|mechanistic|\bevals?\b|red.?team|"
          r"reward hack|oversight|ai control|misalign|sycophan")
ML = [r"machine learning|\bml\b", r"deep learning|neural|transformer", r"\bllms?\b|language model|foundation model",
      r"\bagents?\b|agentic", r"reinforcement|\brl\b", r"computer vision|\bvision\b", r"robot|physical ai",
      r"simulation|scientific|science|physics", r"research", r"\bgpu|compute|inference|training"]
BIG_NAMES = (r"neurips|icml|iclr|anthropic|claude|deepmind|google|openai|mistral|hugging ?face|nvidia|\bmeta\b|"
             r"microsoft|\baws\b|amazon|"
             r"apple|elevenlabs|siemens|\bbmw\b|\bsap\b|bosch|merantix|y ?combinator|\byc\b|a16z|sequoia|"
             r"lovable|cohere|"
             r"nebius|helsing|black forest labs|aleph alpha|deepl|n8n|langchain|perplexity|databricks|snowflake|"
             r"stripe|spotify|zalando|delivery hero|n26|celonis|isomorphic|wayve|synthesia|poolside|h company|"
             r"cursor|replit|vercel|github|\bibm\b|\bintel\b|\bamd\b|qualcomm|samsung|airbus|ellis|"
             r"max planck|fraunhofer")
NOISE = (r"improv|karaoke|run club|\brun\b|running|yoga|book club|party|rave|dinner|brunch|coffee|drinks|"
         r"social|aperitivo|games night|wine|beer|choir")
BEGINNER = r"beginner|intro(duction)? to|non.?technical|for founders|no.?code|for marketers|for pms?\b"
MONEY = re.compile(r"(?:[€$£]|chf|eur|usd|gbp)\s?(\d[\d,.]*)\s?(k|m)?\b|"
                   r"(\d[\d,.]*)\s?(k|m)?\s?(?:€|\$|£|chf|eur|euro|usd|gbp)", re.I)


_boosts: dict[str, int] = dict(DEFAULTS["organiser_boost"])     # refreshed from the config on each collect


def home() -> str:
    """Your home city from discover.json ("home": "Hamburg"), whose events rank first. Empty if not set."""
    return str(config().get("home") or "").strip()


def config() -> dict:
    """Your settings. "enabled" is true once discover.json exists, unless it says "enabled": false."""
    path = config_dir() / "discover.json"
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return {**DEFAULTS, "enabled": False}
    except ValueError:
        saved = {}
    return {**DEFAULTS, "enabled": True, **saved}


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def region(where: str) -> str | None:
    text = where.lower()
    if home() and re.search(rf"\b{re.escape(home().lower())}\b", text):
        return "Home"
    if re.search(GERMANY, text):
        return "Germany"
    if re.search(EUROPE, text):
        return "Europe"
    return None                      # elsewhere in the world, or unknown


def biggest_prize(text: str) -> int:
    best = 0
    for match in MONEY.finditer(text):
        number, unit = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
        try:
            value = float(number.replace(",", "")) if number.count(".") <= 1 else float(number.replace(".", ""))
        except ValueError:
            continue
        value *= {"k": 1_000, "m": 1_000_000}.get((unit or "").lower(), 1)
        if 100 <= value < 50_000_000:
            best = max(best, int(value))
    return best


def _money(value: int) -> str:
    return f"{value // 1_000_000}M" if value >= 1_000_000 else f"{value // 1000}k" if value >= 1000 else str(value)


def fit(title: str, text: str, place: str, boost: int = 0) -> tuple[int, list[str]]:
    """0-100 and the reasons, from the title, description, organiser and place."""
    whole = f"{title}\n{text}".lower()
    head = title.lower()
    points, reasons = REGION_BONUS[place] + boost, [home() if place == "Home" else place]
    for pattern, value, label in KIND:
        if re.search(pattern, head) or (label == "jobs" and re.search(pattern, whole)):
            points += value
            reasons.append(label)
    if re.search(SAFETY, whole):
        points += 12
        reasons.append("AI safety")
    points += min(20, 5 * sum(bool(re.search(p, whole)) for p in ML))
    if re.search(r"\bai\b|artificial intelligence", whole):
        points += 5
    names = sorted({m.group(0).strip() for m in re.finditer(BIG_NAMES, f"{title}\n{text}", re.I)}, key=str.lower)
    names = list({n.lower(): n for n in reversed(names)}.values())        # prefer a capitalised spelling
    names = [n if n != n.lower() else n.title() for n in names]
    if names:
        points += min(20, 10 * len(names))
        reasons.append(", ".join(names[:3]))
    prize = biggest_prize(whole) if re.search(r"prize|award|win|grant|cash|bounty|pool", whole) else 0
    if prize:
        points += 20 if prize >= 50_000 else 15 if prize >= 10_000 else 8 if prize >= 1_000 else 3
        reasons.append(f"prize {_money(prize)}")
    elif re.search(r"prizes?|cash|bounty", whole):
        points += 5
        reasons.append("prizes")
    if re.search(NOISE, head):
        points -= 25
    if re.search(BEGINNER, head):
        points -= 10
    return max(0, min(100, points)), reasons


def _event(title, text, where, start, end, url, organiser="", geo: tuple[str, str] | None = None) -> Item | None:
    """geo: (region, city) when the source gave coordinates; otherwise the place is read from the text."""
    if not title:
        return None
    if geo:
        place, city = geo
        city = city or place
    else:
        place = region(where) or region(title)
        if place is None and re.search(ONLINE, f"{where} {title} {text[:400]}", re.I):
            place = "Online"
        if place is None:
            return None
        city = where.split(",")[0].strip() if place != "Online" else "Online"
        if place != "Online" and (not city or city.startswith("http") or len(city) > 40 or re.search(r"\d", city)):
            city = place
    boost = _boosts.get(organiser, 0)
    score, reasons = fit(title, f"{organiser}\n{text}", place, boost)
    if boost and organiser != "watchlist" and organiser not in " ".join(reasons):
        reasons.insert(1, organiser)
    if score < MIN_FIT:
        return None
    detail = " · ".join(dict.fromkeys([city] + reasons[1:] + [f"fit {score}"]))
    return Item(id=f"potential:{url or title}", source="potential", title=title, detail=detail,
                due=start.isoformat(), end=end.isoformat() if end else None, url=url,
                tags=["event", f"fit:{score}"])


def from_ics(text: str, now: datetime, organiser: str = "") -> list[Item]:
    items = []
    for event in luma.parse_ics(text):
        start = luma._moment(event.get("DTSTART"), now.tzinfo)
        end = luma._moment(event.get("DTEND"), now.tzinfo)
        if start is None or event.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
            continue
        if not isinstance(start, datetime):          # all-day: multi-day hackathons are often listed so
            start = datetime.combine(start, datetime.min.time(), now.tzinfo).replace(hour=9)
            end = datetime.combine(end, datetime.min.time(), now.tzinfo) if end else None
        title = luma._unescape(event.get("SUMMARY", ({}, ""))[1])
        where = luma._unescape(event.get("LOCATION", ({}, ""))[1])
        about = luma._unescape(event.get("DESCRIPTION", ({}, ""))[1])
        url = event.get("URL", ({}, ""))[1]
        if not url.startswith("https://"):
            found = re.search(r"https://(?:lu\.ma|luma\.com|www\.meetup\.com)/[\w/-]+", about)
            url = where if where.startswith("https://") else found.group(0) if found else ""
        geo = None
        point = re.match(r"\s*(-?[\d.]+)\s*[;,]\s*(-?[\d.]+)", event.get("GEO", ({}, ""))[1])
        if point:
            geo = place_from_geo(float(point.group(1)), float(point.group(2)))
            if geo is None:
                continue                     # outside Europe
        if where.startswith("http"):
            where = ""
        item = _event(title, about, where, start, end, url, organiser, geo)
        if item:
            items.append(item)
    return items


def from_luma_city(html: str, now: datetime) -> list[Item]:
    found = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not found:
        return []
    entries = json.loads(found.group(1))["props"]["pageProps"]["initialData"]["data"]["events"]
    items = []
    for entry in entries:
        event = entry.get("event", {})
        info = event.get("geo_address_info") or {}
        where = ", ".join(filter(None, [info.get("city"), info.get("country")])) or (
            "Online" if event.get("location_type") == "online" else "")
        calendar = entry.get("calendar") or {}
        about = " ".join(filter(None, [calendar.get("name"), calendar.get("description_short"),
                                       *[h.get("name") or "" for h in entry.get("hosts") or []],
                                       *[h.get("bio_short") or "" for h in entry.get("hosts") or []]]))
        start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00")).astimezone(now.tzinfo)
        end = event.get("end_at")
        end = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(now.tzinfo) if end else None
        spot = event.get("coordinate") or info.get("coordinate") or {}
        geo = None
        if spot.get("latitude") is not None:
            geo = place_from_geo(float(spot["latitude"]), float(spot["longitude"]))
            if geo is None:
                continue
        if event.get("location_type") == "online":
            geo = ("Online", "Online")
        item = _event(event.get("name", ""), about, where, start, end, f"https://luma.com/{event.get('url', '')}",
                      geo=geo)
        if item:
            items.append(item)
    return items


def from_rss(text: str, now: datetime) -> list[Item]:
    items = []
    for entry in ElementTree.fromstring(text).iter("item"):
        title = entry.findtext("title") or ""
        about = entry.findtext("description") or ""
        said = re.search(r"happening on ([A-Z][a-z]+ \d{1,2}, \d{4}),\s*(.+?)\.\s*(?:More|$)", about)
        if said:
            start = datetime.strptime(said.group(1), "%B %d, %Y").replace(hour=9, tzinfo=now.tzinfo)
            where = said.group(2)
        else:
            try:
                start, where = parsedate_to_datetime(entry.findtext("pubDate") or "").astimezone(now.tzinfo), ""
            except (TypeError, ValueError):
                continue
        item = _event(title, f"{about} {entry.findtext('category') or ''}", where, start, None,
                      entry.findtext("link") or "")
        if item:
            items.append(item)
    return items


def _jobs(cfg: dict) -> list[tuple[str, str, str]]:
    jobs = [(f"Luma: {name}", f"https://api2.luma.com/ics/get?entity=calendar&id={cal}", "ics:" + name)
            for cal, name in cfg["luma_calendars"].items()]
    jobs += [(f"Luma {city}", f"https://luma.com/{city}", "city") for city in cfg["luma_cities"]]
    jobs += [(f"Meetup {g}", f"https://www.meetup.com/{g}/events/ical/", "ics:") for g in cfg["meetup_groups"]]
    jobs += [(url, url, "rss") for url in cfg["rss"]]
    if cfg.get("watchlist"):
        jobs.append(("watchlist", "", "watch"))
    return jobs


def from_watchlist(entries: list[dict], now: datetime) -> list[Item]:
    items = []
    for entry in entries:
        start = datetime.fromisoformat(entry["start"])
        start = start.replace(hour=9, tzinfo=now.tzinfo) if start.tzinfo is None else start
        end = datetime.fromisoformat(entry["end"]).replace(hour=18, tzinfo=now.tzinfo) if entry.get("end") else None
        item = _event(entry["title"], entry.get("about", ""), entry.get("where", ""), start, end, entry.get("url", ""),
                      "watchlist")
        if item:
            items.append(item)
    return items


def collect(now: datetime) -> tuple[list[Item], dict[str, str]]:
    """Fetch every source, keep upcoming European events that fit, best first."""
    errors: dict[str, str] = {}

    def run(job):
        name, url, kind = job
        try:
            if kind == "watch":
                return from_watchlist(config().get("watchlist", []), now)
            text = _get(url)
            if kind == "city":
                return from_luma_city(text, now)
            if kind == "rss":
                return from_rss(text, now)
            return from_ics(text, now, kind[4:])
        except Exception as error:          # one broken source must not stop the others
            log.warning("event source %s failed: %s: %s", name, type(error).__name__, str(error)[:120])
            errors[name] = str(error)[:120]
            return []

    cfg = config()
    _boosts.clear()
    _boosts.update(cfg.get("organiser_boost", {}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [item for batch in pool.map(run, _jobs(cfg)) for item in batch]
    horizon = now + timedelta(days=DAYS_AHEAD)
    best: dict[str, Item] = {}
    for item in found:
        start = datetime.fromisoformat(item.due)
        if not now <= start <= horizon:
            continue
        key = luma._key(item)[0] + item.due[:10]
        if key not in best or _fit(item) > _fit(best[key]):
            best[key] = item
    ranked = sorted(best.values(), key=lambda i: (-_fit(i), i.due))[:KEEP]
    for item in ranked[:TOP]:
        item.tags.append("top")
    return ranked, errors


def _fit(item: Item) -> int:
    return next((int(t[4:]) for t in item.tags if t.startswith("fit:")), 0)


def cache_path():
    return data_dir() / "discover-cache.json"


def fetch(now: datetime, force: bool = False, applied=None) -> tuple[list[Item], list[str]]:
    """Events, plus names of events you already applied to. Cached for 6 hours so the regular
    5-minute sync does not hit every site. applied: optional function returning those names."""
    if not config()["enabled"]:
        return [], []
    path = cache_path()
    try:
        cached = json.loads(path.read_text())
        if not force and now - datetime.fromisoformat(cached["fetched"]) < REFRESH:
            return [Item(**i) for i in cached["items"]], cached.get("applied", [])
    except (FileNotFoundError, ValueError, KeyError):
        pass
    names: list[str] = []
    if applied:
        from .google import NETWORK_ERRORS, describe
        try:
            names = applied()
        except NETWORK_ERRORS as error:
            log.info("Luma applications from Gmail skipped: %s", describe(error))
            names = []                       # Gmail not connected: only the calendar check is used
    items, errors = collect(now)
    if not items and errors:
        raise RuntimeError(f"no event sources answered ({len(errors)} failed)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": now.isoformat(), "errors": errors, "applied": names,
                                "items": [asdict(i) for i in items]}, indent=1))
    return items, names


def drop_known(found: list[Item], mine: list[Item], applied: list[str] = ()) -> list[Item]:
    """Leave out events you are already registered, applied or waitlisted for."""
    titles = {luma._key(i)[0] for i in mine} | {re.sub(r"\W+", "", name.lower()) for name in applied}
    urls = {i.url.rstrip("/") for i in mine if i.url}
    left = [i for i in found if luma._key(i)[0] not in titles and i.url.rstrip("/") not in urls]
    for rank, item in enumerate(left):              # the best few that are left are shown in Important only
        item.tags = [t for t in item.tags if t != "top"] + (["top"] if rank < TOP else [])
    return left
