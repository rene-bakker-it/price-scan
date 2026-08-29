#!/usr/bin/env -S uv run --script

"""
Scanner for the Frecce Rosse. Connects to the Trenitalia booking portal and scrapes ticket
prices for a given route and date.
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
from pc.utils import MinMaxPrice, parse_time, positive_int
from playwright._impl._errors import Error as PlaywrightInternalError
from playwright.async_api import async_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DATA_TAG = "trains"
DB_PATH = Path(__file__).resolve().parent.parent / "resources" / "data.db"

logger = logging.getLogger("frecce")


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


async def _handle_cookie_consent(page):
    try:
        consent_button = page.locator("button#onetrust-accept-btn-handler")
        await consent_button.wait_for(state="visible", timeout=5000)
        await consent_button.click()
        logger.info("Cookie consent accepted.")
    except PlaywrightTimeoutError:
        logger.info("No cookie consent prompt found.")


async def _select_station(page, selector: str, station: str | None):
    field = page.locator(selector)
    # await field.fill(station)
    await field.press_sequentially(station, delay=120)
    await asyncio.sleep(0.25)
    await field.press("ArrowDown")
    await field.press("Enter")
    await asyncio.sleep(0.25)


async def _select_departure_details(page, target_date: date):
    target_time = "06:00" # Default to earliest time (we want all day)
    iso_date = f"{target_date:%Y-%m-%d}"
    await page.locator("#wrapperAndata .date-label").click()

    target_day = page.locator(f"#wrapperAndata [data-date='{iso_date}']").first
    try:
        await target_day.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        month_nav = (
            page.locator("button.next-month").first,
            page.locator("button.previous-month").first,
        )
        current_marker = date.today().replace(day=1)
        target_marker = target_date.replace(day=1)

        while current_marker != target_marker:
            direction = 0 if current_marker < target_marker else 1
            await month_nav[direction].click()
            await page.wait_for_timeout(250)
            step = 1 if current_marker < target_marker else -1
            year = current_marker.year + ((current_marker.month + step - 1) // 12)
            month = (current_marker.month + step - 1) % 12 + 1
            current_marker = current_marker.replace(year=year, month=month)

        await target_day.wait_for(state="visible", timeout=5000)

    await target_day.click()

    await page.locator("#idButtonOrarioAndata").click()
    await page.locator("#andata-orario .time-p", has_text=target_time).click()


async def _set_adults(page, count: int):
    await page.locator("#dropdownMenuButtonPasseggeri").click()
    passengers_menu = await page.locator("#dropdownMenuButtonPasseggeri + .dropdown-menu.drop-left")
    await passengers_menu.wait_for(state="visible")

    adult_container = await passengers_menu.locator(".adulti-container")
    quantity = await adult_container.locator("p.quantity")
    plus_button = await adult_container.locator(".dropdown-item.btn-plus-minus[data-operation='plus']")
    minus_button = await adult_container.locator(".dropdown-item.btn-plus-minus[data-operation='minus']")

    current = int(quantity.inner_text().strip())
    while current < count:
        plus_button.click()
        current = int(quantity.inner_text().strip())
    while current > count:
        minus_button.click()
        current = int(quantity.inner_text().strip())

    await passengers_menu.locator("button.cta.conferma", has_text="Conferma").first.click()


async def _extract_rows(page) -> list[dict]:
    await page.wait_for_url("**www.lefrecce.it/Channels.Website.WEB/**", timeout=120000)
    await page.wait_for_function(
        "() => window.location.hash.includes('search-results')",
        timeout=120000,
    )
    await page.wait_for_function(
        "() => document.querySelectorAll('solution').length > 0",
        timeout=120000,
    )

    rows = await page.evaluate(
        """() => {
            const moneyRegex = /(?:€\\s*)?\\d{1,4}(?:[.,]\\d{2})?\\s*€/g;
            const timeRegex = /\\b(?:[01]\\d|2[0-3]):[0-5]\\d\\b/g;
            const durationRegex = /\\b\\d{1,2}\\s*h(?:\\s*\\d{1,2}\\s*(?:m|min))?\\b|\\b\\d{1,2}\\s*(?:m|min)\\b|\\b\\d{1,2}:\\d{2}\\s*h\\b/gi;
            const normalize = (value) => value.replace(/\\s+/g, ' ').trim();
            const extractDates = (text) => {
                if (!text) return [];
                const dates = [];
                for (const match of text.matchAll(/\\b(\\d{4})-(\\d{2})-(\\d{2})\\b/g)) {
                    dates.push(`${match[1]}-${match[2]}-${match[3]}`);
                }
                for (const match of text.matchAll(/\\b(\\d{2})\\/(\\d{2})\\/(\\d{4})\\b/g)) {
                    dates.push(`${match[3]}-${match[2]}-${match[1]}`);
                }
                return dates;
            };
            const extractMoney = (text) => {
                if (!text) return [];
                const matches = text.match(moneyRegex) || [];
                return matches.map(normalize);
            };
            const extractTimes = (text) => {
                if (!text) return [];
                return (text.match(timeRegex) || []).map(normalize);
            };
            const extractDurations = (text) => {
                if (!text) return [];
                return (text.match(durationRegex) || []).map(normalize);
            };
            const detectClass = (text) => {
                const normalized = (text || '').toLowerCase();
                if (/\\bexecutive\\b/.test(normalized)) return "Executive";
                if (/\\bbusiness\\b/.test(normalized)) return "Business";
                if (/\\bpremium\\b/.test(normalized)) return "Premium";
                if (/\\bstandard\\b/.test(normalized)) return "Standard";
                if (/\\b2\\s*classe\\b|\\bseconda\\s*classe\\b/.test(normalized)) return "2ª Classe";
                if (/\\b1\\s*classe\\b|\\bprima\\s*classe\\b/.test(normalized)) return "1ª Classe";
                return null;
            };
            const classFromNode = (node, solutionNode) => {
                const direct = detectClass(node.innerText || node.textContent || "");
                if (direct) return direct;

                const card = node.closest(
                    "[class*='offer'], [class*='Offer'], [class*='class'], [class*='Class'], [class*='price'], [class*='Price'], li, article, section, div"
                );
                const cardClass = card ? detectClass(card.innerText || card.textContent || "") : null;
                if (cardClass) return cardClass;

                let current = node.parentElement;
                for (let i = 0; i < 5 && current; i += 1) {
                    const found = detectClass(current.innerText || current.textContent || "");
                    if (found) return found;
                    current = current.parentElement;
                }

                const wholeSolutionClass = detectClass(solutionNode.innerText || "");
                return wholeSolutionClass || "Unknown";
            };

            const solutionNodes = Array.from(document.querySelectorAll('solution'));
            return solutionNodes.map((node, index) => {
                const fares = [];
                const seen = new Set();
                const addFare = (className, price) => {
                    if (!price) return;
                    const key = `${className}|${price}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    fares.push({ class_name: className, price: price });
                };

                const candidateNodes = node.querySelectorAll(
                    "[class*='price'], [class*='Price'], [id*='price'], [id*='Price'], [class*='prezzo'], [class*='tariff']"
                );

                candidateNodes.forEach((candidate) => {
                    const className = classFromNode(candidate, node);
                    extractMoney(candidate.textContent || '').forEach((price) => addFare(className, price));
                });

                const times = extractTimes(node.innerText || "");
                const departureTime = times.length > 0 ? times[0] : null;
                const arrivalTime = times.length > 1 ? times[1] : null;
                const durations = extractDurations(node.innerText || "");
                const duration = durations.length > 0 ? durations[0] : null;
                const dates = extractDates(node.innerText || "");
                const departureDate = dates.length > 0 ? dates[0] : null;
                const arrivalDate = dates.length > 1 ? dates[dates.length - 1] : departureDate;
                const trainNumberNode = node.querySelector("span[id^='trains-recap-']");
                const trainNumber = trainNumberNode ? normalize(trainNumberNode.textContent || '') : null;

                return {
                    solution_number: index + 1,
                    train_number: trainNumber,
                    departure_date: departureDate,
                    arrival_date: arrivalDate,
                    departure_time: departureTime,
                    arrival_time: arrivalTime,
                    duration: duration,
                    fares: fares,
                };
            }).filter((row) => row.fares.length > 0);
        }"""
    )

    if not rows:
        raise RuntimeError("No prices found in solution rows on Lefrecce search results page.")
    return rows


async def _scroll_for_more_rows(page, previous_count: int) -> int:
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    try:
        await page.wait_for_function(
            "(prev) => document.querySelectorAll('solution').length > prev",
            arg=previous_count,
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        pass

    return await page.evaluate("() => document.querySelectorAll('solution').length")


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
    if travel_date is None:
        logger.error("Travel date must be provided for persisting rows.")
        return
    services: list[Service] = await Service(itinerary.id, travel_date= travel_date).find_by_example(db)
    now = datetime.now()
    for row in rows:
        if (departure_time := row.get('departure_time')) is None:
            logger.info(f'Skipping row with missing departure time: {row}')
            continue
        try:
            service = next(s for s in services if s.time_depart == departure_time)
            if service.date is None:
                service.date = travel_date
        except StopIteration:
            service = Service(
                itinerary.id,
                code = "FR-" + row.get('train_number', str(departure_time)),
                travel_date= travel_date,
                time_depart = departure_time, time_arrive = row.get('arrival_time'),
                created_at=now)
        if service.duration is None:
            try:
                duration = _duration_to_minutes(row.get('duration'))
                service.duration = duration
            except ValueError:
                pass
            if service.date is None:
                logger.error(f'Date of service not set. Forcing to: {travel_date}')
                service.date = travel_date
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
    async with get_db(False) as db:
        example = Itinerary(DATA_TAG, departure_station, arrival_station)
        if (itinerary := await example.find_one_by_example(db)) is None:
            await example.insert_or_update(db)
            itinerary = example
        await check_ticket_price(db, itinerary, target_date, passenger_count, headless)


async def check_ticket_price(db: Connection, itinerary: Itinerary, target_date: date, passenger_count: int, headless: bool):
    async with async_playwright() as p:
        # 1. Launch a persistent or stealthy headless browser
        browser = await p.firefox.launch(headless=headless)

        # Emulate a real desktop browser to bypass initial bot checks
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        try:
            # 2. Navigate to the landing page
            logger.info("Navigating to booking portal...")
            await page.goto("https://www.trenitalia.com/it.html", wait_until="networkidle")

            await _handle_cookie_consent(page)

            # 3. Step 1: Input Origin and Destination
            await page.locator("#partenza").wait_for(state="visible")
            await _select_station(page, "#partenza", itinerary.depart_from)
            await _select_station(page, "#arrivo", itinerary.arrive_at)

            # 4. Step 2: Input Date
            await _select_departure_details(page, target_date)

            # 4b. Step 2b: Set passengers
            if passenger_count > 1:
                await _set_adults(page, passenger_count)

            # 4c. Step 2c: Choose Le Frecce and enable best price
            await page.locator("#dropdownMenuButtonSoluzioni").click()
            await page.locator("label[for='2']").click()
            # page.locator("#bestprice").check()
            await page.locator(
                ".dropdown-menu.drop-right "
                "button.cta.cta-primary.conferma.nomedia",
                has_text="Conferma",
            ).first.click()

            # 5. Step 3: Click Search and Wait for the SPA to load table data
            logger.info("Submitting search query...")
            await page.locator("#searchBtn").click()

            # 6. Step 4: Wait for Lefrecce redirect and extract all prices per solution row
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

            logger.info(f"Prices found by solution row: n = {len(rows):3}")
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
            await _persist_rows(db, itinerary, args.date, rows)

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
        format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s', datefmt='[%Y-%m-%d %H:%M:%S %z]', level=logging.INFO
    )
    args = parse_args()

    asyncio.run(run_query(args.date, args.departure, args.arrival, args.passengers, args.headless))