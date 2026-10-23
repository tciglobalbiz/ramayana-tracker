"""
Ramayana: Part 1 - all-India advance booking tracker using District (district.in)

How it works
  1. Gets District's list of every city it sells in (about 1,300).
  2. DISCOVERY (twice a day): asks every city, for every language, whether Ramayana is
     listed and on which dates. The list of active cities is saved, so this isn't repeated
     every run.
  3. TRACKING (every run): for every active city x open date x language, gets the full
     showtimes list. That list already includes, for every show, the seats available and
     total seats in each section and their prices - no seat maps needed.
  Each cinema/show is counted once, even when two nearby cities list the same cinema.

Run:  python district_tracker.py
"""

import json
import os
import re
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ------------------------------------------------------------------
# SETTINGS - change these freely
# ------------------------------------------------------------------
MOVIE_TITLE = "Ramayana: Part 1"      # must match the row in your movies table
CONTENT_ID = "176276"                 # Ramayana on District
LISTINGS = {                          # language -> District listing code (frmtid in the page URL)
    "Hindi": "rtb7dmwvuo",
    "Kannada": "ze65w2iloyz",
    "Telugu": "5khjz93zh0",
}
# District only lists the ~50 cinemas nearest a point, so the biggest cities get extra points.
# Every other city uses the one point District gives for it.
EXTRA_POINTS = {
    "mumbai":    [(18.93, 72.83), (19.07, 72.84), (19.12, 72.85), (19.23, 72.86)],
    "delhi-ncr": [(28.63, 77.22), (28.70, 77.10), (28.72, 77.20), (28.58, 77.18), (28.55, 77.25)],
    "bengaluru": [(12.97, 77.59), (13.05, 77.59), (12.90, 77.60), (12.97, 77.75), (12.97, 77.50)],
    "hyderabad": [(17.39, 78.49), (17.45, 78.38), (17.49, 78.40), (17.35, 78.55)],
    "pune":      [(18.52, 73.86), (18.60, 73.76), (18.56, 73.93)],
    "kolkata":   [(22.57, 88.36), (22.58, 88.42), (22.50, 88.35)],
    "chennai":   [(13.08, 80.27), (12.92, 80.23), (13.09, 80.21), (12.92, 80.12)],
    "ahmedabad": [(23.02, 72.57), (23.04, 72.51), (23.02, 72.63)],
}
# Cities whose Ramayana page is checked for new language/format codes (each lists the languages open there)
LANGUAGE_SCOUT_CITIES = ["mumbai", "chennai", "hyderabad", "bengaluru", "kochi", "kolkata", "delhi-ncr"]
# Near-real-time mode: keep running for LOOP_MINUTES, doing a pass every INTERVAL_MINUTES.
# "Hot" dates (opening weekend, plus today/tomorrow) are checked every pass; later dates every
# FAR_EVERY_MINUTES. LOOP_MINUTES=0 does one full pass and stops.
LOOP_MINUTES = float(os.getenv("LOOP_MINUTES", "0"))
INTERVAL_MINUTES = float(os.getenv("INTERVAL_MINUTES", "15"))
FAR_EVERY_MINUTES = float(os.getenv("FAR_EVERY_MINUTES", "60"))
HOT_UNTIL = os.getenv("HOT_UNTIL", "2026-11-08")     # last opening-weekend date
ONLY_CITIES = [c.strip() for c in os.getenv("CITIES", "").split(",") if c.strip()]  # for testing
REDISCOVER_HOURS = float(os.getenv("REDISCOVER_HOURS", "12"))   # how often to re-check all cities
PARALLEL = int(os.getenv("PARALLEL", "4"))     # requests in flight at once (keep it modest)
PAUSE = (0.5, 1.5)                             # pause between batches, seconds
NATIONAL_ONLY = os.getenv("NATIONAL_ONLY", "false").lower() == "true"   # PVR, INOX, Cinepolis only
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
STATE_FILE = "district_state.json"            # used when Supabase isn't connected

IST = ZoneInfo("Asia/Kolkata")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DRY_RUN = not (SUPABASE_URL and SUPABASE_KEY)

API = ("https://www.district.in/gw/consumer/movies/v5/movie?version=3&site_id=1&channel=web"
       "&child_site_id=1&platform=district&movieCode={code}&city_key={city}&content_id={cid}"
       "{date}&latitude={lat}&longitude={lon}&cinemaOrderLogic=3")
CITIES_API = "https://www.district.in/gw/web/get_location_search"
MOVIE_PAGE = "https://www.district.in/movies/namit-malhotra-s-ramayana-movie-tickets-in-{city}-MV" + CONTENT_ID

# Headers District's own page sends with every API request (plus the guest token)
BASE_HEADERS = {"accept": "*/*", "api_source": "district", "cache-control": "no-cache",
                "x-app-type": "ed_web"}

# Runs inside the District page: several requests at once, using the page's own cookies
FETCH_MANY_JS = """
async ({reqs, headers, parallel}) => {
  const out = new Array(reqs.length);
  let next = 0;
  async function worker() {
    while (next < reqs.length) {
      const i = next++;
      const q = reqs[i];
      try {
        const h = Object.assign({}, headers, q.headers || {}, {'x-request-id': crypto.randomUUID()});
        const r = await fetch(q.url, {method: q.method || 'GET', credentials: 'include', headers: h,
                                      body: q.body || undefined});
        out[i] = {status: r.status, text: await r.text()};
      } catch (e) { out[i] = {status: 0, text: String(e)}; }
    }
  }
  await Promise.all(Array.from({length: Math.min(parallel, reqs.length)}, worker));
  return out;
}
"""

FIND_TOKEN_JS = r"""
() => {
  const looks = (v) => typeof v === 'string' && /^\d{10,}_\d+_\w+$/.test(v.trim());
  const stores = [];
  try { stores.push(window.localStorage); } catch (e) {}
  try { stores.push(window.sessionStorage); } catch (e) {}
  for (const st of stores) {
    for (let i = 0; i < st.length; i++) {
      const v = st.getItem(st.key(i));
      if (looks(v)) return v.trim();
      try {
        const stack = [JSON.parse(v)];
        while (stack.length) {
          const x = stack.pop();
          if (looks(x)) return x.trim();
          if (x && typeof x === 'object') for (const k in x) stack.push(x[k]);
        }
      } catch (e) {}
    }
  }
  for (const part of document.cookie.split(';')) {
    const v = decodeURIComponent(part.split('=').slice(1).join('=') || '');
    if (looks(v)) return v.trim();
  }
  return null;
}
"""

# ------------------------------------------------------------------
# Turning District's data into rows
# ------------------------------------------------------------------

def chain_of(cinema_info):
    """INOX is listed under PVR on District (same company) - split it back out by name."""
    name = (cinema_info.get("name") or "").upper()
    key = (cinema_info.get("chainKey") or "").upper()
    if "INOX" in name:
        return "INOX"
    if "PVR" in name or key == "PVR":
        return "PVR"
    if "CINEPOLIS" in name.replace("É", "E") or key == "CINEPOLIS":
        return "CINEPOLIS"
    return (cinema_info.get("chainKey") or "Other").strip().title() or "Other"


def show_counts(session):
    """Seats and gross for one show, from District's per-section numbers (blocked seats excluded)."""
    total = sold = gross = 0
    for a in session.get("areas") or []:
        t = int(a.get("sTotal") or 0)
        av = int(a.get("sAvail") or 0)
        price = float(a.get("price") or 0)
        total += t
        sold += max(t - av, 0)
        gross += max(t - av, 0) * price
    return {"total": total, "available": total - sold, "sold": sold, "gross": round(gross, 2)}


def parse_showtimes(data, date):
    """All shows on `date` (IST) from one showtimes response."""
    page = (data or {}).get("pageData") or {}
    cinemas = (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or [])
    shows = []
    for c in cinemas:
        info = c.get("cinemaInfo") or {}
        for s in c.get("sessions") or []:
            try:
                utc = datetime.fromisoformat(s["showTime"]).replace(tzinfo=timezone.utc)  # District uses UTC
            except (KeyError, ValueError):
                continue
            if utc.astimezone(IST).strftime("%Y-%m-%d") != date:
                continue
            counts = show_counts(s)
            if counts["total"] == 0:
                continue
            shows.append({
                "cinema_id": c.get("id"),
                "cinema": info.get("name") or info.get("label") or str(c.get("id")),
                "city": info.get("city") or "",
                "state": info.get("state") or "",
                "chain": chain_of(info),
                "session_id": str(s.get("sid")),
                "show_time": utc.isoformat(),
                "format": (s.get("scrnFmt") or "2D").upper().replace("-", " "),
                **counts,
            })
    return shows


def language_matches(data, language):
    """District sometimes answers a request for one language with another language's listing
    (e.g. Hindi when Kannada isn't open). Accept a reply only if it is the language asked for."""
    meta = (data or {}).get("meta") or {}
    got = (meta.get("movieLanguage") or "").strip().lower()
    return not got or got == language.lower()


def open_dates(data):
    """Dates District says are open for this city (from its date strip)."""
    meta = (data or {}).get("meta") or {}
    dates = []
    for d in meta.get("showDates") or []:
        if isinstance(d, str):
            dates.append(d[:10])
        elif isinstance(d, dict):
            for k in ("date", "showDate", "value", "key"):
                if isinstance(d.get(k), str):
                    dates.append(d[k][:10]); break
    return sorted(set(dates))

# ------------------------------------------------------------------
# Saving
# ------------------------------------------------------------------

class Store:
    def __init__(self):
        self.sb = None
        if DRY_RUN:
            print("No Supabase keys found - TEST MODE, results are printed only.\n")
            return
        from supabase import create_client
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = self.sb.table("movies").select("id").eq("title", MOVIE_TITLE).limit(1).execute()
        if not res.data:
            sys.exit(f"Movie '{MOVIE_TITLE}' not found in the movies table.")
        self.movie_id = res.data[0]["id"]

    # --- which cities are active (kept between runs) ---
    def load_state(self):
        if self.sb:
            try:
                r = self.sb.table("tracker_state").select("value").eq("key", "district").limit(1).execute()
                if r.data:
                    return r.data[0]["value"] or {}
            except Exception as e:
                print(f"  (couldn't read saved city list from Supabase: {e})")
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_state(self, state):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        if self.sb:
            try:
                self.sb.table("tracker_state").upsert({"key": "district", "value": state,
                                                       "updated_at": datetime.now(timezone.utc).isoformat()},
                                                      on_conflict="key").execute()
            except Exception as e:
                print(f"  (couldn't save city list to Supabase - run tracker_state.sql once: {e})")

    # --- the numbers ---
    def _load_last(self):
        """Each show's last saved sold count and when its last reading was saved."""
        self.last = {}
        start = 0
        while True:
            r = (self.sb.table("shows").select("show_code,last_sold,last_snapshot_at")
                 .eq("movie_id", self.movie_id).like("show_code", "D-%")
                 .range(start, start + 999).execute())
            for row in r.data:
                self.last[row["show_code"]] = (row.get("last_sold"), row.get("last_snapshot_at"))
            if len(r.data) < 1000:
                break
            start += 1000

    def save(self, shows, language):
        """Saves a batch of shows. A new reading is stored only when a show's sold count has
        changed, or it has no reading yet today (IST) - keeps the database small."""
        if DRY_RUN or not shows:
            return 0
        if not hasattr(self, "last"):
            self._load_last()
        now = datetime.now(timezone.utc)
        today_ist = now.astimezone(IST).strftime("%Y-%m-%d")

        theatres = {}
        for s in shows:
            theatres[f'D{s["cinema_id"]}'] = {"name": s["cinema"], "chain": s["chain"],
                                              "city": s["city"], "venue_code": f'D{s["cinema_id"]}'}
        t_id = {}
        items = list(theatres.values())
        for i in range(0, len(items), 500):
            for r in self.sb.table("theatres").upsert(items[i:i + 500], on_conflict="venue_code").execute().data:
                t_id[r["venue_code"]] = r["id"]

        code_of = lambda s: f'D-{s["cinema_id"]}-{s["session_id"]}-{s["show_time"][:10]}'
        changed, same = {}, {}
        for s in shows:
            code = code_of(s)
            prev_sold, prev_at = self.last.get(code, (None, None))
            prev_day = (datetime.fromisoformat(prev_at).astimezone(IST).strftime("%Y-%m-%d")
                        if prev_at else None)
            row = {"movie_id": self.movie_id, "theatre_id": t_id[f'D{s["cinema_id"]}'],
                   "show_time": s["show_time"], "format": s["format"], "language": language,
                   "show_code": code, "last_sold": s["sold"]}
            if prev_sold != s["sold"] or prev_day != today_ist:
                row["last_snapshot_at"] = now.isoformat()
                changed[code] = (row, s)
            else:
                same[code] = row

        s_id = {}
        for group in (list(r for r, _ in changed.values()), list(same.values())):
            for i in range(0, len(group), 500):
                for r in self.sb.table("shows").upsert(group[i:i + 500], on_conflict="show_code").execute().data:
                    s_id[r["show_code"]] = r["id"]

        snaps = [{"show_id": s_id[code], "seats_total": s["total"], "seats_available": s["available"],
                  "seats_sold": s["sold"], "gross": s["gross"]} for code, (_, s) in changed.items()]
        for i in range(0, len(snaps), 500):
            self.sb.table("snapshots").insert(snaps[i:i + 500]).execute()
        for code, (row, s) in changed.items():
            self.last[code] = (s["sold"], row["last_snapshot_at"])
        return len(snaps)

# ------------------------------------------------------------------
# Talking to District
# ------------------------------------------------------------------

class TokenWatcher:
    """Copies District's guest token from any API request its own pages make."""

    def __init__(self, page):
        self.token = None
        page.on("request", self._on_request)

    def _on_request(self, req):
        if "/gw/" in req.url:
            t = req.headers.get("x-guest-token")
            if t:
                self.token = t


class District:
    def __init__(self, page, watcher):
        self.page, self.watcher = page, watcher
        self.headers = dict(BASE_HEADERS)

    def refresh_token(self, city="mumbai"):
        self.watcher.token = None
        t = self.page.evaluate(FIND_TOKEN_JS)
        if not t:
            self.page.goto(MOVIE_PAGE.format(city=city), wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):
                if self.watcher.token:
                    break
                self.page.wait_for_timeout(500)
            t = self.watcher.token or self.page.evaluate(FIND_TOKEN_JS)
        self.headers = dict(BASE_HEADERS, **({"x-guest-token": t} if t else {}))
        return bool(t)

    def fetch(self, reqs):
        """Runs requests a few at a time. Returns parsed JSON ({} = nothing listed, None = failed)."""
        results = [None] * len(reqs)
        todo = list(range(len(reqs)))
        for attempt in range(3):
            if not todo:
                break
            got = []
            for i in range(0, len(todo), PARALLEL * 4):
                chunk = todo[i:i + PARALLEL * 4]
                out = self.page.evaluate(FETCH_MANY_JS, {"reqs": [reqs[j] for j in chunk],
                                                         "headers": self.headers, "parallel": PARALLEL})
                got += list(zip(chunk, out))
                time.sleep(random.uniform(*PAUSE))
            retry, need_token = [], False
            for j, r in got:
                if r["status"] == 200:
                    try:
                        results[j] = json.loads(r["text"])
                    except ValueError:
                        retry.append(j)
                elif r["status"] == 204 or (r["status"] == 400 and "not found" in r["text"].lower()):
                    results[j] = {}                       # nothing listed
                elif r["status"] == 401:
                    need_token = True; retry.append(j)
                else:
                    retry.append(j)
                    if attempt == 2:
                        print(f"    FAILED {r['status']}: {reqs[j]['url'][:90]}... {r['text'][:80]!r}")
            if need_token:
                self.refresh_token()
            todo = retry
            if todo:
                time.sleep(3 * (attempt + 1))
        return results

    def cities(self):
        r = self.fetch([{"url": CITIES_API, "method": "POST", "body": "{}",
                         "headers": {"content-type": "application/json"}}])[0]
        rows = (r or {}).get("cities") or []
        if not rows and os.path.exists("cities.json"):     # fallback: a saved copy
            with open("cities.json", encoding="utf-8") as f:
                rows = json.load(f).get("cities") or []
        return [{"key": c["city_key"], "name": c.get("city_name"), "state": c.get("state_name"),
                 "lat": c.get("city_lat"), "lon": c.get("city_long")}
                for c in rows if c.get("city_key") and c.get("city_lat") is not None]

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def url_for(code, city, lat, lon, date=None):
    return API.format(code=code, city=city, cid=CONTENT_ID, lat=lat, lon=lon,
                      date=f"&date={date}" if date else "")


def find_listings(district, known):
    """Looks for Ramayana's other language listings: collects the codes District's own
    Ramayana pages link to, then asks District which language each one is."""
    listings = dict(known)
    known_codes = set(listings.values())
    page = district.page
    for city in LANGUAGE_SCOUT_CITIES:
        try:
            page.goto(MOVIE_PAGE.format(city=city), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            html = page.content()
        except Exception:
            continue
        codes = set(re.findall(r"frmtid(?:=|%3D|\\u003d)([a-z0-9]{8,14})", html))
        codes |= set(re.findall(r'"(?:frmtid|movieFormatCode|formatCode|movieCode)"\s*:\s*"([a-z0-9]{8,14})"', html))
        new = [c for c in codes if c not in known_codes]
        if not new:
            continue
        lat_lon = EXTRA_POINTS.get(city, [(20.59, 78.96)])[0]
        replies = district.fetch([{"url": url_for(c, city, lat_lon[0], lat_lon[1])} for c in new])
        for c, data in zip(new, replies):
            known_codes.add(c)
            lang = ((data or {}).get("meta") or {}).get("movieLanguage")
            if lang and lang.strip().title() not in listings:
                listings[lang.strip().title()] = c
                print(f"  Found a new language listing: {lang.strip().title()} ({c})")
    return listings


def discover(district, cities, today, listings):
    """Asks every city, for every language, whether Ramayana is listed and on which dates."""
    active = {}
    for language, code in listings.items():
        print(f"Discovering {language} in {len(cities)} cities...")
        reqs = [{"url": url_for(code, c["key"], c["lat"], c["lon"])} for c in cities]
        results = district.fetch(reqs)
        found = {}
        for c, data in zip(cities, results):
            if not data or not language_matches(data, language):
                continue
            dates = [d for d in open_dates(data) if d >= today]
            if not dates:      # no date strip - fall back to the dates of the shows themselves
                page = data.get("pageData") or {}
                dates = sorted({datetime.fromisoformat(s["showTime"]).replace(tzinfo=timezone.utc)
                                .astimezone(IST).strftime("%Y-%m-%d")
                                for cin in (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or [])
                                for s in cin.get("sessions") or [] if s.get("showTime")})
                dates = [d for d in dates if d >= today]
            if dates:
                found[c["key"]] = {"lat": c["lat"], "lon": c["lon"], "state": c["state"], "dates": dates}
        print(f"  {language}: listed in {len(found)} cities\n")
        active[language] = found
    return active


def ensure_active(district, store, today, state_box):
    """Returns (active, listings), re-discovering cities/languages when the saved list is old."""
    state = state_box.get("state")
    if state is None:
        state = store.load_state()
    age = None
    if state.get("discovered_at"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(state["discovered_at"])
    listings = {**LISTINGS, **(state.get("listings") or {})}
    if ONLY_CITIES:
        if "active" not in state_box:
            cities = [c for c in district.cities() if c["key"] in ONLY_CITIES]
            state_box["active"] = discover(district, cities, today, listings)
        return state_box["active"], listings
    if age is None or age > timedelta(hours=REDISCOVER_HOURS) or not set(LISTINGS) <= set(state.get("active", {})):
        cities = district.cities()
        print(f"District lists {len(cities)} cities\n")
        if not cities:
            raise RuntimeError("Couldn't get District's city list.")
        print("Checking for new languages...")
        listings = find_listings(district, listings)
        print(f"Languages: {', '.join(listings)}\n")
        active = discover(district, cities, today, listings)
        state = {"discovered_at": datetime.now(timezone.utc).isoformat(), "active": active,
                 "listings": listings}
        store.save_state(state)
    elif not state_box.get("announced"):
        hrs = age.total_seconds() / 3600
        print(f"Using the active-city list from {hrs:.1f} hours ago "
              f"({', '.join(f'{lang}: {len(v)} cities' for lang, v in state['active'].items())})\n")
        state_box["announced"] = True
    state_box["state"] = state
    return state["active"], listings


def is_hot(date, today):
    tomorrow = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return date <= HOT_UNTIL or date <= tomorrow


def track_pass(district, store, active, listings, today, include_far):
    """One tracking pass. Returns (by_date, by_city, problems)."""
    seen = set()
    by_date, by_city = {}, {}
    problems = 0
    for language, cities in active.items():
        code = listings.get(language)
        if not code or not cities:
            continue
        reqs, meta = [], []
        for key, c in sorted(cities.items(), key=lambda kv: (kv[0] not in EXTRA_POINTS, kv[0])):
            points = EXTRA_POINTS.get(key) or [(c["lat"], c["lon"])]
            for date in c["dates"]:
                if date < today or (not include_far and not is_hot(date, today)):
                    continue
                for lat, lon in points:
                    reqs.append({"url": url_for(code, key, lat, lon, date)})
                    meta.append((key, date))
        if not reqs:
            continue
        print(f"Tracking {language}: {len(cities)} cities, {len(reqs)} requests...")
        results = district.fetch(reqs)

        batch = []
        for (key, date), data in zip(meta, results):
            if data is None:
                problems += 1
                continue
            if not language_matches(data, language):
                continue   # District sent another language's listing instead
            for s in parse_showtimes(data, date):
                k = (s["cinema_id"], s["session_id"], s["show_time"])
                if k in seen or (NATIONAL_ONLY and s["chain"] not in ("PVR", "INOX", "CINEPOLIS")):
                    continue
                seen.add(k)
                batch.append(s)
                d = by_date.setdefault(date, {"shows": 0, "sold": 0, "total": 0, "gross": 0.0})
                d["shows"] += 1; d["sold"] += s["sold"]; d["total"] += s["total"]; d["gross"] += s["gross"]
                cc = by_city.setdefault((language, s["city"] or key),
                                        {"shows": 0, "sold": 0, "total": 0, "gross": 0.0, "cinemas": set()})
                cc["shows"] += 1; cc["sold"] += s["sold"]; cc["total"] += s["total"]
                cc["gross"] += s["gross"]; cc["cinemas"].add(s["cinema_id"])
        try:
            n_new = store.save(batch, language)
            print(f"  {len(batch):,} {language} shows found, {n_new or 0:,} new readings saved\n")
        except Exception as e:
            problems += 1
            print(f"  could not save to Supabase: {e}\n")
    return by_date, by_city, problems


def print_summary(by_date, by_city, problems, full):
    if by_city:
        print("Top cities" + (" (all dates):" if full else " (opening-weekend dates):"))
        top = sorted(by_city.items(), key=lambda kv: -kv[1]["sold"])[:25]
        for (language, key), v in top:
            occ = f"{v['sold'] / v['total'] * 100:5.1f}%" if v["total"] else "    -"
            print(f"  {key[:22]:22} {language:8} {len(v['cinemas']):4} cinemas {v['shows']:6,} shows "
                  f"{v['sold']:8,} sold {occ}  Rs {v['gross']:>14,.0f}")
        print("\nBy show date (all cities, all languages):")
        for date in sorted(by_date):
            v = by_date[date]
            occ = f"{v['sold'] / v['total'] * 100:5.1f}%" if v["total"] else "    -"
            print(f"  {date}  {v['shows']:7,} shows  {v['sold']:9,}/{v['total']:<10,} {occ}  Rs {v['gross']:>15,.0f}")
        t = {k: sum(v[k] for v in by_date.values()) for k in ("shows", "sold", "total", "gross")}
        print(f"\nDONE: {len(by_city)} city/language listings | {t['shows']:,} shows | "
              f"{t['sold']:,} / {t['total']:,} seats sold | approx Rs {t['gross']:,.0f} gross")
    else:
        print("\nDONE: no seat data collected.")
    if problems:
        print(f"{problems} requests failed - see FAILED lines above.")


def main():
    store = Store()
    proxy = None
    if os.getenv("PROXY_SERVER"):
        proxy = {"server": os.getenv("PROXY_SERVER"),
                 "username": os.getenv("PROXY_USERNAME") or None,
                 "password": os.getenv("PROXY_PASSWORD") or None}
        print(f"Using proxy {proxy['server']}\n")

    started = time.monotonic()
    deadline = started + LOOP_MINUTES * 60
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, proxy=proxy)
        context = browser.new_context(locale="en-IN", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        watcher = TokenWatcher(page)
        page.goto("https://www.district.in/movies/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        title = page.title()
        print(f"District home page: {title!r}")
        if "access denied" in title.lower():
            browser.close()
            sys.exit("District is blocking this connection (Akamai 'Access Denied'). "
                     "Use a residential proxy (PROXY_SERVER secrets) or run from a home connection.")
        district = District(page, watcher)
        print("Got District's guest token\n" if district.refresh_token() else "No guest token found\n")
        try:
            context.clear_cookies(name="location")   # our own points decide the location, not a saved one
        except TypeError:
            pass

        state_box = {}
        last_far = None
        n = 0
        while True:
            n += 1
            pass_start = time.monotonic()
            today = datetime.now(IST).strftime("%Y-%m-%d")
            include_far = last_far is None or (pass_start - last_far) >= FAR_EVERY_MINUTES * 60
            if LOOP_MINUTES:
                stamp = datetime.now(IST).strftime("%H:%M IST")
                print(f"===== Pass {n} at {stamp} - " +
                      ("all dates" if include_far else "opening-weekend dates") + " =====")
            try:
                active, listings = ensure_active(district, store, today, state_box)
                by_date, by_city, problems = track_pass(district, store, active, listings, today, include_far)
                print_summary(by_date, by_city, problems, include_far)
                if include_far:
                    last_far = pass_start
            except Exception as e:
                print(f"Pass {n} failed: {e}")
            if not LOOP_MINUTES:
                break
            next_start = pass_start + INTERVAL_MINUTES * 60
            if next_start >= deadline:
                print(f"\nStopping after {n} passes (the next run takes over).")
                break
            wait = next_start - time.monotonic()
            if wait > 0:
                print(f"\nNext pass in {wait / 60:.1f} min\n")
                time.sleep(wait)

        browser.close()


if __name__ == "__main__":
    main()
