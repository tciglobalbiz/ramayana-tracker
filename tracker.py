"""
Ramayana: Part 1 - national chain advance booking tracker (BookMyShow)

What it does, every time you run it:
  1. Opens the Ramayana showtimes page for each city/date below
  2. Collects every PVR / INOX / Cinepolis show (theatre code + show ID)
  3. Opens each show's seat map and counts sold vs available seats
  4. Saves the numbers to Supabase (or just prints them if no keys are set)

Run:  python tracker.py
"""

import os
import random
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ------------------------------------------------------------------
# SETTINGS - change these freely
# ------------------------------------------------------------------
EVENT_CODE = "ET00451914"              # Ramayana on BookMyShow
MOVIE_SLUG = "namit-malhotras-ramayana"
MOVIE_TITLE = "Ramayana: Part 1"       # must match the row in your movies table
LANGUAGES = ["Hindi"]                  # add "Tamil", "Telugu", "Kannada", "Malayalam" once they open for booking
CITIES = [                             # BookMyShow city names, as they appear in the URL
    "mumbai", "national-capital-region-ncr", "bengaluru", "hyderabad",
    "pune", "kolkata", "chennai", "ahmedabad",
]
# GitHub runs one city per job (set by the workflow). Locally you can do the same:
#   set CITIES=mumbai   (Windows)   then   py -3.13 tracker.py
if os.getenv("CITIES"):
    CITIES = [c.strip() for c in os.getenv("CITIES").split(",") if c.strip()]
DATES = ["20261104", "20261105", "20261106", "20261107", "20261108"]  # paid previews + opening weekend
CHAINS = ["PVR", "INOX", "CINEPOLIS"]  # national chains only

MAX_SHOWS = int(os.getenv("MAX_SHOWS", "15"))   # shows checked per city per date
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
DELAY_SECONDS = (4, 8)                 # polite random pause between shows

IST = ZoneInfo("Asia/Kolkata")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DRY_RUN = not (SUPABASE_URL and SUPABASE_KEY)

# ------------------------------------------------------------------
# JavaScript that runs inside the BookMyShow page
# ------------------------------------------------------------------

# Reads the showtimes data bundle (window.__INITIAL_STATE__) and lists every show
EXTRACT_SHOWS_JS = r"""
async ({url, slug}) => {
  // Use the page Chrome already loaded; only fetch it again if the data isn't in it
  let t = document.documentElement.outerHTML;
  if (t.indexOf('window.__INITIAL_STATE__') < 0) {
    try { t = await (await fetch(url, {credentials: 'include'})).text(); } catch (e) {}
  }
  let i = t.indexOf('window.__INITIAL_STATE__');
  if (i < 0) return {error: 'showtimes data not found on page'};
  i = t.indexOf('{', i);
  let depth = 0, inStr = false, esc = false, j = i;
  for (; j < t.length; j++) {
    const c = t[j];
    if (inStr) { if (esc) esc = false; else if (c === '\\') esc = true; else if (c === '"') inStr = false; continue; }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) break; }
  }
  let state;
  try { state = JSON.parse(t.slice(i, j + 1)); } catch (e) { return {error: 'could not read data: ' + e.message}; }

  // Format labels, most specific first
  const FORMATS = ['2D SCREEN X','3D SCREEN X','4DX 3D','MX4D 3D','IMAX 3D','IMAX 2D','4DX','MX4D','IMAX','SCREEN X','3D','2D'];
  const norm = (s) => String(s).toUpperCase().replace(/SCREENX/g, 'SCREEN X').replace(/\s+/g, ' ').trim();
  const asFormat = (s) => { const n = norm(s); return FORMATS.includes(n) ? n : null; };
  const isEt = (s) => /^ET\d{8}$/.test(String(s));

  // Look inside one show's own data for a format label / event code
  const scan = (o, found = {fmt: null, et: null}) => {
    if (typeof o === 'string') {
      if (!found.fmt && asFormat(o)) found.fmt = asFormat(o);
      if (!found.et && isEt(o)) found.et = o;
      if (!found.et) { const m = o.match(/seat-layout\/(ET\d{8})\//i); if (m) found.et = m[1]; }
    } else if (o && typeof o === 'object') {
      for (const k in o) if (!/uuid/i.test(k)) scan(o[k], found);
    }
    return found;
  };

  const shows = [], seen = new Set();
  let venue = null, curFormat = null, curEt = null;
  const walk = (o) => {
    if (typeof o === 'string') {          // headings like "IMAX 3D" above a row of showtimes
      if (asFormat(o)) curFormat = asFormat(o);
      if (isEt(o)) curEt = o;
      return;
    }
    if (Array.isArray(o)) { for (const x of o) walk(x); return; }
    if (!o || typeof o !== 'object') return;
    const ad = o.additionalData;
    if (ad && ad.venueCode && ad.venueName && (!venue || venue.code !== ad.venueCode)) {
      venue = {code: ad.venueCode, name: ad.venueName}; curFormat = null; curEt = null;
    }
    if (ad && ad.sessionId && ad.showDateTime && venue) {
      const key = venue.code + '-' + ad.sessionId;
      if (!seen.has(key)) {
        seen.add(key);
        const own = scan(o);
        shows.push({venueCode: venue.code, venueName: venue.name, sessionId: String(ad.sessionId),
                    showDateTime: String(ad.showDateTime), availStatus: ad.availStatus ?? null,
                    format: own.fmt || curFormat || null, eventCode: own.et || curEt || null});
      }
    }
    for (const k in o) if (!/uuid/i.test(k)) walk(o[k]);
  };
  walk(state);

  // Learn which event code means which format from shows that have both, then fill the gaps
  const etFormat = {};
  for (const s of shows) if (s.eventCode && s.format && !etFormat[s.eventCode]) etFormat[s.eventCode] = s.format;
  for (const s of shows) if (!s.format) s.format = (s.eventCode && etFormat[s.eventCode]) || '2D';

  // Ramayana's own listings only, e.g. /movies/namit-malhotras-ramayana-3dhindi/ET00493076
  const listings = {};
  for (const m of t.matchAll(new RegExp('/movies/(' + slug + '[a-z0-9-]*)/(ET\\d{8})', 'g'))) listings[m[2]] = m[1];
  return {shows, codes: Object.keys(listings), listings, etFormat};
}
"""

# Counts seats on the Konva seat map and estimates gross from the price headings
COUNT_SEATS_JS = r"""
() => {
  if (!window.Konva || !Konva.stages.length) return null;
  const s = Konva.stages[Konva.stages.length - 1];   // newest map, never a leftover one
  const headers = s.find('Text')
    .map(t => ({y: t.getClientRect().y, m: String(t.text()).match(/₹\s*([\d,]+)/)}))
    .filter(h => h.m)
    .map(h => ({y: h.y, price: Number(h.m[1].replace(/,/g, ''))}))
    .sort((a, b) => a.y - b.y);
  const priceAt = (y) => { let p = 0; for (const h of headers) { if (h.y <= y) p = h.price; else break; } return p; };

  let total = 0, sold = 0, available = 0, other = 0, gross = 0, maxGross = 0;
  const colors = {};
  const add = (node, isSold) => {
    const price = priceAt(node.getClientRect().y);
    total++; maxGross += price;
    if (isSold) { sold++; gross += price; } else { available++; }
  };
  for (const r of s.find('Rect')) {
    const fill = String(r.fill() || '').toUpperCase();
    const stroke = String(r.stroke() || '').toUpperCase();
    if (fill === '#E5E5E5') add(r, true);                            // sold (grey)
    else if (fill === '#FFFFFF' && stroke === '#1FAD3E') add(r, false); // available (green outline)
    else if (fill === '#1EA83C') add(r, false);                      // selected (counts as available)
    else { other++; const k = fill + '/' + stroke; colors[k] = (colors[k] || 0) + 1; }
  }
  for (const im of s.find('Image')) if (im.width() < 30) add(im, false); // yellow bestseller seats
  return {total, sold, available, gross, maxGross, other, colors};
}
"""

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def chain_of(venue_name):
    """'PVR: Haseen, Bhiwandi' -> 'PVR'. Returns None for non-national chains."""
    plain = unicodedata.normalize("NFKD", venue_name).encode("ascii", "ignore").decode().upper()
    prefix = plain.split(":")[0].strip()
    for chain in CHAINS:
        if prefix.startswith(chain):
            return chain
    return None


def pick_sample(shows, n):
    """Same spread of shows every run, so the trend lines are comparable."""
    shows = sorted(shows, key=lambda s: (s["venueCode"], s["showDateTime"]))
    if len(shows) <= n:
        return shows
    step = len(shows) / n
    return [shows[int(k * step)] for k in range(n)]


def to_ist_iso(show_dt):
    return datetime.strptime(show_dt, "%Y%m%d%H%M").replace(tzinfo=IST).isoformat()


class Store:
    """Saves to Supabase, or prints if no keys are set."""

    def __init__(self):
        self.sb = None
        self.movie_id = None
        if DRY_RUN:
            print("No Supabase keys found - TEST MODE, results are printed only.\n")
            return
        from supabase import create_client
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = self.sb.table("movies").select("id").eq("title", MOVIE_TITLE).limit(1).execute()
        if not res.data:
            sys.exit(f"Movie '{MOVIE_TITLE}' not found in the movies table.")
        self.movie_id = res.data[0]["id"]

    def save(self, city, language, show, chain, counts):
        if DRY_RUN:
            return
        theatre = self.sb.table("theatres").upsert(
            {"name": show["venueName"], "chain": chain, "city": city.title(), "venue_code": show["venueCode"]},
            on_conflict="venue_code",
        ).execute().data[0]
        show_row = self.sb.table("shows").upsert(
            {"movie_id": self.movie_id, "theatre_id": theatre["id"], "show_time": to_ist_iso(show["showDateTime"]),
             "format": show["format"], "language": language,
             "show_code": f'{show["venueCode"]}-{show["sessionId"]}'},
            on_conflict="show_code",
        ).execute().data[0]
        self.sb.table("snapshots").insert(
            {"show_id": show_row["id"], "seats_total": counts["total"], "seats_available": counts["available"],
             "seats_sold": counts["sold"], "gross": counts["gross"]}
        ).execute()

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

VENUE_CODE_MEMORY = {}   # theatre -> event code that worked last time


def count_show(page, city, date, show, listings):
    """Returns (counts, problem, code_used). Tries the likeliest Ramayana listing first, then the others."""
    problem = "unknown"
    three_d = [c for c, s in listings.items() if "3d" in s]
    others = [c for c in listings if c not in three_d]
    likely = (three_d + [EVENT_CODE] + others) if "3D" in show["format"] else ([EVENT_CODE] + others + three_d)
    first = [show.get("eventCode"), VENUE_CODE_MEMORY.get((show["venueCode"], show["format"]))]
    codes = [c for c in dict.fromkeys(first + likely) if c]
    for code in codes:
        url = (f"https://in.bookmyshow.com/movies/{city}/seat-layout/"
               f"{code}/{show['venueCode']}/{show['sessionId']}/{date}")
        ready = ("() => window.Konva && Konva.stages.length && "
                 "Konva.stages[Konva.stages.length - 1].find('Rect').length > 20")
        ready_or_error = (f"() => ({ready[6:]})"
                          " || (document.body && document.body.innerText.includes('Something is not right'))")
        try:
            # Blank the tab first so the previous show's seat map can never be counted again
            page.goto("about:blank")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if show["sessionId"] not in page.url:
                problem = "BookMyShow redirected to a different page"
                continue
            try:
                page.wait_for_function(ready_or_error, timeout=10000)
                if page.evaluate("() => document.body.innerText.includes('Something is not right')"):
                    problem = "wrong event code for this show"
                    continue   # BookMyShow error page - try the next code straight away
            except PWTimeout:
                # Some shows only draw the map after the "How many seats?" pop-up is closed.
                # "Select Seats" just closes it - it does not book or hold any seat.
                button = page.get_by_text("Select Seats", exact=True)
                if button.count():
                    button.first.click()
                page.wait_for_function(ready, timeout=20000)
            page.wait_for_timeout(1500)  # let the map finish drawing
            counts = page.evaluate(COUNT_SEATS_JS)
            if counts and counts["total"] > 0:
                VENUE_CODE_MEMORY[(show["venueCode"], show["format"])] = code
                return counts, None, code
            problem = f"seat colours not recognised {counts.get('colors') if counts else ''}"
        except PWTimeout:
            problem = f"seat map did not load with any of {len(codes)} event codes"
        page.wait_for_timeout(2000)
    return None, problem, None


def main():
    store = Store()
    grand = {"shows": 0, "sold": 0, "total": 0, "gross": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(locale="en-IN", viewport={"width": 1366, "height": 900})
        page = context.new_page()

        done = set()   # never count the same show twice in one run
        last_sig = None   # previous show's numbers, to catch a stale seat map
        today = datetime.now(IST).strftime("%Y%m%d")
        dates = [d for d in DATES if d >= today]   # skip dates that are already over
        jobs = [(c, d, l) for c in CITIES for d in dates for l in LANGUAGES]
        for city, date, language in jobs:
            if True:
                list_url = (f"https://in.bookmyshow.com/movies/{city}/{MOVIE_SLUG}/buytickets/"
                            f"{EVENT_CODE}/{date}?etCodes=*&language={language}&refEventCode={EVENT_CODE}")
                print(f"=== {city.title()} | {date} | {language} (all formats) ===")
                result = {"error": "not tried"}
                for attempt in range(1, 4):
                    try:
                        page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3000 * attempt)
                        result = page.evaluate(EXTRACT_SHOWS_JS, {"url": list_url, "slug": MOVIE_SLUG})
                    except Exception as e:
                        result = {"error": f"page failed to load ({type(e).__name__})"}
                    if not result.get("error"):
                        break
                    print(f"  attempt {attempt}: {result['error']}")
                    page.wait_for_timeout(random.randint(10, 20) * 1000)
                if result.get("error"):
                    # Record what BookMyShow actually showed, to tell a block apart from a real change
                    try:
                        body = " ".join(page.inner_text("body").split())[:300]
                        print(f"  Page title: {page.title()!r}")
                        print(f"  Page says: {body!r}")
                        os.makedirs("debug", exist_ok=True)
                        page.screenshot(path=f"debug/{city}-{date}.png")
                    except Exception as e:
                        print(f"  (could not capture page: {e})")
                    print("  Problem reading showtimes - skipped\n")
                    continue

                # BookMyShow jumps to the next open date when a date has no shows - keep only the date we asked for
                on_date = [s for s in result["shows"] if s["showDateTime"].startswith(date)]
                national = [s for s in on_date if chain_of(s["venueName"])
                            and f'{s["venueCode"]}-{s["sessionId"]}' not in done]
                done.update(f'{s["venueCode"]}-{s["sessionId"]}' for s in national)
                formats = {}
                for s in national:
                    formats[s["format"]] = formats.get(s["format"], 0) + 1
                print(f"  {len(on_date)} shows found, {len(national)} at national chains  {formats or ''}")
                if not national:
                    print()
                    continue
                sample = pick_sample(national, MAX_SHOWS)
                print(f"  Checking {len(sample)} shows...\n")

                for n, show in enumerate(sample, 1):
                    chain = chain_of(show["venueName"])
                    listings = result.get("listings", {})
                    counts, problem, used = count_show(page, city, date, show, listings)
                    if counts:
                        sig = (show["venueCode"], counts["sold"], counts["total"], counts["gross"])
                        if sig == last_sig:
                            # Identical to the previous show at the same theatre - almost always a
                            # leftover map. Wait and read it once more before trusting it.
                            page.wait_for_timeout(5000)
                            counts, problem, used = count_show(page, city, date, show, listings)
                            if counts and (show["venueCode"], counts["sold"], counts["total"],
                                           counts["gross"]) == last_sig:
                                print("      (same numbers as the previous show after a re-check - kept)")
                        if counts:
                            last_sig = (show["venueCode"], counts["sold"], counts["total"], counts["gross"])
                    if used:
                        show["eventCode"] = used
                        # Opened only with the 3D listing -> it's a 3D show (e.g. "IMAX" -> "IMAX 3D")
                        if "3d" in listings.get(used, "") and "3D" not in show["format"]:
                            show["format"] = "3D" if show["format"] == "2D" else show["format"] + " 3D"
                    label = f'{show["venueName"][:34]:34} {show["format"]:11} {show["showDateTime"][-4:]}'
                    if not counts:
                        print(f"  [{n}/{len(sample)}] {label}  could not read seat map - {problem}")
                    else:
                        occ = counts["sold"] / counts["total"] * 100
                        print(f"  [{n}/{len(sample)}] {label}  {counts['sold']:>4}/{counts['total']:<4} "
                              f"{occ:5.1f}%  Rs {counts['gross']:>8,.0f}")
                        store.save(city, language, show, chain, counts)
                        grand["shows"] += 1
                        for k in ("sold", "total", "gross"):
                            grand[k] += counts[k]
                    time.sleep(random.uniform(*DELAY_SECONDS))

        browser.close()

    if grand["total"]:
        print(f"\nDONE: {grand['shows']} shows | {grand['sold']:,} / {grand['total']:,} seats sold "
              f"({grand['sold'] / grand['total'] * 100:.1f}%) | approx Rs {grand['gross']:,.0f} gross (sampled shows only)")
    else:
        print("\nDONE: no seat data collected.")


if __name__ == "__main__":
    main()
