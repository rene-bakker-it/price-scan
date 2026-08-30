# price-scan

Price Scan collects and serves travel prices from:

- **Train scrapers** for  [Trenitalia Frecce](https://www.trenitalia.com/) and [Italo](https://www.italotreno.it/)
- **Flight fare retrieval** through the [Ignav API](https://www.ignav.com/)

A **FastAPI** web interface exposes the stored results and renders the UI for browsing them.

The project is intended to scrape specific destinations over a period of time to investigate
the price of tickets and how they change over time. It is not intended to be a general-purpose 
travel search engine, or provide robust performance.

The train-prices are scraped using [Playwright](https://playwright.dev/) and is highly unstable. 
Any change to the web-sites might brake the script. The flight prices are retrieved using the 
[Ignav API](https://www.ignav.com/) and require a subscription. At the time of writing the first
1000 scans per month are free. Beyond that you need to pay for the service. 
See [Ignav pricing](https://www.ignav.com/pricing) for details.

## Components

- `app/frecce.py` - scraper for Trenitalia Frecce routes
- `app/italo.py` - scraper for Italo routes
- `app/flights.py` - flight fare retriever using Ignav
- `app/main.py` - FastAPI application and web interface
- `resources/scan.sh` - helper script to launch train or flight scans

## Data storage

Collected data is stored in a local **SQLite** database under `resources/data.db`.
Inside a docker container, the database is persisted in a volume mounted at `/data`.
Use the `docker cp` command to copy the database out of the container if you want 
to analyze it, or modify it, locally.

```bash
# find the image:
docker ps
# expected result:
# CONTAINER ID   IMAGE        .......       
# 51d9479036e8   price-scan   

# copy the database out of the container:
docker cp 51d9479036e8:/data/data.db ./data.db  
```

## Stack
Python 3.14+, Playwright, aiohttp, aiosqlite, FastAPI, Jinja2

## Installing local

```bash
# In this directory
uv sync

# for frecce.py and italo.py
playwright install firefox

# for flights.py -> add the Ignav access token
export IGNAV_API_KEY=your_key
# or add it the the environtment variables of the system.
```

## Running

Run the web app with your preferred FastAPI / Uvicorn command, or use Docker Compose:

```bash
docker compose up --build
```

The container exposes the web interface on port `80` Change this number if it conflicts with
other services.

To run the server locally:
```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir ./app
```

If the sqlite database is not found, a new one will be created automatically.

To populate the database use one of the scanning programs:
```bash
cd app

# scan tren-italia
uv run frecce.py "Roma Termini" "Milano Centrale" 2026-10-10 [--headless]
# scan italo
uv run italo.py  "Roma Termini" "Milano Centrale" 2026-10-10 [--headless]
# scan a flight, use IATA airport codes
uv run flights.py FCO BRU 2026-10-10 
```