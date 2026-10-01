"""Command-line interface shared with the dashboard's processing engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import Columns, parse_csv, run_pipeline
from .export import csv_bytes, geojson_bytes, geopackage_bytes, report_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geoflow", description="Local CSV GPS validation and GIS point exports.")
    parser.add_argument("--version", action="version", version=f"GeoFlow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local dashboard.")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    for name, help_text in [("validate", "Inspect coordinates and write a QA report."),
                            ("repair", "Estimate bounded missing GPS and write an audited CSV."),
                            ("export", "Export usable points as GeoJSON or GeoPackage.")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("input", type=Path)
        command.add_argument("--lat", default="latitude", help="Latitude column name.")
        command.add_argument("--lon", default="longitude", help="Longitude column name.")
        command.add_argument("--id", dest="identifier", default="", help="Optional identifier column.")
        command.add_argument("--filename", default="", help="Filename / URL column for image sequence grouping.")
        command.add_argument("--group", action="append", default=[], help="Additional grouping column; repeat as needed.")
        command.add_argument("--allow-zero", action="store_true", help="Treat (0,0) as a valid geographic location.")
        command.add_argument("--max-gap", type=int, default=3)
        command.add_argument("--max-distance", type=float, default=250.0, help="Maximum original anchor distance in meters.")
        command.add_argument("--report", type=Path, help="Write a JSON QA and repair audit.")
        command.add_argument("--force", action="store_true", help="Allow replacing an existing output file.")
        if name in {"repair", "export"}:
            command.add_argument("--output", type=Path, required=True)
        if name == "export":
            command.add_argument("--repair", action="store_true", help="Estimate eligible gaps before export.")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .server import serve
        try:
            return serve(args.port, not args.no_browser)
        except (OSError, ValueError) as error:
            parser.exit(2, f"GeoFlow: {error}\n")
    try:
        if args.input.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("The maximum CSV size is 5 MiB.")
        table = parse_csv(args.input.read_text(encoding="utf-8-sig"))
        columns = Columns(args.lat, args.lon, args.identifier, args.filename, tuple(args.group))
        table, report = run_pipeline(table, columns, not args.allow_zero,
                                     args.command == "repair" or getattr(args, "repair", False),
                                     args.max_gap, args.max_distance)
        writes = []
        if args.command == "repair":
            if args.output.suffix.lower() != ".csv":
                raise ValueError("Repair output must use a .csv extension.")
            writes.append((args.output, csv_bytes(table, report)))
        elif args.command == "export":
            extension = args.output.suffix.lower()
            if extension not in {".geojson", ".gpkg"}:
                raise ValueError("Export output must use a .geojson or .gpkg extension.")
            writes.append((args.output, geojson_bytes(report) if extension == ".geojson" else geopackage_bytes(table, report)))
        if args.report:
            writes.append((args.report, report_bytes(report)))
        targets = [path.resolve() for path, _ in writes]
        if len(set(targets)) != len(targets):
            raise ValueError("Output and report paths must be different.")
        if args.input.resolve() in targets:
            raise ValueError("Choose an output path different from the source CSV.")
        if not args.force and any(path.exists() for path, _ in writes):
            raise ValueError("An output already exists. Choose another path or pass --force.")
        for path, data in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        print(json.dumps(report["summary"], indent=2))
        # CI can distinguish invalid data from execution / configuration errors.
        return 1 if args.command == "validate" and report["summary"]["invalid"] else 0
    except (OSError, ValueError, UnicodeError) as error:
        print(f"GeoFlow: {error}", file=sys.stderr)
        return 2
