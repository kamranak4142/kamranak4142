"""GeoJSON, CSV and a minimal 2D POINT GeoPackage writer."""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import struct
import tempfile
from pathlib import Path

from .core import Table

WGS84_WKT = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
             'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]')


def geojson_bytes(report: dict) -> bytes:
    features = []
    for row in report["rows"]:
        if not row["usable"]:
            continue
        properties = {**row["values"], "_geoflow_row": row["row"], "_geoflow_status": row["status"]}
        features.append({"type": "Feature", "properties": properties,
                         "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]}})
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2,
                      allow_nan=False, ensure_ascii=False).encode("utf-8")


def spreadsheet_safe(value: object) -> str:
    """Neutralize formula-like strings in CSV exports; preserve finite numerics."""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        try:
            if math.isfinite(float(text)):
                return text
        except ValueError:
            pass
        return "'" + text
    return text


def csv_bytes(table: Table, report: dict) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([*(spreadsheet_safe(header) for header in table.headers), "_geoflow_row", "_geoflow_status", "_geoflow_issues"])
    for record, row in zip(table.records, report["rows"]):
        writer.writerow([*(spreadsheet_safe(record[header]) for header in table.headers), row["row"],
                         row["status"], "; ".join(issue["code"] for issue in row["issues"])])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def report_bytes(report: dict) -> bytes:
    """A portable audit with issue records, estimates and skipped reasons."""
    audit = {**report, "rows": [{key: value for key, value in row.items() if key != "values"}
                                for row in report["rows"]]}
    return json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def attribute_names(headers: list[str]) -> list[str]:
    used = {"fid", "geom", "_geoflow_row", "_geoflow_status"}
    names = []
    for header in headers:
        candidate = header
        while candidate.casefold() in used:
            candidate = "source_" + candidate
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def geopackage_bytes(table: Table, report: dict) -> bytes:
    """Write EPSG:4326 2D points using the GeoPackage 1.4 binary encoding.

    This writer deliberately supports a single point layer, without R-tree or
    vendor extensions. Coordinate transforms and other geometry types are out
    of scope. Original attribute values are stored as TEXT.
    """
    ready = [row for row in report["rows"] if row["usable"]]
    with tempfile.TemporaryDirectory(prefix="geoflow-") as directory:
        path = Path(directory) / "points.gpkg"
        connection = sqlite3.connect(path)
        try:
            connection.executescript("""
                PRAGMA application_id = 1196444487;
                PRAGMA user_version = 10400;
                PRAGMA foreign_keys = ON;
                CREATE TABLE gpkg_spatial_ref_sys (
                    srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
                    organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
                    definition TEXT NOT NULL, description TEXT
                );
                CREATE TABLE gpkg_contents (
                    table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
                    identifier TEXT UNIQUE, description TEXT DEFAULT '',
                    last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
                    FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
                );
                CREATE TABLE gpkg_geometry_columns (
                    table_name TEXT NOT NULL, column_name TEXT NOT NULL,
                    geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
                    z TINYINT NOT NULL, m TINYINT NOT NULL,
                    PRIMARY KEY (table_name, column_name), UNIQUE (table_name),
                    FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
                    FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
                );
            """)
            connection.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)", [
                ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", "undefined Cartesian coordinate reference system"),
                ("Undefined Geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84", 4326, "EPSG", 4326, WGS84_WKT, "WGS 84 longitude / latitude"),
            ])
            bounds = (min(row["longitude"] for row in ready), min(row["latitude"] for row in ready),
                      max(row["longitude"] for row in ready), max(row["latitude"] for row in ready)) if ready else (None,) * 4
            connection.execute("""INSERT INTO gpkg_contents
                (table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id)
                VALUES ('points', 'features', 'points', 'GeoFlow validated points', ?, ?, ?, ?, 4326)""", bounds)
            connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('points', 'geom', 'POINT', 4326, 0, 0)")
            names = attribute_names(table.headers)
            columns_sql = ", ".join(quote_identifier(name) + " TEXT" for name in names)
            connection.execute('CREATE TABLE points (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT, '
                               + columns_sql + ', "_geoflow_row" INTEGER, "_geoflow_status" TEXT)')
            attributes = ["geom", *names, "_geoflow_row", "_geoflow_status"]
            placeholders = ",".join("?" for _ in attributes)
            statement = f'INSERT INTO points ({",".join(quote_identifier(name) for name in attributes)}) VALUES ({placeholders})'
            for row in ready:
                # GP, version 0, little endian, no envelope, not empty; then SRS.
                header = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326)
                wkb = struct.pack("<BIdd", 1, 1, row["longitude"], row["latitude"])
                connection.execute(statement, [header + wkb, *(row["values"][name] for name in table.headers),
                                               row["row"], row["status"]])
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()
