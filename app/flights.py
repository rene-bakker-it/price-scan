#!/usr/bin/env -S uv run --script

"""
Scanner for Ignav flights.
"""
import argparse
import asyncio
import logging
import os
from datetime import date, datetime
from pathlib import Path

import aiohttp
from aiosqlite import Connection
from pc.database import get_db
from pc.models import DEFAULT_CLASS_CODE, FLIGHTS, FlightItinerary, FlightPriceClass, FlightService, IgnavQueries
from pc.utils import parse_airport

DB_PATH = Path(__file__).resolve().parent.parent / "resources" / "data.db"
DATA_TAG = FLIGHTS

URL_ONE_WAY = r'https://ignav.com/api/fares/one-way'

HEADERS = {
    'Content-Type': 'application/json',
    'X-Api-Key': os.environ.get('IGNAV_API_KEY','default')
}

MONTHLY_LIMIT = 1000

logger = logging.getLogger(DATA_TAG)


def parse_args(description: str | None = __doc__) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument( "departure", metavar="airport", type=parse_airport,
        help="Airport departure.")
    parser.add_argument("arrival", metavar="airport", type=parse_airport,
        help="Airport arrival.")
    parser.add_argument("date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        help="Travel date in YYYY-MM-DD format.")
    return parser.parse_args()


async def run_query(target_date: date, iata_departure: str, iata_arrival: str):
    async with get_db(False) as db:
        itinerary_args = {'origin': iata_departure, 'destination': iata_arrival}
        example = FlightItinerary(**itinerary_args)
        if (itinerary := await example.find_one_by_example(db)) is None:
            await example.insert_or_update(db)
            itinerary = example
        if isinstance(itinerary, FlightItinerary):
            await check_ticket_price(db, itinerary, {
                'origin': iata_departure,
                'destination': iata_arrival,
                'departure_date': target_date.strftime("%Y-%m-%d")
            })

        else:
            raise RuntimeError('Unexpected itinerary type returned from database.')



async def check_ticket_price(db: Connection, itinerary: FlightItinerary, query: dict[str, str]):
    async with aiohttp.ClientSession() as session:
        outcome = IgnavQueries(query)
        try:
            if (n_success := await IgnavQueries.success_counter(db)) > (MONTHLY_LIMIT - 10):
                outcome.outcome = 0
                outcome.message = f'Monthly limit reached: {n_success} successful queries.'
            else:
                async with session.post(URL_ONE_WAY, json=query, headers=HEADERS) as response:
                    await outcome.set(response)
                    if outcome.outcome == 1:
                        data = await response.json()
                        departure_date = data['departure_date']
                        for item in data['itineraries']:
                            example = FlightService(itinerary, departure_date, **item)
                            if (service := await example.find_one_by_example(db)) is None:
                                service = example
                            service.update_available_classes([DEFAULT_CLASS_CODE, ])
                            await service.insert_or_update(db)
                            price = FlightPriceClass(service, **item)
                            if 0 < price.price_cents < 1000000:
                               await price.insert_or_update(db)
                            else:
                                raise RuntimeError('Unexpected service type returned from database.')

        except Exception as e:
            logger.error(f'Error occurred while checking ticket price: {e}')
            if outcome.outcome is None:
                outcome.outcome = 0
                outcome.message = f'Processing error: {e}'
        await outcome.insert_or_update(db)


if __name__ == "__main__":
    logging.basicConfig(
        format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s', datefmt='[%Y-%m-%d %H:%M:%S %z]', level=logging.INFO
    )
    args = parse_args()

    asyncio.run(run_query(args.date, args.departure, args.arrival))
