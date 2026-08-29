#!/usr/bin/env -S uv run --script

"""
Transform an Excel file containing airport data into a YAML file.
KEY: IATA code, VALUE: airport name
"""

from pathlib import Path

import yaml
from openpyxl import load_workbook


def main(excel_file: Path, yaml_file: Path):
    wb = load_workbook(filename=excel_file, read_only=True)
    ws = wb.worksheets[0]
    header: dict[str, int] = {}
    data: dict[str, dict[str, str | int | None]] = {}
    for row in ws.iter_rows(values_only=True):
        if len(header) == 0:
            header = dict([(str(c), x) for x, c in enumerate(row, start=0)])
            continue
        if row[header['type']] != 'large_airport':
            continue
        if (code := row[header['iata_code']]) in data:
            print(f"Duplicate IATA code: {code}")
            continue
        if code is None:
            continue
        if isinstance(elevation_ft := row[header['elevation_ft']], int):
            elevation = int(round(0.3048*elevation_ft))
        else:
            elevation = None
        data[code] = {
            'name': row[header['name']],
            'country': row[header['iso_country']],
            'region': row[header['iso_region']],
            'latitude': row[header['latitude_deg']],
            'longitude': row[header['longitude_deg']],
            'elevation': elevation,
        }
    with open(yaml_file, 'w') as fp:
        yaml.dump(data, fp, sort_keys=True, allow_unicode=True)
    print(f'Done. Found {len(data)} airports.!')

if __name__ == "__main__":    
    i_file = Path(__file__).parent.parent / "resources" / "airports.xlsx"
    o_file = Path(__file__).parent.parent / "app" / "resources" / "airports.yaml"
    main(i_file, o_file)
