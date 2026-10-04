import logging
from collections import namedtuple
from pathlib import Path

from aiosqlite import Connection
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pc.calls import list_names
from pc.database import get_db
from pc.models import DeleteCounter, IgnavQueries, Itinerary, PriceClass, Service, Sqlite
from pydantic import BaseModel, Field

logger = logging.getLogger('gui')

logging.basicConfig(format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s', datefmt='[%Y-%m-%d %H:%M:%S %z]', level=logging.INFO)

TEMPLATES_DIR = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=TEMPLATES_DIR)

app = FastAPI(title="Price Scan API")

DatePrice = namedtuple('DatePrice', ['date', 'price'])


class QueryParameters(BaseModel):
    company: str | None = None
    depart_from: str | None = None
    arrive_at: str | None = None
    date: str | None = None
    time_depart: str | None = None
    class_code: str | None = None
    max_duration: int | None = Field(default=None, gt=0)

    async def _get_itinerary(self, db: Connection) -> Itinerary:
        if self.depart_from is None or self.arrive_at is None:
            raise HTTPException(status_code=400, detail='Depart from and arrive at are required')
        if (item := await Itinerary(company=self.company, depart_from=self.depart_from, arrive_at=self.arrive_at).find_one_by_example(db)) is None:
            raise HTTPException(status_code=404, detail='Itinerary not found')
        return item

    async def _get_services(self, db: Connection) -> list[Service]:
        itinerary = await self._get_itinerary(db)
        if self.date is None:
            raise HTTPException(status_code=400, detail='Date is required')
        services = await Service(
            itinerary=itinerary.id, travel_date=self.date, time_depart=self.time_depart).find_by_example(db)
        if self.max_duration is None:
            return services
        return [service for service in services if service.duration is not None and service.duration <= self.max_duration]

    async def _get_service(self, db: Connection) -> Service:
        if (n := len(services := await self._get_services(db))) == 0:
            raise HTTPException(status_code=404, detail='Service not found')
        if (n > 1) and (m := len(set([s.id_itinerary for s in services]))) > 1:
            logger.warning(f"Multiple itineraries ({n}) for {m} services found for "
                           f"{self.company} {self.depart_from} {self.arrive_at} {self.date} {self.time_depart}")
        return services[0]

    async def _get_service_template(self, db: Connection) -> Service:
        service = await self._get_service(db)
        return Service(itinerary=service.id_itinerary, travel_date=service.date, time_depart=service.time_depart)

    async def list_values(self, db: Connection, name: str) -> list[str]:
        if self.company is None:
            raise HTTPException(status_code=400, detail="Company is required")
        match name:
            case 'departures':
                return await Itinerary(company=self.company).find_distinct_by_attribute(db, 'depart_from')
            case 'arrivals':
                if self.depart_from is None:
                    raise HTTPException(status_code=400, detail="Depart from is required")
                return await Itinerary(company=self.company, depart_from=self.depart_from).find_distinct_by_attribute(db, 'arrive_at')
            case 'dates':
                itinerary = await self._get_itinerary(db)
                return [d.strftime('%Y-%m-%d') for d in await Service(itinerary.id).find_distinct_by_attribute(db, 'date')]
            case 'services':
                return sorted({service.time_depart for service in await self._get_services(db)})
            case 'classes':
                codes: set[str] = set()
                self.time_depart = None
                for service in await self._get_services(db):
                    codes.update(await PriceClass(service.id).find_distinct_by_attribute(db, 'class_code'))
                return sorted(codes)
            case _:
                raise HTTPException(status_code=404, detail=f"List '{name}' not found")

    async def delete_entries(self, db: Connection, name: str) -> dict[str, set[int]]:
        if self.company is None:
            raise HTTPException(status_code=400, detail="Company is required")
        counter = DeleteCounter()
        match name:
            case 'departures':
                if self.depart_from is None:
                    raise HTTPException(status_code=400, detail="Depart from is required")
                for itinerary in await Itinerary(company=self.company, depart_from=self.depart_from).find_by_example(db):
                    await itinerary.delete(db, counter)
            case 'arrivals':
                if self.depart_from is None:
                    raise HTTPException(status_code=400, detail="Depart from is required")
                if self.arrive_at is None:
                    raise HTTPException(status_code=400, detail="Arrive at is required")
                for itinerary in await Itinerary(company=self.company, depart_from=self.depart_from, arrive_at=self.arrive_at).find_by_example(db):
                    await itinerary.delete(db, counter)
            case 'dates':
                itinerary = await self._get_itinerary(db)
                if self.date is None:
                    raise HTTPException(status_code=400, detail="Date is required")
                for service in await Service(itinerary.id, travel_date=self.date).find_by_example(db):
                    await service.delete(db, counter)
            case 'services':
                service = await self._get_service(db)
            case 'classes':
                service = await self._get_service(db)
                if self.class_code is None:
                    raise HTTPException(status_code=400, detail='Class or fare is not specified')
                for pc in await PriceClass(service.id, class_code=self.class_code).find_by_example(db):
                    await pc.delete(db, counter)
            case _:
                raise HTTPException(status_code=404, detail=f"List '{name}' not found")
        return counter.items

    async def list_prices(self, db: Connection) -> dict:
        if self.company is None:
            raise HTTPException(status_code=400, detail="Company is required")
        service = await self._get_service(db)
        if self.class_code is None:
            raise HTTPException(status_code=400, detail='Class is not specified')
        prices: list[PriceClass] = await PriceClass(service.id, class_code=self.class_code).find_by_example(db)
        return {
            "prices": sorted(set([DatePrice(p.timestamp, p.euro) for p in prices])),
            "duration": service.duration
        }

    async def list_prices_of_day(self, db: Connection) -> dict[str, list[tuple[str, float]]]:
        if self.company is None:
            raise HTTPException(status_code=400, detail="Company is required")
        await self._get_itinerary(db)
        if self.date is None:
            raise HTTPException(status_code=400, detail='Date is required')
        if self.class_code is None:
            raise HTTPException(status_code=400, detail='Class is not specified')
        prices_of_day: dict[str, list[tuple[str, float]]] = {}
        for depart_at in await self.list_values(db, 'services'):
            query = self.model_copy(update={'time_depart': depart_at})
            for service in await query._get_services(db):
                prices: list[PriceClass] = await PriceClass(service.id, class_code=self.class_code).find_by_example(db)
                prices_of_day[depart_at] = sorted(set([DatePrice(p.timestamp, p.euro) for p in prices]))
        return prices_of_day

    async def service_code(self, db: Connection) -> dict[str, str | bool | None]:
        service = await self._get_service(db)
        return {
            "code": service.code,
            "available_classes": service.get_available_classes(),
        }

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # 1x1 transparent GIF to satisfy browser favicon requests.
    return Response(
        content=b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02L\x01\x00;",
        media_type="image/gif",
    )


@app.get("/tables")
async def list_tables():
    """List all tables in the database."""
    try:
        async with get_db() as db:
            return await list_names(db, 'tables')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/companies")
async def list_companies():
    """List all company types in the database: trains, flights."""
    try:
        async with get_db() as db:
            return await Itinerary().find_distinct_by_attribute(db, 'company')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/list")
async def list_prices(payload: QueryParameters):
    """List all train prices for a given full query set."""
    logger.info(f"/api/list/prices/{payload}")
    try:
        async with get_db() as db:
            return await payload.list_prices(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/list/day")
async def list_prices_of_day(payload: QueryParameters):
    """List all train prices for a given company type for a specific day in the database."""
    logger.info(f"/api/list/day_prices/{payload}")
    try:
        async with get_db() as db:
            return await payload.list_prices_of_day(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/service-code")
async def get_service_code(payload: QueryParameters):
    """Return the code of the selected service."""
    try:
        async with get_db() as db:
            return await payload.service_code(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/list/{name}")
async def list_values(name: str, payload: QueryParameters):
    """
    List all values for a given company type and day in the database. Name can be one of the following:
        departures - list locations: depart from
        arrivals   - list locations: arrive at
        dates      - list dates: departing date
        services   - list services of the day
        classes    - list price classes of a service or a set of services.
    """
    logger.info(f"/api/list/{name}/{payload}")
    try:
        async with get_db() as db:
            return await payload.list_values(db, name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/delete/{name}")
async def delete_entries(name: str, payload: QueryParameters) -> dict[str, set[int]]:
    """Delete all entries for a given name in the database, see previous command."""
    try:
        async with get_db(read_only=False) as db:
            return await payload.delete_entries(db, name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/ignav")
async def get_ignav_counters():
    try:
        async with get_db() as db:
            months: dict[str, dict[str, int]] = {}
            for item in await IgnavQueries().find_all(db):
                if (month := item.created_at.strftime("%Y-%m")) not in months:
                    months[month] = {'success': 0, 'failed': 0}
                attr = 'success' if item.outcome == 1 else 'failed'
                months[month][attr] += 1
            return months
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
