"""
https://ignav.com/playground
"""

import asyncio

import aiohttp


async def main():
    async with aiohttp.ClientSession() as session:
        async with session.post(
                "https://ignav.com/api/fares/one-way",
                headers={"X-Api-Key": "YOUR_API_KEY"},
                json={
                    "origin": "FCO",
                    "destination": "AMS",
                    "departure_date": "2026-09-22",
                    "market": "IT"
                },
        ) as response:
            print(await response.json())

asyncio.run(main())
