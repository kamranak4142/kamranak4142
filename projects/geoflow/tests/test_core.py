import unittest

from geoflow.core import Columns, Table, analyze, image_identity, interpolate, parse_csv, run_pipeline


class ParsingTests(unittest.TestCase):
    def test_unicode_quoted_fields_and_bom(self):
        table = parse_csv('\ufeffid,latitude,longitude,note\n1,51.5,-0.12,"اسلام, sample"\n')
        self.assertEqual(table.records[0]["note"], "اسلام, sample")

    def test_ambiguous_or_reserved_headers_are_rejected(self):
        for text in ["lat,LAT\n1,2", "lat,,lon\n1,2,3", "_geoflow_row,lat\n1,2"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_csv(text)

    def test_ragged_and_empty_csvs_are_rejected(self):
        for text in ["lat,lon\n1,2,3", "lat,lon\n1", "lat,lon\n", "", 'lat,lon\n"unterminated,2']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_csv(text)


class ValidationTests(unittest.TestCase):
    def test_nonfinite_range_and_swap_issues(self):
        table = parse_csv("latitude,longitude\nNaN,0\nInfinity,0\n151,-33\n20,181\n51.5,-0.12\n")
        report = analyze(table, Columns("latitude", "longitude"))
        self.assertEqual(report["summary"]["valid"], 1)
        self.assertEqual(report["issue_counts"]["non_numeric"], 2)
        self.assertEqual(report["issue_counts"]["possible_swap"], 1)

    def test_zero_pair_can_be_a_valid_location(self):
        table = parse_csv("latitude,longitude\n0,0\n0,67\n51,0\n")
        columns = Columns("latitude", "longitude")
        self.assertEqual(analyze(table, columns)["summary"]["valid"], 2)
        self.assertEqual(analyze(table, columns, False)["summary"]["valid"], 3)

    def test_duplicates_warn_without_discarding_valid_points(self):
        table = parse_csv("id,latitude,longitude\na,10,20\na,10,20\n")
        report = analyze(table, Columns("latitude", "longitude", "id"))
        self.assertEqual(report["summary"]["valid"], 2)
        self.assertEqual(report["summary"]["warning_rows"], 2)
        self.assertTrue(all(row["status"] == "warning" for row in report["rows"]))

    def test_column_mapping_must_be_unambiguous(self):
        table = parse_csv("lat,lon\n1,2")
        for columns in [Columns("lat", "lat"), Columns("missing", "lon")]:
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                analyze(table, columns)


class InterpolationTests(unittest.TestCase):
    columns = Columns("latitude", "longitude", filename="filename")

    def table(self, records):
        return Table(["filename", "latitude", "longitude"], [dict(zip(["filename", "latitude", "longitude"], row)) for row in records])

    def test_unsorted_rows_use_sequence_fraction_and_original_anchors(self):
        table = self.table([("cam_0004.jpg", "10.0003", "20.0003"), ("cam_0002.jpg", "0", "0"),
                            ("cam_0001.jpg", "10", "20"), ("cam_0003.jpg", "", "")])
        repaired, changes, skipped = interpolate(table, self.columns)
        self.assertEqual(len(changes), 2)
        self.assertEqual(skipped, [])
        self.assertAlmostEqual(float(repaired.records[1]["latitude"]), 10.0001)
        self.assertAlmostEqual(float(repaired.records[3]["longitude"]), 20.0002)
        self.assertTrue(all(change["before_row"] == 3 and change["after_row"] == 1 for change in changes))
        self.assertEqual(table.records[1]["latitude"], "0", "The input must stay unchanged.")

    def test_different_camera_prefixes_do_not_mix(self):
        table = self.table([("front_0001.jpg", "10", "20"), ("front_0002.jpg", "0", "0"), ("rear_0003.jpg", "10.0002", "20.0002")])
        _, changes, _ = interpolate(table, self.columns)
        self.assertEqual(changes, [])

    def test_folder_and_additional_group_boundaries_are_respected(self):
        table = self.table([("a/cam_1.jpg", "10", "20"), ("a/cam_2.jpg", "0", "0"), ("b/cam_3.jpg", "10.0002", "20.0002")])
        self.assertEqual(interpolate(table, self.columns)[1], [])
        headers = [*table.headers, "collection"]
        records = [{**record, "collection": "B" if index == 2 else "A"} for index, record in enumerate(table.records)]
        for record in records:
            record["filename"] = record["filename"].split("/")[-1]
        columns = Columns("latitude", "longitude", filename="filename", groups=("collection",))
        self.assertEqual(interpolate(Table(headers, records), columns)[1], [])

    def test_unbounded_and_invalid_values_are_left_unchanged(self):
        table = self.table([("cam_1.jpg", "0", "0"), ("cam_2.jpg", "10", "20"), ("cam_3.jpg", "bad", "20"),
                            ("cam_4.jpg", "10.0002", "20.0002"), ("cam_5.jpg", "", "")])
        repaired, changes, skipped = interpolate(table, self.columns)
        self.assertEqual(changes, [])
        self.assertEqual(len(skipped), 2)
        self.assertEqual(repaired.records, table.records)

    def test_sequence_and_distance_limits_prevent_estimates(self):
        table = self.table([("cam_1.jpg", "10", "20"), ("cam_2.jpg", "0", "0"), ("cam_9.jpg", "10.0001", "20.0001")])
        self.assertEqual(interpolate(table, self.columns, max_gap=3)[1], [])
        table = self.table([("cam_1.jpg", "10", "20"), ("cam_2.jpg", "0", "0"), ("cam_3.jpg", "11", "21")])
        self.assertEqual(interpolate(table, self.columns, max_distance=250)[1], [])

    def test_duplicate_image_sequences_are_ambiguous(self):
        table = self.table([("cam_1.jpg", "10", "20"), ("cam_1.JPG", "10", "20"),
                            ("cam_2.jpg", "0", "0"), ("cam_3.jpg", "10.0001", "20.0001")])
        _, changes, skipped = interpolate(table, self.columns)
        self.assertEqual(changes, [])
        self.assertIn("duplicate", skipped[0]["reason"])

    def test_short_antimeridian_crossing(self):
        table = self.table([("cam_1.jpg", "0", "179.999"), ("cam_2.jpg", "", ""), ("cam_3.jpg", "0", "-179.999")])
        repaired, changes, _ = interpolate(table, self.columns, max_distance=500)
        self.assertEqual(len(changes), 1)
        self.assertAlmostEqual(abs(float(repaired.records[1]["longitude"])), 180)

    def test_url_queries_and_windows_paths(self):
        first = image_identity("https://example.invalid/front/cam_0001.jpg?signature=placeholder")
        second = image_identity("https://example.invalid/front/cam_0002.jpg")
        self.assertEqual(first[0], second[0])
        self.assertEqual(image_identity(r"C:\samples\front\cam_0003.jpg")[1], 3)
        self.assertIsNone(image_identity("image-without-sequence.jpg"))
        self.assertIsNone(image_identity("https://[invalid/cam_2.jpg"))

    def test_estimates_are_marked_in_the_pipeline(self):
        table = self.table([("cam_1.jpg", "10", "20"), ("cam_2.jpg", "", ""), ("cam_3.jpg", "10.0002", "20.0002")])
        _, report = run_pipeline(table, self.columns, repair=True)
        self.assertEqual(report["rows"][1]["status"], "estimated")
        self.assertEqual(report["summary"]["estimated"], 1)

    def test_invalid_limits_are_rejected(self):
        table = self.table([("cam_1.jpg", "10", "20")])
        for gap, distance in [(0, 250), (True, 250), (3, float("nan")), (3, 0), (3, 20000)]:
            with self.subTest(gap=gap, distance=distance), self.assertRaises(ValueError):
                interpolate(table, self.columns, max_gap=gap, max_distance=distance)


if __name__ == "__main__":
    unittest.main()
