import csv
import io
import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from geoflow.core import Columns, analyze, parse_csv
from geoflow.export import csv_bytes, geojson_bytes, geopackage_bytes, report_bytes


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.table = parse_csv('id,latitude,longitude,note\n1,51.5,-0.12,"اسلام"\n2,0,0,missing\n')
        self.report = analyze(self.table, Columns("latitude", "longitude", "id"))

    def test_geojson_has_lon_lat_and_excludes_invalid_points(self):
        data = json.loads(geojson_bytes(self.report))
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["geometry"]["coordinates"], [-0.12, 51.5])
        self.assertEqual(data["features"][0]["properties"]["note"], "اسلام")
        self.assertEqual(data["features"][0]["properties"]["_geoflow_status"], "ready")

    def test_csv_keeps_all_rows_with_audit_status(self):
        rows = list(csv.DictReader(io.StringIO(csv_bytes(self.table, self.report).decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["_geoflow_status"], "invalid")

    def test_csv_formula_strings_are_neutralized_in_values_and_headers(self):
        table = parse_csv('latitude,longitude,=header,note\n51.5,-0.12,"=2+2"," @SUM(1)"\n')
        rows = list(csv.reader(io.StringIO(csv_bytes(table, analyze(table, Columns("latitude", "longitude"))).decode("utf-8-sig"))))
        self.assertEqual(rows[0][2], "'=header")
        self.assertEqual(rows[1][1], "-0.12")
        self.assertEqual(rows[1][2], "'=2+2")
        self.assertEqual(rows[1][3], "' @SUM(1)")

    def test_report_excludes_unneeded_source_attributes(self):
        report = json.loads(report_bytes(self.report))
        self.assertNotIn("values", report["rows"][0])
        self.assertEqual(report["summary"]["invalid"], 1)

    def test_geopackage_metadata_and_geometry_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.gpkg"
            path.write_bytes(geopackage_bytes(self.table, self.report))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], 0x47504B47)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10400)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("SELECT geometry_type_name, srs_id FROM gpkg_geometry_columns").fetchone(), ("POINT", 4326))
                blob, note = db.execute("SELECT geom, note FROM points").fetchone()
                self.assertEqual(blob[:4], b"GP\x00\x01")
                self.assertEqual(struct.unpack("<i", blob[4:8])[0], 4326)
                self.assertEqual(struct.unpack("<BIdd", blob[8:]), (1, 1, -0.12, 51.5))
                self.assertEqual(note, "اسلام")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM points").fetchone()[0], 1)

    def test_geopackage_handles_reserved_and_quoted_attribute_names(self):
        table = parse_csv('latitude,longitude,fid,geom,source_fid,"a""b"\n1,2,source-id,source-shape,keep,quoted\n')
        data = geopackage_bytes(table, analyze(table, Columns("latitude", "longitude")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attributes.gpkg"
            path.write_bytes(data)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT source_fid, source_geom, source_source_fid, "a""b" FROM points').fetchone(),
                                 ("source-id", "source-shape", "keep", "quoted"))

    def test_empty_geopackage_is_a_valid_empty_point_layer(self):
        table = parse_csv("latitude,longitude\n0,0\n")
        data = geopackage_bytes(table, analyze(table, Columns("latitude", "longitude")))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.gpkg"
            path.write_bytes(data)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM points").fetchone()[0], 0)
                self.assertIsNone(db.execute("SELECT min_x FROM gpkg_contents").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
