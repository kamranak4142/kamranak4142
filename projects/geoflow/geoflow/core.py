"""Deterministic CSV validation and bounded GPS interpolation.

Coordinates are WGS84 decimal degrees. No CRS guessing or network requests occur.
"""

from __future__ import annotations

import bisect
import csv
import io
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
RESERVED_PREFIX = "_geoflow_"


@dataclass(frozen=True)
class Table:
    headers: list[str]
    records: list[dict[str, str]]


@dataclass(frozen=True)
class Columns:
    latitude: str
    longitude: str
    identifier: str = ""
    filename: str = ""
    groups: tuple[str, ...] = ()

    def validate(self, table: Table) -> None:
        if not self.latitude or not self.longitude:
            raise ValueError("Choose both a latitude and a longitude column.")
        if self.latitude == self.longitude:
            raise ValueError("Latitude and longitude must use different columns.")
        selected = [self.latitude, self.longitude, self.identifier, self.filename, *self.groups]
        missing = [name for name in selected if name and name not in table.headers]
        if missing:
            raise ValueError("A selected column is not present in the dataset.")


def parse_csv(text: str) -> Table:
    """Parse UTF-8 CSV; reject ambiguous headers and malformed row lengths."""
    if not isinstance(text, str):
        raise ValueError("Supply CSV text encoded as UTF-8.")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("The maximum CSV size is 5 MiB.")
    try:
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), strict=True)
        raw_headers = next(reader, None)
        if not raw_headers:
            raise ValueError("The CSV has no header row.")
        headers = [name.strip() for name in raw_headers]
        if len(headers) > 100 or any(not name for name in headers):
            raise ValueError("Use 1–100 nonempty column names.")
        if len(set(name.casefold() for name in headers)) != len(headers):
            raise ValueError("Column names must be unique, ignoring case.")
        if any(name.casefold().startswith(RESERVED_PREFIX) for name in headers):
            raise ValueError("Column names starting with _geoflow_ are reserved for audit fields.")
        if any(any(ord(char) < 32 for char in name) for name in headers):
            raise ValueError("Column names cannot contain control characters.")
        records = []
        for fields in reader:
            if not fields:
                continue
            if len(fields) != len(headers):
                raise ValueError(f"CSV record {len(records) + 1} has a different number of fields than the header.")
            records.append(dict(zip(headers, fields)))
            if len(records) > MAX_ROWS:
                raise ValueError("The maximum dataset size is 50,000 records.")
        if not records:
            raise ValueError("The CSV has no data records.")
        return Table(headers, records)
    except csv.Error as error:
        raise ValueError("The CSV could not be parsed. Check quoting and field lengths.") from error


def number(value: str) -> float | None:
    try:
        parsed = float(value.strip())
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, AttributeError):
        return None


def position(record: dict[str, str], columns: Columns, zero_missing: bool) -> tuple[float, float] | None:
    lat, lon = number(record[columns.latitude]), number(record[columns.longitude])
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if zero_missing and lat == lon == 0:
        return None
    return lat, lon


def is_missing_pair(record: dict[str, str], columns: Columns, zero_missing: bool) -> bool:
    lat, lon = record[columns.latitude].strip(), record[columns.longitude].strip()
    return (not lat and not lon) or (zero_missing and number(lat) == number(lon) == 0)


def analyze(table: Table, columns: Columns, zero_missing: bool = True, repairs: list[dict] | None = None) -> dict:
    columns.validate(table)
    repairs = repairs or []
    estimated = {repair["row"] for repair in repairs}
    rows = []
    ids: dict[str, list[int]] = defaultdict(list)
    locations: dict[tuple[float, float], list[int]] = defaultdict(list)
    for index, record in enumerate(table.records):
        lat_text, lon_text = record[columns.latitude], record[columns.longitude]
        lat, lon = number(lat_text), number(lon_text)
        issues = []
        if is_missing_pair(record, columns, zero_missing):
            issues.append({"code": "missing_gps", "severity": "error", "message": "Both GPS values are missing."})
        else:
            for label, value, raw, bound in [("latitude", lat, lat_text, 90), ("longitude", lon, lon_text, 180)]:
                if not raw.strip():
                    issues.append({"code": "missing_coordinate", "severity": "error", "message": f"{label.capitalize()} is blank."})
                elif value is None:
                    issues.append({"code": "non_numeric", "severity": "error", "message": f"{label.capitalize()} must be a finite number."})
                elif abs(value) > bound:
                    issues.append({"code": "out_of_range", "severity": "error", "message": f"{label.capitalize()} is outside ±{bound}°."})
            if lat is not None and lon is not None and abs(lat) > 90 and abs(lat) <= 180 and abs(lon) <= 90:
                issues.append({"code": "possible_swap", "severity": "warning", "message": "The latitude and longitude may be swapped. Review the source."})
        valid_position = position(record, columns, zero_missing)
        if valid_position:
            locations[(round(valid_position[0], 7), round(valid_position[1], 7))].append(index)
        if columns.identifier and record[columns.identifier].strip():
            ids[record[columns.identifier].strip()].append(index)
        rows.append({"row": index + 1, "values": dict(record), "latitude": lat, "longitude": lon,
                     "issues": issues, "usable": valid_position is not None, "estimated": index + 1 in estimated})
    for buckets, code, message in [
        (ids, "duplicate_id", "Identifier appears on multiple rows."),
        (locations, "duplicate_location", "Coordinates match another row at 7 decimal places."),
    ]:
        for indexes in buckets.values():
            if len(indexes) > 1:
                for index in indexes:
                    rows[index]["issues"].append({"code": code, "severity": "warning", "message": message})
    for row in rows:
        row["status"] = ("invalid" if not row["usable"] else "estimated" if row["estimated"]
                         else "warning" if row["issues"] else "ready")
    issue_counts = Counter(issue["code"] for row in rows for issue in row["issues"])
    valid = sum(row["usable"] for row in rows)
    return {
        "crs": "EPSG:4326", "coordinate_order": "longitude, latitude in GIS exports",
        "columns": {"latitude": columns.latitude, "longitude": columns.longitude,
                    "identifier": columns.identifier, "filename": columns.filename, "groups": list(columns.groups)},
        "summary": {"total": len(rows), "valid": valid, "invalid": len(rows) - valid,
                    "flagged": sum(bool(row["issues"]) for row in rows),
                    "warning_rows": sum(any(issue["severity"] == "warning" for issue in row["issues"]) for row in rows),
                    "estimated": len(estimated), "quality_percent": round(100 * valid / len(rows), 1)},
        "issue_counts": dict(issue_counts), "rows": rows, "repairs": repairs,
    }


def image_identity(value: str) -> tuple[str, int] | None:
    """Keep folder, filename prefix and extension in the series identity."""
    normalized = value.strip().replace("\\", "/")
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    # Ignore a URL query string without requesting the URL.
    path = unquote(parsed.path) if parsed.scheme.lower() in {"http", "https"} else normalized
    folder, _, name = path.rpartition("/")
    match = re.fullmatch(r"(.*?)(\d+)(\.[^./]+)", name)
    if not match:
        return None
    origin = parsed.netloc if parsed.scheme.lower() in {"http", "https"} else ""
    series = "/".join([origin, folder, match[1], match[3]]).casefold()
    return series, int(match[2])


def distance_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def interpolate(table: Table, columns: Columns, zero_missing: bool = True,
                max_gap: int = 3, max_distance: float = 250.0) -> tuple[Table, list[dict], list[dict]]:
    """Fill eligible gaps using original anchors only; never extrapolate.

    Sequence numbers define the fraction, not CSV row order. Endpoints must be
    within max_distance and separated by at most max_gap missing sequence values.
    """
    columns.validate(table)
    if not columns.filename:
        raise ValueError("Choose a filename column before estimating missing GPS.")
    if isinstance(max_gap, bool) or not isinstance(max_gap, int) or not 1 <= max_gap <= 100:
        raise ValueError("Maximum gap must be an integer between 1 and 100.")
    if not math.isfinite(max_distance) or not 0 < max_distance <= 10_000:
        raise ValueError("Maximum anchor distance must be greater than 0 and at most 10,000 meters.")
    records = [dict(record) for record in table.records]
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    skipped, repairs = [], []
    for index, record in enumerate(table.records):
        identity = image_identity(record[columns.filename])
        if identity:
            series, seq = identity
            key = (series, *(record[column] for column in columns.groups))
            groups[key].append((seq, index))
        elif is_missing_pair(record, columns, zero_missing):
            skipped.append({"row": index + 1, "reason": "Filename has no numeric sequence before its extension."})
    for entries in groups.values():
        entries.sort()
        duplicates = len({seq for seq, _ in entries}) != len(entries)
        anchors = [(seq, index) for seq, index in entries if position(table.records[index], columns, zero_missing)]
        anchor_sequences = [seq for seq, _ in anchors]
        for seq, index in entries:
            original = table.records[index]
            if not is_missing_pair(original, columns, zero_missing):
                continue
            reason = ""
            at = bisect.bisect_left(anchor_sequences, seq)
            if duplicates:
                reason = "This series contains duplicate image sequence numbers."
            elif at == 0 or at == len(anchors):
                reason = "No original GPS anchor on both sides of this image."
            else:
                before_seq, before_index = anchors[at - 1]
                after_seq, after_index = anchors[at]
                if after_seq - before_seq - 1 > max_gap:
                    reason = "The sequence gap exceeds the configured limit."
                else:
                    before = position(table.records[before_index], columns, zero_missing)
                    after = position(table.records[after_index], columns, zero_missing)
                    assert before is not None and after is not None
                    span = distance_meters(before, after)
                    if span > max_distance:
                        reason = "The anchor distance exceeds the configured limit."
                    else:
                        fraction = (seq - before_seq) / (after_seq - before_seq)
                        lat = before[0] + (after[0] - before[0]) * fraction
                        # Use the shorter longitude direction across the antimeridian.
                        delta_lon = (after[1] - before[1] + 180) % 360 - 180
                        lon = (before[1] + delta_lon * fraction + 180) % 360 - 180
                        records[index][columns.latitude] = f"{lat:.10f}"
                        records[index][columns.longitude] = f"{lon:.10f}"
                        repairs.append({"row": index + 1, "before_row": before_index + 1, "after_row": after_index + 1,
                                        "before_sequence": before_seq, "after_sequence": after_seq,
                                        "fraction": round(fraction, 6), "anchor_distance_m": round(span, 2),
                                        "original_latitude": original[columns.latitude],
                                        "original_longitude": original[columns.longitude],
                                        "latitude": lat, "longitude": lon, "method": "linear_sequence_interpolation",
                                        "source": "estimated"})
            if reason:
                skipped.append({"row": index + 1, "reason": reason})
    return Table(table.headers, records), sorted(repairs, key=lambda item: item["row"]), sorted(skipped, key=lambda item: item["row"])


def run_pipeline(table: Table, columns: Columns, zero_missing: bool = True,
                 repair: bool = False, max_gap: int = 3, max_distance: float = 250.0) -> tuple[Table, dict]:
    repairs, skipped = [], []
    if repair:
        table, repairs, skipped = interpolate(table, columns, zero_missing, max_gap, max_distance)
    report = analyze(table, columns, zero_missing, repairs)
    report["skipped_repairs"] = skipped
    report["settings"] = {"zero_pair_is_missing": zero_missing, "repair_enabled": repair,
                          "max_gap": max_gap, "max_anchor_distance_m": max_distance}
    return table, report
