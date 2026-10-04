import unittest
from pathlib import Path

import aiosqlite
from fastapi import HTTPException
from main import QueryParameters
from pc.models import Itinerary, PriceClass, Service
from pydantic import ValidationError


class DurationFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(':memory:')
        self.db.row_factory = aiosqlite.Row
        schema = Path(__file__).resolve().parents[1] / 'app/resources/tables.sql'
        await self.db.executescript(schema.read_text())
        itinerary = Itinerary('test', 'Origin', 'Destination')
        await itinerary.insert_or_update(self.db)
        for time, duration, class_code in [
            ('08:00', 90, 'short'),
            ('09:00', 120, 'boundary'),
            ('10:00', 180, 'long'),
            ('11:00', None, 'unknown'),
            ('08:00', 240, 'long'),
        ]:
            service = Service(itinerary, travel_date='2026-10-10', time_depart=time, duration=duration)
            await service.insert_or_update(self.db)
            await PriceClass(service, class_code, 1000).insert_or_update(self.db)
        self.query = QueryParameters(company='test', depart_from='Origin', arrive_at='Destination', date='2026-10-10')

    async def asyncTearDown(self):
        await self.db.close()

    async def test_default_includes_all_durations(self):
        self.assertEqual(await self.query.list_values(self.db, 'services'), ['08:00', '09:00', '10:00', '11:00'])

    async def test_limit_includes_boundary_and_filters_classes(self):
        self.query.max_duration = 120
        self.assertEqual(await self.query.list_values(self.db, 'services'), ['08:00', '09:00'])
        self.assertEqual(await self.query.list_values(self.db, 'classes'), ['boundary', 'short'])

    async def test_no_matches_returns_empty_choices(self):
        self.query.max_duration = 60
        self.assertEqual(await self.query.list_values(self.db, 'services'), [])
        self.assertEqual(await self.query.list_values(self.db, 'classes'), [])

    async def test_price_queries_respect_limit(self):
        self.query.max_duration = 120
        self.query.time_depart = '08:00'
        self.query.class_code = 'short'
        self.assertEqual((await self.query.list_prices(self.db))['duration'], 90)
        self.query.time_depart = '10:00'
        with self.assertRaises(HTTPException):
            await self.query.list_prices(self.db)
        self.query.time_depart = None
        self.assertEqual(set(await self.query.list_prices_of_day(self.db)), {'08:00', '09:00'})

    def test_invalid_limits_are_rejected(self):
        for value in (0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                QueryParameters(max_duration=value)


if __name__ == '__main__':
    unittest.main()
