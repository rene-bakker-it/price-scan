from __future__ import annotations

import json
import logging
import re
from collections import namedtuple
from copy import deepcopy
from datetime import datetime
from sqlite3.dbapi2 import paramstyle
from typing import Any, Self

from aiohttp import ClientResponse
from aiosqlite import Connection

from pc.utils import find_airport

try:
    from pc.utils import airports
except ModuleNotFoundError:
    # for testing
    pass

logger = logging.getLogger('models')

FLIGHTS = 'flights'

DEFAULT_CLASS_CODE = 'lowest'

def map_class(class_code: str) -> str:
    if class_code is None:
        class_code = 'unknown'

    match class_code.lower():
        case 'standard' | 'premium' | 'smart':
            return '2nd Class'
        case 'business' | 'prima business' | 'prima':
            return '1st Class'
        case 'executive' | 'club executive':
            return 'Executive'
        case 'business solottino' | 'solotto':
            return 'Salotto'

    return class_code


def validate_table_name(table_name: str):
    if table_name is None:
        raise RuntimeError('table_name cannot be None.')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]+', table_name):
        raise ValueError('Invalid table name')


def validate_regex(regex: str, value: str | None, allow_null=True) -> str | None:
    if isinstance(value, str) and re.match(regex, value):
        return value
    if allow_null:
        return None
    raise ValueError('Value may not be null.')


def validate_range(r: tuple[int, int] | None) -> tuple[int | None, int | None]:
    v1, v2 = None, None
    if r:
        v1, v2 = r
        if v1 < 1:
            v1 = None
        if v2 < 1:
            v2 = None
        if v1 and v2 and v2 < v1:
            v1, v2 = v2, v1
    return v1, v2


async def find_by_id(db: Connection, table_name: str, _id: int) -> dict[str, Any]:
    def tcn(name: str) -> str:
        """Transform column names to match the class attributes."""
        name = name.lower()
        return 'pk' if name == 'id' else name

    validate_table_name(table_name)
    cursor = await db.execute(f'SELECT * FROM "{table_name}" WHERE ID = ?', (_id,))
    row = await cursor.fetchone()
    return dict(row) if row is not None else {}

def value_to_instance_value(ct: ColumnType, value: Any) -> Any:
    match ct.type.__name__:
        case 'datetime':
            try:
                return datetime.strptime(str(value), ct.format)
            except ValueError:
                logger.error(f'Cannot convert value "{value}" to datetime for column "{ct.name}".')
                return None
        case 'int':
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    logger.error(f'Cannot convert value "{value}" to int for column "{ct.name}".')
                    return None
        case 'str':
            if ct.format:
                if not re.match(ct.format, str(value)):
                    logger.error(f'Value "{value}" does not match format "{ct.format}" for column "{ct.name}".')
                    return None
        case _:
            logger.warning(f'Unknown type "{ct.type}" for column "{ct.name}".')
    return value

def airport_name(code: str) -> str:
    if airport := airports.get(code):
        return f'{airport["name"]} ({code})'
    return code


ColumnType = namedtuple('ColumnType', ['name', 'type', 'format'])

class Sqlite:

    table_name = None

    id = ColumnType('ID', int, None)
    created_at = ColumnType('CREATED_AT', datetime, '%Y-%m-%d %H:%M:%S')

    def __init__(self, created_at: datetime | None = None):
        self._column_types: dict[str, ColumnType] = {}
        for cls in type(self).__mro__[:-1]:
            for k, v in cls.__dict__.items():
                if isinstance(v, ColumnType):
                    self._column_types[k] = v

        self.id: int | None = None
        self.created_at = created_at
        validate_table_name(str(self.table_name))

    def _values_to_db(self, exclude_null: bool = False, exclude_ts: bool = False) -> dict[str, Any]:
        """Return a dictionary of column names and their values for database operations."""
        data: dict[str, Any] = {}
        excluded_values = ['id']
        if exclude_ts:
            excluded_values.append('created_at')
        for k in [_k for _k in self.__dict__.keys() if (not _k.startswith('_')) and (_k not in excluded_values)]:
            if (ct := self._column_types.get(k)) is None:
                logger.warning(f'Cannot find column definition "{k}" for "{self.table_name}".')
                continue
            value = getattr(self, k)
            if k == 'id' and (value is None or value < 1):
                continue
            if isinstance(value, datetime) and (ct.format is not None):
                value = datetime.strftime(value, ct.format)
            if isinstance(value, str) and (len(value.strip()) == 0):
                value = None
            if exclude_null and (value is None):
                continue
            data[ct.name] = value
        return data

    def _db_to_instance(self, data: dict[str, Any]) -> Self:
        """Populate the instance attributes from a database row."""
        instance = deepcopy(self)
        for k in [_k for _k in self.__dict__.keys() if not _k.startswith('_')]:
            if (ct := self._column_types.get(k)) is None:
                logger.warning(f'Cannot find column definition "{k}" for "{self.table_name}".')
                continue
            if (value := data.get(ct.name)) is None:
                setattr(instance, k, None)
            else:
                setattr(instance, k, value_to_instance_value(ct, value))
        return instance

    @property
    def dict(self):
        return dict([(k, v) for k, v in self.__dict__.items() if not k.startswith('_')])

    @property
    def timestamp(self) -> str:
        if self.created_at is None:
            return datetime.now().strftime(Sqlite.created_at.format)
        return self.created_at.strftime(Sqlite.created_at.format)

    @property
    def euro(self) -> float:
        if hasattr(self, 'price_cents') and (p := getattr(self, 'price_cents')) is not None:
            return 0.01 *p
        return 0.0

    async def insert_or_update(self, db: Connection) -> None:
        """Insert or update the itinerary in the database."""
        
        if self.created_at is None:
            self.created_at = datetime.now()
        data: dict[str, Any] = self._values_to_db()
        if (self.id is None) or (self.id < 1):
            sql = f"INSERT INTO {self.table_name} ({','.join(data.keys())}) VALUES ({', '.join(['?']*len(data))})"
        else:
            setters = ','.join([f"{k} = ?" for k in data.keys()])
            sql = f"UPDATE {self.table_name} SET {setters} WHERE ID = {self.id}"

        cursor = await db.execute(sql, list(data.values()))
        if ((self.id is None) or (self.id < 1)) and (last_id := cursor.lastrowid) is not None:
            self.id = last_id
        await db.commit()

    async def find_all(self, db: Connection) -> list[Self]:
        sql = f"SELECT * FROM {self.table_name} ORDER BY ID"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        return [self._db_to_instance(dict(row)) for row in rows]

    async def find_by_example(self, db: Connection) -> list[Self]:
        data = self._values_to_db(exclude_null=True, exclude_ts=True)
        where_clauses = [f'{k} = ?' for k in data.keys()]
        sql = f"SELECT * FROM {self.table_name} WHERE {' AND '.join(where_clauses)} ORDER BY ID"
        cursor = await db.execute(sql, list(data.values()))
        rows = await cursor.fetchall()
        return [self._db_to_instance(dict(row)) for row in rows]

    async def find_one_by_example(self, db: Connection) -> Self | None:
        items = await self.find_by_example(db)
        if (n := len(items)) == 0:
            return None
        if n > 1:
            logger.warning(f'find_one_by_example found {n} items taking first.')
        return items[0]

    async def find_distinct_by_attribute(self, db: Connection, attribute: str) -> list[Any]:
        def filter_key(key: str, column_name: str) -> bool:
            if key.startswith('ID_'):
                return True
            return key != column_name


        if (ct := self._column_types.get(attribute)) is None:
            raise ValueError(f'Attribute "{attribute}" not found in class "{type(self).__name__}".')
        if len(idc := dict([(k, v) for k, v in self._values_to_db(exclude_null=True, exclude_ts=True).items() if filter_key(k, ct.name)])) == 0:
            sql = f"SELECT DISTINCT {ct.name} FROM {self.table_name} ORDER BY {ct.name}"
            cursor = await db.execute(sql)
        else:
            where_clauses = [f'{k} = ?' for k in idc.keys()]
            sql = f"SELECT DISTINCT {ct.name} FROM {self.table_name} WHERE {' AND '.join(where_clauses)} ORDER BY {ct.name}"
            cursor = await db.execute(sql, list(idc.values()))
        rows = await cursor.fetchall()
        return [value_to_instance_value(ct, row[ct.name]) for row in rows]

    async def delete(self, db: Connection, counter: DeleteCounter) -> None:
        counter.add(self)
        sql = f"DELETE FROM {self.table_name} WHERE ID = ?"
        await db.execute(sql, (self.id,))
        await db.commit()


class DeleteCounter:
    def __init__(self):
        self.items: dict[str, set[int]] = {}

    def add(self, data: Sqlite):
        if data.id is not None:
           if data.table_name not in self.items:
               self.items[data.table_name] = set()
           self.items[data.table_name].add(data.id)

    @property
    def total(self):
        n = 0
        for s in self.items.values():
            n += len(s)
        return n


class Itinerary(Sqlite):
    table_name = "ITINERARY"

    company = ColumnType('COMPANY', str, None)
    depart_from = ColumnType('DEPARTURE', str, None)
    arrive_at = ColumnType('ARRIVAL', str, None)

    def __init__(self, company: str | None = None, depart_from: str | None = None, arrive_at: str | None = None, created_at: datetime | None = None):
        super().__init__(created_at)
        self.company = company
        self.depart_from = depart_from
        self.arrive_at = arrive_at

    @classmethod
    async def find_by_id(cls, db: Connection, _id: int) -> Itinerary | None:
        data = await find_by_id(db, cls.table_name, _id)
        return cls(**data)

    async def delete(self, db: Connection, counter: DeleteCounter) -> None:
        # Delete all services and price classes associated with this itinerary
        services = await Service(itinerary=self).find_by_example(db)
        for service in services:
            await service.delete(db, counter)
        await super().delete(db, counter)


class FlightItinerary(Itinerary):

    def __init__(self, **kwargs):
        super().__init__(FLIGHTS, airport_name(kwargs['origin']), airport_name(kwargs['destination']))


class Service(Sqlite):
    table_name = "SERVICE"

    id_itinerary = ColumnType('ID_ITINERARY', int, None)
    code = ColumnType('CODE', str, None)
    date = ColumnType('DATE_TRIP', datetime, '%Y-%m-%d')
    time_depart = ColumnType('TIME_DEPART', str, r'[0-9]{2}:[0-9]{2}')
    time_arrive = ColumnType('TIME_ARRIVE', str, r'[0-9]{2}:[0-9]{2}')
    duration = ColumnType('DURATION_MINUTES', int, None)
    available_classes = ColumnType('AVAILABLE_CLASSES', str, None)

    def __init__(self, itinerary: Itinerary | int | None = None, code: str | None = None,
                 travel_date: datetime | str | None = None,
                 time_depart: str | None = None, time_arrive: str | None = None, duration: int | None = None,
                 created_at: datetime | None = None, available_classes: str | None = None):
        super().__init__(created_at)
        if isinstance(itinerary, Itinerary):
            self.id_itinerary = itinerary.id
        else:
            self.id_itinerary = itinerary
        if self.id_itinerary is None:
            raise ValueError('FK reference may not be null.')

        self.code = code
        if isinstance(travel_date, str):
            try:
                self.date = datetime.strptime(travel_date, '%Y-%m-%d')
            except ValueError:
                logger.error(f'Cannot convert value "{travel_date}" to datetime for column "date".')
                self.date = None
        else:
            self.date = travel_date
        self.time_depart = validate_regex(Service.time_depart.format, time_depart)
        self.time_arrive = validate_regex(Service.time_arrive.format, time_arrive)
        self.available_classes = available_classes
        self.duration = duration
        if (duration is not None) and (duration < 1):
            self.duration = None

    def get_available_classes(self) -> list[str]:
        if (self.available_classes is not None) and (len(self.available_classes) >= 5):
            try:
                return json.loads(self.available_classes)
            except Exception:
                pass
        return []

    def update_available_classes(self, classes: list[str] | set[str]) -> bool:
        if not isinstance(classes, (list, set)):
            raise ValueError('classes must be a list or set of strings.')

        available_classes = set(self.get_available_classes())

        new_classes = set(classes)
        if (len(available_classes) != len(new_classes)) or (len(available_classes.difference(new_classes)) > 0):
            if len(new_classes) == 0:
                self.available_classes = None
            else:
                self.available_classes = json.dumps(sorted(new_classes))
            return True
        return False


    def is_available(self, class_code: str) -> bool:
        if class_code is None:
            return False
        available_classes = set([s.lower() for s in self.get_available_classes()])
        return class_code.lower() in available_classes

    async def insert_or_update(self, db: Connection):
        if self.time_depart is None:
            raise ValueError('time_depart may not be null.')
        if len(self.get_available_classes()) == 0:
            self.available_classes = None
        await super().insert_or_update(db)

    async def delete(self, db: Connection, counter: DeleteCounter) -> None:
        # Delete all price classes associated with this service
        for pc in await PriceClass(service=self).find_by_example(db):
            await pc.delete(db, counter)
        await super().delete(db, counter)


class FlightService(Service):

    def __init__(self, itinerary: FlightItinerary, travel_date: str, **kwargs):
        data = kwargs['outbound']
        codes: list[str] = []
        time_depart = None
        time_arrive = None
        for segment in data['segments']:
            if time_depart is None:
                time_depart = segment['departure_time_local'][11:16]
            time_arrive = segment['arrival_time_local'][11:16]
            codes.append(f'{segment["marketing_carrier_code"]}{segment["flight_number"]} ({segment["operating_carrier_name"]}) '
                         f'{segment["departure_airport"]}-{segment["arrival_airport"]}')

        super().__init__(itinerary=itinerary, code=' | '.join(codes),
                         travel_date=travel_date, time_depart=time_depart, time_arrive=time_arrive, duration=data['duration_minutes'],
                         available_classes=json.dumps([DEFAULT_CLASS_CODE,]))


class PriceClass(Sqlite):
    table_name = "FARE"

    id_service = ColumnType('ID_SERVICE', int, None)
    class_code = ColumnType('CLASS', str, None)
    price_cents = ColumnType('PRICE_CENTS', int, None)

    def __init__(self,
                 service: int | Service | None = None, class_code: str | None = None, price_cents: int | None = None,
                 created_at: datetime | None = None):
        super().__init__(created_at)
        if isinstance(service, Service):
            self.id_service = service.id
        else:
            self.id_service = service
        if self.id_service is None:
            raise ValueError('FK reference may not be null.')
        self.class_code = class_code
        self.price_cents = price_cents
        if self.price_cents and self.price_cents < 1:
            self.price_cents = None
            
    def _class(self):
        return map_class(self.class_code)


class FlightPriceClass(PriceClass):

    def __init__(self, service: FlightService, **kwargs):
        data = kwargs['price']
        super().__init__(service, DEFAULT_CLASS_CODE, 100*data['amount'])


class IgnavQueries(Sqlite):

    table_name = "IGNAV_QUERIES"

    outcome = ColumnType('OUTCOME', int, None)
    code = ColumnType('CODE', int, None)
    message = ColumnType('MESSAGE', str, None)
    query = ColumnType('QUERY', str, None)

    def __init__(self, query: dict[str, str] | None = None, created_at: datetime | None = None):
        super().__init__(created_at)
        if query:
            self.query = json.dumps(query)
        else:
            self.query = None
        self.outcome = None
        self.code = None
        self.message = None

    async def set(self, response: ClientResponse):
        self.code = response.status
        self.outcome = 1 if (self.code >= 200) and (self.code < 300) else 0
        if self.outcome == 0:
            self.message = await response.text()

    @staticmethod
    async def success_counter(db: Connection, target_date: datetime | None = None) -> int:
        if target_date is None:
            target_date = datetime.now()
        date_str = target_date.strftime('%Y-%m')
        cursor = await db.execute(
            f'SELECT COUNT(*) AS count FROM {IgnavQueries.table_name} WHERE OUTCOME = 1 AND CREATED_AT LIKE ?', (date_str,))
        row = await cursor.fetchone()
        return row['count'] if row else 0


if __name__ == '__main__':
    import asyncio

    from utils import airports
    from database import get_db
    from pathlib import Path

    logging.basicConfig(
        format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s', datefmt='[%Y-%m-%d %H:%M:%S %z]', level=logging.INFO

    )

    async def test_database():
        itinerary =  Itinerary('frecce', 'Starting', 'Arriving')
        async with get_db(False) as db:
            await itinerary.insert_or_update(db)
            print(json.dumps(itinerary.dict, default=str, indent=4))

            service = Service(itinerary,
                              'FR9093', datetime.strptime('2026-08-31', '%Y-%m-%d'), '10:20', '13:20', 180)
            await service.insert_or_update(db)
            print(json.dumps(service.dict, default=str, indent=4))

            itinerary.id = None
            c = await itinerary.find_one_by_example(db)
            print(f'Itinerary: {json.dumps(c.dict, default=str, indent=4)}')

            itinerary.company = 'italo'
            if await itinerary.find_one_by_example(db) is None:
                print('Italo not found.')

    async def test_ignav():
        with open(Path(__file__).parent.parent.parent / "test" / "ignav.json") as fp:
            data = json.load(fp)
        itinerary =  FlightItinerary(**data)
        async with get_db(False) as db:
            print(f'Success counter: {await IgnavQueries.success_counter(db)}')
            return

            if (db_itinerary := await itinerary.find_one_by_example(db)) is None:
                await itinerary.insert_or_update(db)
            else:
                itinerary = db_itinerary
            print(json.dumps(itinerary.dict, default=str, indent=4))

            departure_date = data['departure_date']
            for item in data["itineraries"]:
                service = FlightService(itinerary, departure_date, **item)
                if (db_srvice := await service.find_one_by_example(db)) is None:
                    await service.insert_or_update(db)
                else:
                    service = db_srvice
                print(json.dumps(service.dict, default=str, indent=4))

                price = FlightPriceClass(service, **item)
                await price.insert_or_update(db)
                print(json.dumps(price.dict, default=str, indent=4))

    # asyncio.run(test_database())
    asyncio.run(test_ignav())
