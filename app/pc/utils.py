import argparse
import re
from datetime import datetime, time
from pathlib import Path

import yaml


class MinMaxPrice:
    def __init__(self):
        self.min_price: int | None = None
        self.max_price: int | None = None

    def update(self, price: int | None):
        if price is None:
            return
        if self.min_price is None or price < self.min_price:
            self.min_price = price
        if self.max_price is None or price > self.max_price:
            self.max_price = price

    @property
    def valid(self) -> bool:
        return self.min_price is not None and self.max_price is not None


def search_name(s: str) -> str:
    return re.sub(r'[\W_]+', ' ', s.replace('-', ' ').replace('_', ' ')).strip().lower()


airports_file = Path(__file__).parent.parent / "resources" / "airports.yaml"
with open(airports_file) as fp:
    airports = yaml.safe_load(fp)
for v in airports.values():
    v['search'] = [s for s in search_name(v['name']).split() if len(s) > 2]


def find_airport(name: str) -> str:
    if name is None:
        raise ValueError('The airport was not specified.')

    s_name = search_name(name)
    if len(s_name) == 0:
        raise ValueError('The airport cannot be an empty string.')
    if (len(s_name) == 3) and (s_name.upper() in airports):
        return s_name.upper()

    found_airports: list[str] = []
    for n in [_n for _n in s_name.split() if len(_n) > 2]:
        if len(codes := set([k for k, v in airports.items() if n in v['search']])) == 0:
            if len(found_airports) == 0:
                continue
            found_airports = []
            break
        if len(found_airports) == 0:
            found_airports = list(codes)
        else:
            found_airports = list(set(found_airports) & codes)
            if len(found_airports) == 0:
                break
    if (n:= len(found_airports)) == 0:
        raise ValueError(f'No airport found: {name}')
    if n > 1:
        msg = 'Multiple airports: ' + ', '.join(found_airports)
        raise ValueError(msg)

    return found_airports[0]


def positive_int(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError(f"passengers must be a positive integer, got {count}.")
    return count


def parse_time(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must be in HH:MM format.") from exc

def parse_airport(value: str) -> str:
    try:
        return find_airport(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc

if __name__ == '__main__':
    for name in ['AMS', 'Rhodes', 'Rome', 'FCO', 'San Francisco', 'SFO', 'New York', 'JFK', 'LGA']:
        try:
            code = find_airport(name)
            print(f"{name} -> {code} ({airports[code]['name']})")
        except ValueError as e:
            print(f"{name} -> {e}")

