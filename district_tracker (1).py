"""
Ramayana: Part 1 - advance booking tracker using District (district.in)

For every city and date it asks District for the full showtimes list. That list already
includes, for every show, the seats available and total seats in each section and their
prices - so no seat maps need opening. One request covers a whole city for one date.

It opens district.in once in a real browser and makes the requests from inside that page,
the same way the website itself does.

Run:  python district_tracker.py
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ------------------------------------------------------------------
# SETTINGS - change these freely
# ------------------------------------------------------------------
MOVIE_TITLE = "Ramayana: Part 1"      # must match the row in your movies table
CONTENT_ID = "176276"                 # Ramayana on District
LISTINGS = {                          # language -> District listing code (add others when they open)
    "Hindi": "rtb7dmwvuo",
}
# District city name (as in its URLs) -> a point in the city (District asks for one)
CITIES = {
    "mumbai":    (19.0760, 72.8777),
    "gurgaon":   (28.4595, 77.0266),   # covers Gurgaon + much of Delhi + Faridabad
    "delhi":     (28.6139, 77.2090),
    "noida":     (28.5355, 77.3910),
    "bengaluru": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "pune":      (18.5204, 73.8567),
    "kolkata":   (22.5726, 88.3639),
    "chennai":   (13.0827, 80.2707),
    "ahmedabad": (23.0225, 72.5714),
}
if os.getenv("CITIES"):
    CITIES = {c: CITIES.get(c, (0, 0)) for c in os.getenv("CITIES").split(",") if c}
DATES = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
if os.getenv("DATES"):
    DATES = [d.strip() for d in os.getenv("DATES").split(",") if d.strip()]
NATIONAL_ONLY = os.getenv("NATIONAL_ONLY", "false").lower() == "true"   # PVR, INOX, Cinepolis only
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
DELAY_SECONDS = (3, 6)                # polite pause between requests

IST = ZoneInfo("Asia/Kolkata")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DRY_RUN = not (SUPABASE_URL and SUPABASE_KEY)

API = ("https://www.district.in/gw/consumer/movies/v5/movie?version=3&site_id=1&channel=web"
       "&child_site_id=1&platform=district&movieCode={code}&city_key={city}&content_id={cid}"
       "&date={date}&latitude={lat}&longitude={lon}&cinemaOrderLogic=3")

# Runs inside the District page, so it uses the page's own cookies and connection
FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json'}});
    const text = await r.text();
    return {status: r.status, text};
  } catch (e) { return {status: 0, text: String(e)}; }
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
    """Seats and gross for one show, from District's per-section numbers."""
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
    """All shows on `date` from one showtimes response -> list of dicts."""
    page = data.get("pageData") or {}
    cinemas = (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or [])
    shows = []
    for c in cinemas:
        info = c.get("cinemaInfo") or {}
        for s in c.get("sessions") or []:
            # District gives show times in UTC
            utc = datetime.fromisoformat(s["showTime"]).replace(tzinfo=timezone.utc)
            if utc.astimezone(IST).strftime("%Y-%m-%d") != date:
                continue
            counts = show_counts(s)
            if counts["total"] == 0:
                continue
            shows.append({
                "cinema_id": c.get("id"),
                "cinema": info.get("name") or info.get("label") or str(c.get("id")),
                "city": info.get("city") or "",
                "chain": chain_of(info),
                "session_id": str(s.get("sid")),
                "show_time": utc.isoformat(),
                "format": (s.get("scrnFmt") or "2D").upper().replace("-", " "),
                **counts,
            })
    return shows

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

    def save(self, shows, language):
        """Saves a whole city/date in three batch requests."""
        if DRY_RUN or not shows:
            return
        theatres = {}
        for s in shows:
            theatres[f'D{s["cinema_id"]}'] = {"name": s["cinema"], "chain": s["chain"],
                                              "city": s["city"], "venue_code": f'D{s["cinema_id"]}'}
        t_rows = self.sb.table("theatres").upsert(list(theatres.values()), on_conflict="venue_code").execute().data
        t_id = {r["venue_code"]: r["id"] for r in t_rows}

        show_rows = {}
        for s in shows:
            code = f'D-{s["cinema_id"]}-{s["session_id"]}-{s["show_time"][:10]}'
            show_rows[code] = {"movie_id": self.movie_id, "theatre_id": t_id[f'D{s["cinema_id"]}'],
                               "show_time": s["show_time"], "format": s["format"],
                               "language": language, "show_code": code}
        s_rows = self.sb.table("shows").upsert(list(show_rows.values()), on_conflict="show_code").execute().data
        s_id = {r["show_code"]: r["id"] for r in s_rows}

        snaps = [{"show_id": s_id[f'D-{s["cinema_id"]}-{s["session_id"]}-{s["show_time"][:10]}'],
                  "seats_total": s["total"], "seats_available": s["available"],
                  "seats_sold": s["sold"], "gross": s["gross"]} for s in shows]
        for i in range(0, len(snaps), 500):
            self.sb.table("snapshots").insert(snaps[i:i + 500]).execute()

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    store = Store()
    grand = {"shows": 0, "sold": 0, "total": 0, "gross": 0.0}
    seen = set()          # the same cinema can appear for two nearby cities - count it once
    problems = 0
    today = datetime.now(IST).strftime("%Y-%m-%d")
    dates = [d for d in DATES if d >= today]

    proxy = None
    if os.getenv("PROXY_SERVER"):
        proxy = {"server": os.getenv("PROXY_SERVER"),
                 "username": os.getenv("PROXY_USERNAME") or None,
                 "password": os.getenv("PROXY_PASSWORD") or None}
        print(f"Using proxy {proxy['server']}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, proxy=proxy)
        context = browser.new_context(locale="en-IN", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.goto("https://www.district.in/movies/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        title = page.title()
        print(f"District home page: {title!r}\n")

        for language, code in LISTINGS.items():
            for city, (lat, lon) in CITIES.items():
                for date in dates:
                    url = API.format(code=code, city=city, cid=CONTENT_ID, date=date, lat=lat, lon=lon)
                    data, error = None, None
                    for attempt in (1, 2, 3):
                        r = page.evaluate(FETCH_JS, url)
                        if r["status"] == 200:
                            try:
                                data = json.loads(r["text"])
                                break
                            except ValueError:
                                error = "reply was not JSON: " + r["text"][:120].replace("\n", " ")
                        else:
                            error = f"HTTP {r['status']}: " + r["text"][:120].replace("\n", " ")
                        time.sleep(5 * attempt)

                    label = f"{city:10} {date} {language}"
                    if data is None:
                        problems += 1
                        print(f"  {label}  FAILED - {error}")
                        continue

                    shows = [s for s in parse_showtimes(data, date)
                             if (s["cinema_id"], s["session_id"], s["show_time"]) not in seen
                             and (not NATIONAL_ONLY or s["chain"] in ("PVR", "INOX", "CINEPOLIS"))]
                    seen.update((s["cinema_id"], s["session_id"], s["show_time"]) for s in shows)

                    sold = sum(s["sold"] for s in shows); total = sum(s["total"] for s in shows)
                    gross = sum(s["gross"] for s in shows)
                    occ = f"{sold / total * 100:5.1f}%" if total else "    -"
                    cinemas = len({s["cinema_id"] for s in shows})
                    print(f"  {label}  {len(shows):4} shows  {cinemas:3} cinemas  "
                          f"{sold:6,}/{total:<7,} {occ}  Rs {gross:>13,.0f}")

                    try:
                        store.save(shows, language)
                    except Exception as e:
                        problems += 1
                        print(f"      could not save to Supabase: {e}")

                    grand["shows"] += len(shows); grand["sold"] += sold
                    grand["total"] += total; grand["gross"] += gross
                    time.sleep(random.uniform(*DELAY_SECONDS))

        browser.close()

    if grand["total"]:
        print(f"\nDONE: {grand['shows']:,} shows | {grand['sold']:,} / {grand['total']:,} seats sold "
              f"({grand['sold'] / grand['total'] * 100:.1f}%) | approx Rs {grand['gross']:,.0f} gross")
    else:
        print("\nDONE: no seat data collected.")
    if problems:
        print(f"{problems} city/date requests had problems - see FAILED lines above.")


if __name__ == "__main__":
    main()
