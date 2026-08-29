#!/usr/bin/env -S uv run --script

"""
Scanner for Italo (Italotreno). Connects to the Italo booking engine and scrapes ticket
prices for a given route and date.

NOTE ON SELECTORS
-----------------
The Italo booking engine (https://biglietti.italotreno.com/{lang}/booking/ricerca) is a
React single-page application. Element ids are generated dynamically (e.g.
`autocomplete-input-:R55bjttsvafjpkqkq:`) and are therefore NOT stable, so all controls are
located by placeholder / role / accessible text instead of by id.

The search / input flow below is derived from the live booking-engine markup. The
post-search results parsing (`_extract_rows`) is a best-effort, structure-agnostic scan and
should be validated against a live results page. Spots that need live validation are marked
with `# TODO(live-validate)`.
"""

import argparse
import asyncio
import json
import logging
import re
from datetime import date, datetime, time
from pathlib import Path

from aiosqlite import Connection
from pc.database import get_db
from pc.models import Itinerary, PriceClass, Service, map_class
from pc.utils import MinMaxPrice, positive_int
from playwright._impl._errors import Error as PlaywrightInternalError
from playwright.async_api import async_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DATA_TAG = "trains"
DB_PATH = Path(__file__).resolve().parent.parent / "resources" / "data.db"

# Italo booking engine (React SPA), Italian locale.
BOOKING_URL = "https://biglietti.italotreno.com/it/booking/ricerca"
DEPART_PLACEHOLDER = "Partenza da (città o stazione)"
ARRIVE_PLACEHOLDER = "Arrivo a (città o stazione)"
PASSENGERS_PLACEHOLDER = "Passeggeri"

logger = logging.getLogger("italo")


def parse_args(description: str | None = __doc__) -> argparse.Namespace:
    # noinspection PyDuplicates
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("departure", metavar="station", help="Departure station.")
    parser.add_argument("arrival", metavar="station", help="Arrival station.")
    parser.add_argument("date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        help="Travel date in YYYY-MM-DD format.")
    parser.add_argument('-p', "--passengers", type=positive_int, default=1,
        help="Number of passengers (default: 1).")
    parser.add_argument("--headless", action="store_false", help="Enable visible execution.")
    return parser.parse_args()


async def _dismiss_cookie_banner(page) -> None:
    """Best-effort dismissal of the cookie consent banner."""
    for selector in (
        "button:has-text('Accetta')",
        "#onetrust-accept-btn-handler",
        "button:has-text('Agree & Close')",
        "button:has-text('Accept')",
    ):
        try:
            btn = page.locator(selector).first
            await btn.wait_for(state="visible", timeout=4000)
            await btn.click()
            logger.info("Dismissed cookie banner via %s", selector)
            return
        except PlaywrightTimeoutError:
            continue
        except PlaywrightInternalError:
            continue


async def _select_station(page, placeholder: str, station: str | None) -> None:
    """Select a station via the Italo autocomplete.

    The field is a role="button" that, when clicked, remounts into a combobox ``input``.
    Two subtleties, both observed live:
      * React remounts the input right after the click, so we must wait for it to settle and
        then type into the *re-resolved* focused input (typing immediately loses focus).
      * The dropdown only populates from real keystrokes, so we use ``press_sequentially``
        (``fill`` blurs the field and no options appear). Suggestions are ``li[role=option]``.
    """
    if not station:
        return
    await page.get_by_placeholder(placeholder).first.click()
    await page.wait_for_timeout(1200)  # allow the React field to remount before typing

    combo = page.locator("input:focus").first
    await combo.press_sequentially(station, delay=120)

    # The autocomplete renders a listbox of role="option" entries.
    options = page.get_by_role("option")
    try:
        await options.first.wait_for(state="visible", timeout=10000)
        # Prefer an option whose text matches the requested station, else take the first.
        match = options.filter(has_text=re.compile(re.escape(station), re.IGNORECASE))
        target = match.first if await match.count() > 0 else options.first
        await target.click()
    except PlaywrightTimeoutError:
        # Fallback to keyboard selection if the listbox could not be observed.
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")


async def _select_departure_date(page, target_date: date) -> None:
    """Open the departure-date calendar and click the target day.

    The Italo search page renders the outbound ("Andata") date as a button showing e.g.
    "19 ago 2026". Clicking it opens a calendar whose day cells are
    ``td[role=gridcell][data-date='YYYY-MM-DD']`` each wrapping a ``button``. We address the
    day by its ISO ``data-date`` (robust across months/locale) and click the inner button.
    """
    iso = f"{target_date:%Y-%m-%d}"

    # Open the date picker via the outbound-date field (text like "19 ago 2026").
    trigger = page.get_by_role('button').filter(has_text=re.compile(r'\d{1,2}\s+\w{3,}\s+\d{4}')).first
    try:
        await trigger.click()
    except PlaywrightInternalError:
        logger.warning('Could not open the date picker; continuing with default date.')
        return

    # The calendar (react-day-picker) shows two months at once and only renders the currently
    # visible months, so a target outside that window must be reached via the month-navigation
    # arrows. The arrows use English aria-labels ("Next Month" / "Previous Month"); because two
    # month panels are shown, each panel has its own arrow and the non-actionable copies are
    # either ``disabled`` or ``.invisible`` -- so we always target the visible, enabled one.
    # The selectable target day is ``td[data-day='ISO']`` (excluding disabled/outside duplicates)
    # wrapping a ``button.rdp-day_button``.
    forward = target_date.replace(day=1) >= date.today().replace(day=1)
    arrow_label = 'Next Month' if forward else 'Previous Month'
    day_cell = page.locator(
        f"td[data-day='{iso}']:not([data-disabled='true']):not([data-outside='true']) button"
    ).first

    for _ in range(24):  # cap navigation to a two-year window
        try:
            await day_cell.wait_for(state='visible', timeout=1000)
            break
        except PlaywrightTimeoutError:
            pass
        arrow = page.locator(f"button[aria-label='{arrow_label}']:not([disabled]):visible").first
        if await arrow.count() == 0:
            break
        try:
            await arrow.click()
        except PlaywrightInternalError:
            break
        await page.wait_for_timeout(300)

    try:
        await day_cell.wait_for(state='visible', timeout=5000)
        await day_cell.click()
        # Give the SPA a moment to close the picker so it does not overlay the CERCA button.
        await page.wait_for_timeout(500)
        return
    except (PlaywrightTimeoutError, PlaywrightInternalError):
        pass

    logger.warning('Could not select departure date %s in the calendar.', iso)
    # Close the calendar (Escape) so a lingering overlay does not block the search button.
    try:
        await page.keyboard.press('Escape')
    except PlaywrightInternalError:
        pass


async def _set_passengers(page, count: int) -> None:
    """Open the passenger panel and set the number of adults.

    The Italo passenger control is a ``div[role=combobox][aria-haspopup=dialog]`` whose popover
    (``#passenger-popover-content-*``) only opens once both stations are selected. The stepper
    starts at 0 passengers, so the search is invalid ("Inserire almeno un passeggero") until at
    least one adult is added -- we therefore always add ``max(1, count)`` adults and confirm.
    Each row exposes an "Aggiungi passeggero" (add) and "Togli passeggero" (remove) button; the
    first add button is the Adults row.
    """
    count = max(1, count)

    trigger = page.locator("[aria-controls*='passenger-popover-content']").filter(has_text=PASSENGERS_PLACEHOLDER).first
    try:
        await trigger.click()
    except PlaywrightInternalError:
        logger.warning('Could not open the passenger panel; using default passenger count.')
        return

    # The first "Aggiungi passeggero" button corresponds to the Adults row.
    add = page.get_by_role('button', name='Aggiungi passeggero').first
    try:
        await add.wait_for(state='visible', timeout=8000)
    except PlaywrightTimeoutError:
        logger.warning('Passenger stepper did not appear; using default passenger count.')
        return

    for _ in range(count):
        try:
            await add.click()
            await page.wait_for_timeout(150)
        except PlaywrightInternalError:
            logger.warning('Could not increment passenger count.')
            break

    # Confirm the selection to close the popover.
    try:
        await page.get_by_role('button', name=re.compile(r'^\s*conferma\s*$', re.IGNORECASE)).first.click(timeout=4000)
    except (PlaywrightTimeoutError, PlaywrightInternalError):
        await page.keyboard.press('Escape')


# Shared JS helpers injected into the page-evaluated functions below.
# (norm/detectClass/childrenByTag/cleanClassName/parseFarePanel + the main-row list.)
_JS_HELPERS = r"""
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const timeRe = /\b([01]\d|2[0-3]):[0-5]\d\b/g;
    const durationRe = /\b\d{1,2}\s*h(?:\s*\d{1,2}\s*min)?\b|\b\d{1,2}\s*min\b/i;
    const priceRe = /\d{1,3}(?:[.\s]\d{3})*,\d{2}\s*€/;

    const detectClass = (text) => {
        const n = (text || '').toLowerCase();
        if (/club\s*executive|\bclub\b/.test(n)) return 'Club Executive';
        if (/\bsalotto\b/.test(n)) return 'Salotto';
        if (/prima\s*business|\bprima\b/.test(n)) return 'Prima Business';
        if (/\bcomfort\b/.test(n)) return 'Comfort';
        if (/smart\s*xl/.test(n)) return 'Smart XL';
        if (/\bsmart\b/.test(n)) return 'Smart';
        return null;
    };

    const childrenByTag = (el, tag) =>
        Array.from(el.children).filter((c) => c.tagName === tag);

    const cleanClassName = (th) => {
        const span = th.querySelector('span');
        return norm(span ? span.textContent : th.textContent);
    };

    const rowText = (el) => el.innerText || el.textContent || '';
    const countTimes = (el) => (rowText(el).match(timeRe) || []).length;

    // The list of main train rows (direct tbody children that carry >= 2 times).
    const mainRows = () => {
        const tbody = document.querySelector('#scrollable-table tbody');
        if (!tbody) return [];
        return childrenByTag(tbody, 'TR').filter((tr) => countTimes(tr) >= 2);
    };

    // Parse a train's expanded fare panel (a nested <table>) into per-class fares.
    // The nested table has a header row of <th> columns (travel classes) and one or
    // more price rows of <td> cells ("89,90 € Flex", "53,90 € Economy") aligned to them.
    const parseFarePanel = (tr) => {
        if (!tr) return [];
        const inner = tr.querySelector('table');
        if (!inner) return [];
        const fares = [];
        let columnClasses = [];
        Array.from(inner.querySelectorAll('tr')).forEach((r) => {
            const headers = childrenByTag(r, 'TH');
            if (headers.length) {
                columnClasses = headers.map((th) => detectClass(cleanClassName(th)) || cleanClassName(th));
                return;
            }
            childrenByTag(r, 'TD').forEach((td, col) => {
                const priceMatch = (td.innerText || td.textContent || '').match(priceRe);
                if (!priceMatch) return;
                const className = columnClasses[col] || detectClass(td.textContent);
                if (!className) return;
                fares.push({ class_name: className, price: norm(priceMatch[0]) });
            });
        });
        // Fallback for panels that only offer a Flex fare: such panels render
        // without the per-class <th> header row, so the column-based parse above
        // finds nothing. Capture the single Flex price and detect its class from
        // the panel text (defaulting to Smart when the class is not spelled out).
        if (!fares.length) {
            const panelText = rowText(inner);
            const flexPrice = panelText.match(priceRe);
            if (flexPrice) {
                fares.push({ class_name: detectClass(panelText) || 'Smart', price: norm(flexPrice[0]) });
            }
        }
        return fares;
    };
"""

# JavaScript that opens the fare panel of the i-th main train row.
# NOTE: the Italo fare accordion is single-open (opening one panel closes the
# previously opened one), so panels must be expanded and read one at a time.
# Each train row also renders a duplicate (mobile) toggle for the same panel;
# clicking both would toggle it twice (net: closed), so we click exactly one.
_CLICK_PANEL_JS = r"""(i) => {""" + _JS_HELPERS + r"""
    const tr = mainRows()[i];
    if (!tr) return null;
    const btn = tr.querySelector('button[aria-label^="Consulta tariffe"]');
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click();
    const m = rowText(tr).match(/\b([01]\d|2[0-3]):[0-5]\d\b/);
    return m ? m[0] : '';
}"""

# JavaScript that reports whether the i-th train's fare panel has finished rendering.
_PANEL_READY_JS = r"""(i) => {""" + _JS_HELPERS + r"""
    const tr = mainRows()[i];
    if (!tr) return false;
    const panel = tr.nextElementSibling;
    if (!panel) return false;
    const inner = panel.querySelector('table');
    if (!inner) return false;
    // Ready when the per-class header row has rendered, OR when a Flex-only panel
    // (which has no <th> header) has rendered its price. Without the second case
    // Flex-only trains would never satisfy this wait and time out.
    return !!(inner.querySelector('th') || rowText(inner).match(priceRe));
}"""

# JavaScript that extracts the i-th train row: its times/duration and the per-class
# fares from its (currently open) fare panel. Falls back to the cheapest "from" fare
# shown in the main row when the panel is not available.
_EXTRACT_ONE_JS = r"""(i) => {""" + _JS_HELPERS + r"""
    const tr = mainRows()[i];
    if (!tr) return null;
    const text = rowText(tr);
    const times = text.match(timeRe) || [];
    const durationMatch = text.match(durationRe);
    const fromPrice = text.match(priceRe);

    let fares = parseFarePanel(tr.nextElementSibling);
    if (!fares.length && fromPrice) {
        fares = [{ class_name: detectClass(text) || 'Smart', price: norm(fromPrice[0]) }];
    }

    return {
        solution_number: i + 1,
        train_number: null,
        departure_date: null,
        arrival_date: null,
        departure_time: times[0] || null,
        arrival_time: times[1] || null,
        duration: durationMatch ? norm(durationMatch[0]) : null,
        fares: fares,
    };
}"""

# Counts the number of main train rows currently rendered in the results table.
# Fare-detail rows (which have no times) are excluded so the count is stable
# regardless of how many panels are expanded.
_COUNT_JS = r"""() => {
    const table = document.querySelector('#scrollable-table');
    if (!table) return 0;
    const timeRegex = /\b([01]\d|2[0-3]):[0-5]\d\b/g;
    return Array.from(table.querySelectorAll('tbody tr')).filter((tr) => {
        return ((tr.innerText || '').match(timeRegex) || []).length >= 2;
    }).length;
}"""


async def _extract_rows(page) -> list[dict]:
    # Wait until the results table has rendered at least one train row.
    await page.wait_for_function("() => (" + _COUNT_JS + ")() > 0", timeout=120000)

    count = await page.evaluate(_COUNT_JS)

    # The fare accordion is single-open: expand each train's panel one at a time,
    # read its per-class fares, then move on (opening the next closes this one).
    rows: list[dict] = []
    for i in range(count):
        await page.evaluate(_CLICK_PANEL_JS, i)
        try:
            await page.wait_for_function(_PANEL_READY_JS, arg=i, timeout=8000)
        except PlaywrightTimeoutError:
            # Panel did not render in time; _EXTRACT_ONE_JS falls back to the "from" fare.
            logger.warning(f"Fare panel for train #{i + 1} did not open; using the 'from' fare.")
        row = await page.evaluate(_EXTRACT_ONE_JS, i)
        if row and row["fares"]:
            rows.append(row)

    if not rows:
        raise RuntimeError("No prices found in the results table on the Italo results page.")
    return rows


async def _scroll_for_more_rows(page, previous_count: int) -> int:
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    try:
        await page.wait_for_function(
            "(prev) => (" + _COUNT_JS + ")() > prev",
            arg=previous_count,
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        pass

    return await page.evaluate(_COUNT_JS)


def _duration_to_minutes(value: str | None) -> int:
    if not value:
        return 0

    normalized = value.lower().replace(" ", "")
    match = re.search(r"(?:(\d+)h)?(?:(\d+)(?:m|min))?", normalized)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return hours * 60 + minutes

    match = re.search(r"(\d{1,2}):(\d{2})h", normalized)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))

    raise ValueError(f"Cannot parse duration: {value!r}")


def _price_to_cents(value: str | None) -> int:
    if not value:
        raise ValueError("Missing price value.")

    normalized = value.replace("€", "").strip().replace(".", "").replace(",", ".")
    return int(round(float(normalized) * 100))


async def _persist_rows(db: Connection, itinerary: Itinerary, travel_date: datetime, rows: list[dict]) -> None:
    services: list[Service] = await Service(itinerary.id, travel_date= travel_date).find_by_example(db)
    now = datetime.now()
    for row in rows:
        if (departure_time := row.get('departure_time')) is None:
            logger.info(f'Skipping row with missing departure time: {row}')
            continue
        try:
            service = next(s for s in services if s.time_depart == departure_time)
        except StopIteration:
            service = Service(
                itinerary.id,
                code = f"ITA-{row.get('train_number', str(departure_time))}",
                travel_date= travel_date,
                time_depart = departure_time, time_arrive = row.get('arrival_time'),
                created_at=now)
        if service.duration is None:
            try:
                duration = _duration_to_minutes(row.get('duration'))
                service.duration = duration
            except ValueError:
                pass
            if service.available_classes is None:
                service.update_available_classes([])
            await service.insert_or_update(db)
        class_fares: dict[str, MinMaxPrice] = {}
        for fare in row["fares"]:
            try:
                price_cents = _price_to_cents(fare.get('price'))
            except ValueError:
                continue
            if (class_name := fare.get('class_name')) is None:
                continue
            class_name = map_class(class_name)
            if class_name not in class_fares:
                class_fares[class_name] = MinMaxPrice()
            class_fares[class_name].update(price_cents)

        found_classes: set[str] = set()
        for class_name, p in class_fares.items():
            if not p.valid:
                continue
            if (price_cents := p.min_price) > 0:
                price = PriceClass(service.id, class_name, price_cents)
                await price.insert_or_update(db)
                found_classes.add(class_name)
        if service.update_available_classes(found_classes):
            await service.insert_or_update(db)


async def run_query(target_date: date, departure_station: str, arrival_station: str, passenger_count: int, headless: bool):
    async with get_db(read_only=False) as db:
        example = Itinerary(DATA_TAG, departure_station, arrival_station)
        if (itinerary := await example.find_one_by_example(db)) is None:
            await example.insert_or_update(db)
            itinerary = example
        await check_ticket_price(db, itinerary, target_date, passenger_count, headless)


async def check_ticket_price(db: Connection, itinerary: Itinerary, target_date: date, passenger_count: int, headless: bool):
    async with async_playwright() as p:
        # 1. Launch a headless browser.
        #    IMPORTANT: Italo's edge/anti-bot rejects Chromium's TLS/HTTP2 fingerprint
        #    (the connection is aborted -> net::ERR_HTTP2_PROTOCOL_ERROR / timeout),
        #    while Firefox's fingerprint is accepted. So we use the Firefox engine.
        browser = await p.firefox.launch(headless=headless)

        # Emulate a real desktop Firefox browser.
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        try:
            # 2. Navigate to the Italo booking engine.
            #    Use domcontentloaded (not networkidle): the SPA keeps RUM/telemetry
            #    connections open, so networkidle can hang. Readiness is confirmed by
            #    waiting for the departure field below.
            logger.info("Navigating to Italo booking engine...")
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=90000)
            await _dismiss_cookie_banner(page)
            # Let the React SPA finish hydrating before interacting with the form fields.
            await page.wait_for_timeout(2500)
            await page.get_by_placeholder(DEPART_PLACEHOLDER).first.wait_for(state="visible", timeout=60000)

            # 3. Step 1: Input Origin and Destination via the autocomplete fields.
            await _select_station(page, DEPART_PLACEHOLDER, itinerary.depart_from)
            await page.wait_for_timeout(500)
            await _select_station(page, ARRIVE_PLACEHOLDER, itinerary.arrive_at)
            await page.wait_for_timeout(500)

            # 4. Step 2: Pick the departure date.
            await _select_departure_date(page, target_date)

            # 4b. Step 2b: Set passengers (the panel only opens once both stations are chosen).
            await _set_passengers(page, passenger_count)
            await page.wait_for_timeout(500)

            # 5. Step 3: Submit the search via the named CERCA (search) button.
            #    NOTE: several cookie-consent buttons are also type=submit, so target by name.
            logger.info("Submitting search query...")
            await page.get_by_role("button", name=re.compile(r"^\s*cerca\s*$", re.IGNORECASE)).first.click()

            # 6. Step 4: Wait for the results to render and extract all prices per result card.
            rows: list[dict] = []
            seen_rows: set[str] = set()
            while True:
                current_rows = await _extract_rows(page)
                stop_scrolling = False
                for row in current_rows:
                    if row["departure_date"] and row["arrival_date"] and row["arrival_date"] != row["departure_date"]:
                        stop_scrolling = True
                        break

                    row_key = (
                        f"{row['train_number']}|{row['departure_date']}|{row['arrival_date']}|"
                        f"{row['departure_time']}|{row['arrival_time']}|{row['duration']}|"
                        f"{json.dumps(row['fares'], sort_keys=True, ensure_ascii=False)}"
                    )
                    if row_key not in seen_rows:
                        seen_rows.add(row_key)
                        rows.append(row)

                if stop_scrolling:
                    break

                previous_count = len(current_rows)
                try:
                    new_count = await _scroll_for_more_rows(page, previous_count)
                except PlaywrightInternalError as e:
                    logger.warning(f'Scrolling failed with Playwright error: {e}')
                    new_count = 0
                if new_count <= previous_count:
                    break

            logger.info(f"Prices found by result card: n = {len(rows):3}")
            for row in rows:
                train_number = row["train_number"] or "N/A"
                dep_time = row["departure_time"] or "N/A"
                arr_time = row["arrival_time"] or "N/A"
                duration = row["duration"] or "N/A"
                fare_text = ", ".join(
                    f"{fare['class_name']}: {fare['price']}" for fare in row["fares"]
                )
                logger.debug(
                    "Solution %s [Train %s] (%s -> %s, %s): %s",
                    row["solution_number"],
                    train_number,
                    dep_time,
                    arr_time,
                    duration,
                    fare_text,
                )
            await _persist_rows(db, itinerary, datetime.combine(target_date, time()), rows)

        except Exception as e:
            logger.exception("Scraping failed during routine execution: %s", e)
            # Optional: Take a screenshot for debugging inside your Docker container
            # await page.screenshot(path="error_fallback.png")
            return None

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    logging.basicConfig(
        format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s',
        datefmt='[%Y-%m-%d %H:%M:%S %z]',
        level=logging.INFO
    )
    args = parse_args()

    asyncio.run(run_query(args.date, args.departure, args.arrival, args.passengers, args.headless))
