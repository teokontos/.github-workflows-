#!/usr/bin/env python3
"""
Collects today's high/low temperature, rainfall and peak wind gust from several
weather-station sources (Wunderground, Meteoclub, Penteli/NOA, Meteociel,
Weathercloud, IonianWeather) and writes:
 
    data/extract/results_<YYYY-MM-DD>_<HHMM>.txt   human-readable table
    data/extract/results_<YYYY-MM-DD>_<HHMM>.csv   machine-readable, one row per station
 
Requirements: Python 3.9+, requests, beautifulsoup4, selenium>=4.6 (it downloads
the matching chromedriver itself, so webdriver-manager is no longer needed),
Chrome/Chromium.  On slim Docker images / Windows also: pip install tzdata
"""
from __future__ import annotations
 
import csv
import logging
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
 
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry
 
log = logging.getLogger("weather")
 
# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/Athens")  # "today" for the stations is local time, not CI/UTC time
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "extract"
 
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30    # seconds, plain HTTP requests
PAGE_TIMEOUT = 30    # seconds to wait for the FIRST element of a JS-rendered page
FIELD_TIMEOUT = 5    # seconds for the remaining elements once the page has rendered
POLITE_DELAY = 1.0   # seconds between requests to the same site
 
WUNDERGROUND_STATIONS = {
    "IKERKIRA2": "Arillas", "IAVLIO1": "Avliotes", "IPERIT4": "Acharavi",
    "IKAROU2": "Gialos Karousadon", "ILOUTS1": "Loutses1",
    "ILOUTS2": "Loutses Anapaftiria", "IKASSI2": "Kassiopi", "ISPART11": "Spartylas",
    "ISINIE3": "Sinies Porta", "INISSA6": "Old Sinies", "IYANNA5": "Ropa",
    "ICORFU29": "Potamos", "ICORFU22": "Laiki Agora", "ICORFU20": "Kentro Kofineta",
    "ICORFU9": "1st Epal", "ICORFU33": "Garitsa", "ICORFU28": "Koulines",
    "IKOBIT2": "Kobitsi", "IGASTO3": "Perama", "IKALAF4": "Kothoniki",
    "ISTAVR23": "Stavros", "ICORFU8": "Agios Georgios Argyr", "ICHLOM1": "Chlomos",
    "IARGYR6": "Perivoli", "ISAYAD1": "Sagiada", "IIGOUM1": "Igoumenitsa",
    "IU0389U02": "Filothei Thesprot",
}
 
VALANEIO_URL = "https://valanio-kerkyra.meteoclub.gr/"
 
# friendly name -> URL slug on penteli.meteo.gr
PENTELI_STATIONS = {
    "Gouvia": "kerkyra",
    "Paxoi": "paxoi",
    "Petaleia": "petalia",
    "AcharaviEAA": "acharavi",
    "MavroudiThesprot": "igoumenitsa",
}
 
METEOCIEL_URL = "https://www.meteociel.fr/temps-reel/obs_villes.php?code2=16641"
METEOCIEL_COLUMNS = {  # French column header -> Reading field
    "Température Maxi": "high",
    "Température Mini": "low",
    "Rafale maxi": "gust",
    "Précipitations": "rain",
}
 
WEATHERCLOUD_STATIONS = {
    "d7463552240": "AgiosPanteleimonas",
    "d4862620509": "Kassiopi - WC",
    "d0774314531": "Agni",
    "d7746504386": "Nissaki",
    "d7550631437": "Ypsos",
    "d6992125691": "Potamos - Weathercloud",
    "d6245085291": "Kothoniki - WC",
    "d1871029033": "Perama - Weathercloud",
    "d1594180981": "Gastouri",
    "d2603547554": "Milia Kynopiaston",
    "d5203070705": "Agioi Deka",
    "d4591805891": "Petriti",
    "d1066173634": "Vlachopoulatika Paxos",
    "d3332581754": "GraikoxoriThesprot",
    "d0228718460": "FiliatesThesprot",
}
# NOTE: "rain-min-day" looks odd for a rain gauge (there is no daily *minimum* rain).
# Your old comment said it was changed to a "rain-day" ID — please verify against the page.
WEATHERCLOUD_GAUGES = {
    "high": "gauge-temp-max-day",
    "low": "gauge-temp-min-day",
    "rain": "gauge-rain-min-day",
}
CONSENT_XPATH = (
    "//button[contains(., 'onsent')] | //button[contains(., 'gree')] "
    "| //button[contains(., 'ccept')]"
)
 
IONIAN_URL = "https://ionianweather.gr/stations/stas.html"
IONIAN_TARGET_CODES = {"CRF-1", "CRF-2", "CRF-3", "CRF-4", "PAX-1"}
 
# Plausibility limits: catch parser bugs (lost minus sign, wrong unit, wrong cell...)
FIELDS = ("high", "low", "rain", "gust")
LIMITS = {"high": (-30, 55), "low": (-30, 55), "rain": (0, 500), "gust": (0, 300)}
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Data model & parsing helpers
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Reading:
    """One station's daily summary, always in °C / mm / km/h."""
    source: str
    station: str
    station_id: str = ""
    status: str = "OK"          # OK | Incomplete | Suspect | Offline | No data | Error
    high: float | None = None   # °C
    low: float | None = None    # °C
    rain: float | None = None   # mm
    gust: float | None = None   # km/h
    error: str = ""
 
 
_NUMBER = r"-?\d+(?:[.,]\d+)?"
 
 
def to_float(text) -> float | None:
    """First number in *text* (accepts '-3,5', '12.4 °C', 7). None if there is none."""
    if text is None:
        return None
    m = re.search(_NUMBER, str(text))
    return float(m.group().replace(",", ".")) if m else None
 
 
def number_before(text: str, unit: str) -> float | None:
    """Number immediately followed by *unit* (a regex), e.g. number_before(t, 'km/h')."""
    m = re.search(rf"({_NUMBER})\s*{unit}", text or "", re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None
 
 
def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9
 
 
def _err(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
 
 
def finalize(r: Reading, expected: tuple[str, ...] = FIELDS) -> Reading:
    """Set an honest status: missing values -> Incomplete/No data, implausible -> Suspect."""
    if r.status != "OK":
        return r
    missing = [f for f in expected if getattr(r, f) is None]
    if len(missing) == len(expected):
        r.status, r.error = "No data", "no values found on page"
        return r
 
    suspect = [
        f"{f}={getattr(r, f):.1f}"
        for f, (lo, hi) in LIMITS.items()
        if getattr(r, f) is not None and not lo <= getattr(r, f) <= hi
    ]
    if r.high is not None and r.low is not None and r.low > r.high:
        suspect.append("low > high")
 
    notes = []
    if suspect:
        r.status = "Suspect"
        notes.append("implausible: " + ", ".join(suspect))
    elif missing:
        r.status = "Incomplete"
    if missing:
        notes.append("missing: " + ", ".join(missing))
    r.error = "; ".join(notes)
    return r
 
 
# ──────────────────────────────────────────────────────────────────────────────
# HTTP + browser factories
# ──────────────────────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    """Session with a shared User-Agent and automatic retry/backoff on 429/5xx."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=3, backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
 
 
@contextmanager
def browser():
    """One headless Chrome shared by all Selenium sources, always closed on exit."""
    opts = webdriver.ChromeOptions()
    for arg in (
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--window-size=1920,1080", f"--user-agent={USER_AGENT}",
    ):
        opts.add_argument(arg)
    opts.page_load_strategy = "eager"  # we wait for the elements we need explicitly
    driver = webdriver.Chrome(options=opts)  # Selenium Manager fetches chromedriver
    driver.set_page_load_timeout(30)
    try:
        yield driver
    finally:
        driver.quit()
 
 
def wait_for_text(driver, locator, timeout, pattern=r"\d") -> str | None:
    """Wait until the element exists AND its text matches *pattern*; None on timeout.
    Re-locates the element on every poll, so stale-element errors can't escape."""
    def _ready(d):
        try:
            text = d.find_element(*locator).text.strip()
        except (NoSuchElementException, StaleElementReferenceException):
            return False
        return text if re.search(pattern, text) else False
 
    try:
        return WebDriverWait(driver, timeout).until(_ready)
    except TimeoutException:
        return None
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: Wunderground (requests + BeautifulSoup)
# ──────────────────────────────────────────────────────────────────────────────
def _wunderground_gust(table) -> float | None:
    for row in table.find_all("tr"):
        if "Wind Gust" in row.get_text():
            value = row.find("span", class_="wu-value")
            unit = row.find("span", class_="wu-label")
            gust = to_float(value.get_text().replace(",", "")) if value else None
            if gust is not None and unit and "mph" in unit.get_text():
                gust *= 1.60934
            return gust
    return None
 
 
def _wunderground_station(session, station_id: str, name: str) -> Reading:
    r = Reading("Wunderground", name, station_id)
    try:
        resp = session.get(
            f"https://www.wunderground.com/dashboard/pws/{station_id}", timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        tables = BeautifulSoup(resp.content, "html.parser").find_all(class_="summary-table")
        if len(tables) < 2:
            r.status, r.error = "Offline", "summary tables not on page"
            return r
 
        temps_table = tables[0]
        # Decimals are required on purpose (as before) but a leading minus is now kept.
        values = [float(x) for x in re.findall(r"-?\d+\.\d+", temps_table.get_text())]
        if len(values) < 4:
            r.status, r.error = "Incomplete", f"only {len(values)} numeric values found"
            return r
 
        unit = temps_table.select_one("span.wu-unit-temperature span.wu-label")
        imperial = "F" in (unit.get_text(strip=True) if unit else "F")
        high, low, _avg = values[:3]
        rain = values[-1]
        if imperial:
            high, low, rain = f_to_c(high), f_to_c(low), rain * 25.4  # °F->°C, in->mm
        r.high, r.low, r.rain = high, low, rain
        r.gust = _wunderground_gust(tables[1])
        return finalize(r)
    except Exception as exc:  # one bad station must not stop the others
        log.warning("Wunderground %s (%s): %s", station_id, name, _err(exc))
        r.status, r.error = "Error", _err(exc)
        return r
 
 
def fetch_wunderground(session) -> list[Reading]:
    readings = []
    for station_id, name in WUNDERGROUND_STATIONS.items():
        readings.append(_wunderground_station(session, station_id, name))
        time.sleep(POLITE_DELAY)
    return readings
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: Valaneio / Meteoclub (requests + BeautifulSoup)
# ──────────────────────────────────────────────────────────────────────────────
def _value_next_to_label(soup, label: str, unit: str) -> float | None:
    """Number in the cell to the right of the INNERMOST <td> containing *label*.
    (The old version could match an outer layout cell that also contained the label.)"""
    for td in soup.find_all("td"):
        if label in td.get_text() and not td.find("td"):
            sibling = td.find_next_sibling("td")
            if sibling:
                return number_before(sibling.get_text(" ", strip=True), unit)
    return None
 
 
def fetch_valaneio(session) -> list[Reading]:
    r = Reading("Meteoclub", "Valaneio")
    resp = session.get(VALANEIO_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
 
    temp_td = soup.find("td", align="left", bgcolor="#CCFFFF")
    if temp_td:
        paragraphs = temp_td.find_all("p")
        if len(paragraphs) >= 2:
            r.high = number_before(paragraphs[0].get_text(), "°C")
            r.low = number_before(paragraphs[1].get_text(), "°C")
 
    r.rain = _value_next_to_label(soup, "Today's Rain", "mm")
    # NB: the site only offers "High Wind Speed" (not a gust) - see notes.
    r.gust = _value_next_to_label(soup, "High Wind Speed", r"km/hr?")
    return [finalize(r)]
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: Penteli / NOA (Selenium)
# ──────────────────────────────────────────────────────────────────────────────
def _penteli_xpath(label: str, tail: str = "") -> str:
    return f'//span[contains(text(), "{label}")]/parent::div/following-sibling::div{tail}'
 
 
def _penteli_station(driver, name: str, slug: str) -> Reading:
    r = Reading("Penteli", name, slug)
    try:
        driver.get(f"https://penteli.meteo.gr/stations/{slug}/")
 
        # Wait ONCE for the page to render (full timeout) ...
        temp_box = (By.XPATH, _penteli_xpath("High Temperature"))
        if wait_for_text(driver, temp_box, PAGE_TIMEOUT) is None:
            r.status, r.error = "Offline", "page did not render in time"
            return r
 
        # ... then read the rest with short timeouts (old code: up to 3 x 15 s per station).
        spans = driver.find_element(*temp_box).find_elements(By.TAG_NAME, "span")
        if len(spans) >= 2:
            r.high = number_before(spans[0].text, "°C")
            r.low = number_before(spans[1].text, "°C")
 
        rain = wait_for_text(driver, (By.XPATH, _penteli_xpath("Today's Rain", "/span")), FIELD_TIMEOUT)
        gust = wait_for_text(driver, (By.XPATH, _penteli_xpath("High Wind Gust", "/span")), FIELD_TIMEOUT)
        r.rain = number_before(rain, "mm")
        r.gust = number_before(gust, "km/h")
        return finalize(r)
    except Exception as exc:
        log.warning("Penteli %s: %s", name, _err(exc))
        r.status, r.error = "Error", _err(exc)
        return r
 
 
def fetch_penteli(driver) -> list[Reading]:
    return [_penteli_station(driver, name, slug) for name, slug in PENTELI_STATIONS.items()]
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: Meteociel (requests + BeautifulSoup)
# ──────────────────────────────────────────────────────────────────────────────
def fetch_meteociel(session) -> list[Reading]:
    r = Reading("Meteociel", "Aerodromio", "16641")
    resp = session.get(METEOCIEL_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
 
    table = soup.find("table", bgcolor="#FFFF99")  # the yellow summary table
    rows = table.find_all("tr") if table else []
    if len(rows) < 2:
        r.status, r.error = "No data", "summary table not found"
        return [r]
 
    headers = [c.get_text(strip=True) for c in rows[0].find_all("td")]
    cells = [c.get_text(strip=True) for c in rows[1].find_all("td")]
    for label, value in zip(headers, cells):
        for key, field in METEOCIEL_COLUMNS.items():
            if key in label:
                setattr(r, field, to_float(value))
    return [finalize(r)]
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: Weathercloud (Selenium)
# ──────────────────────────────────────────────────────────────────────────────
def accept_consent(driver) -> None:
    try:
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, CONSENT_XPATH))
        ).click()
        time.sleep(1)
    except WebDriverException:  # includes TimeoutException: no banner is fine
        pass
 
 
def fetch_weathercloud(driver) -> list[Reading]:
    readings = []
    consent_done = False  # the cookie banner only needs dismissing once per browser session
 
    for station_id, name in WEATHERCLOUD_STATIONS.items():
        r = Reading("Weathercloud", name, station_id)
        try:
            driver.get(f"https://app.weathercloud.net/{station_id}#current")
            if not consent_done:
                accept_consent(driver)
                consent_done = True
 
            for i, (field, element_id) in enumerate(WEATHERCLOUD_GAUGES.items()):
                text = wait_for_text(
                    driver, (By.ID, element_id), PAGE_TIMEOUT if i == 0 else FIELD_TIMEOUT
                )
                if text is None and i == 0:
                    r.status, r.error = "Offline", "gauges did not load"
                    break  # don't burn another 2 x timeout on a dead station
                setattr(r, field, to_float(text))
            else:
                finalize(r, expected=("high", "low", "rain"))  # this source has no gust
        except Exception as exc:
            log.warning("Weathercloud %s (%s): %s", station_id, name, _err(exc))
            r.status, r.error = "Error", _err(exc)
        readings.append(r)
    return readings
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Source: IonianWeather (JSON)
# ──────────────────────────────────────────────────────────────────────────────
def fetch_ionianweather(session) -> list[Reading]:
    resp = session.get(IONIAN_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    stations = resp.json().get("stats") or {}
    if not stations:
        raise ValueError("'stats' key is missing or empty in the JSON")
 
    readings = []
    for name, info in stations.items():
        code = info.get("code")
        if code in IONIAN_TARGET_CODES:
            readings.append(finalize(Reading(
                "IonianWeather", name, code,
                high=to_float(info.get("Max Temperature")),
                low=to_float(info.get("Min Temperature")),
                rain=to_float(info.get("Rain By Day")),
                gust=to_float(info.get("Gust KlmPerHour")),
            )))
 
    for code in sorted(IONIAN_TARGET_CODES - {r.station_id for r in readings}):
        log.warning("IonianWeather: station %s missing from feed", code)
        readings.append(Reading("IonianWeather", "(not in feed)", code,
                                status="Offline", error="code missing from JSON"))
    return readings
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────────────
def _cell(value: float | None, unit: str) -> str:
    return "N/A" if value is None else f"{value:.1f} {unit}"
 
 
def render_text(readings: list[Reading], started: datetime) -> str:
    header = (
        f"{'SOURCE':<14} | {'STATION':<22} | {'ID':<12} | {'HIGH':>9} | "
        f"{'LOW':>9} | {'RAIN':>9} | {'GUST':>11} | STATUS"
    )
    rule = "=" * len(header)
    lines = [
        f"Weather data extracted at {started:%Y-%m-%d %H:%M:%S} ({started.tzname()})",
        "", rule, header, "-" * len(header),
    ]
    for r in readings:
        lines.append(
            f"{r.source:<14} | {r.station:<22} | {r.station_id:<12} | "
            f"{_cell(r.high, '°C'):>9} | {_cell(r.low, '°C'):>9} | "
            f"{_cell(r.rain, 'mm'):>9} | {_cell(r.gust, 'km/h'):>11} | {r.status}"
        )
    lines.append(rule)
 
    problems = [r for r in readings if r.status != "OK"]
    if problems:
        lines += ["", "Issues:"]
        lines += [
            f"  {r.source} / {r.station}: {r.status}" + (f" ({r.error})" if r.error else "")
            for r in problems
        ]
    ok = len(readings) - len(problems)
    lines += ["", f"{ok}/{len(readings)} stations OK"]
    return "\n".join(lines) + "\n"
 
 
def write_reports(readings: list[Reading], started: datetime, out_dir: Path = OUTPUT_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"results_{started:%Y-%m-%d}_{started:%H%M}"
    txt_path, csv_path = out_dir / f"{stem}.txt", out_dir / f"{stem}.csv"
 
    text = render_text(readings, started)
    txt_path.write_text(text, encoding="utf-8")
 
    stamp = started.isoformat(timespec="seconds")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["extracted_at", *(fl.name for fl in fields(Reading))])
        writer.writeheader()
        for r in readings:
            writer.writerow({"extracted_at": stamp, **asdict(r)})
    return txt_path, csv_path, text
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────
def run_source(name: str, func, client) -> list[Reading]:
    """Run one source; a crash in it becomes a visible 'Error' row, not a dead script."""
    t0 = time.monotonic()
    try:
        readings = func(client)
    except Exception as exc:
        log.error("%s failed: %s", name, _err(exc))
        return [Reading(name, "(entire source)", status="Error", error=_err(exc))]
    ok = sum(r.status == "OK" for r in readings)
    log.info("%-14s %2d/%-2d OK  (%.1fs)", name, ok, len(readings), time.monotonic() - t0)
    return readings
 
 
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    started = datetime.now(TZ)
    log.info("Run started %s (system TZ: %s)", started.isoformat(timespec="seconds"), time.tzname)
 
    readings: list[Reading] = []
 
    session = make_session()
    for name, func in (
        ("Wunderground", fetch_wunderground),
        ("Meteoclub", fetch_valaneio),
        ("Meteociel", fetch_meteociel),
        ("IonianWeather", fetch_ionianweather),
    ):
        readings += run_source(name, func, session)
 
    try:
        with browser() as driver:
            for name, func in (("Penteli", fetch_penteli), ("Weathercloud", fetch_weathercloud)):
                readings += run_source(name, func, driver)
    except WebDriverException as exc:  # Chrome missing / failed to start
        log.error("Browser unavailable: %s", _err(exc))
        readings.append(Reading("Selenium", "(browser)", status="Error", error=_err(exc)))
 
    txt_path, text = write_reports(readings, started)
    print(text)
    log.info("Saved %s", txt_path)
 
    # Non-zero exit when nothing worked, so cron/CI notices.
    return 0 if any(r.status == "OK" for r in readings) else 1
 
 
if __name__ == "__main__":
    sys.exit(main())
