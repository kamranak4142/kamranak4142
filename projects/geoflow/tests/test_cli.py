import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from geoflow.cli import main


class CliTests(unittest.TestCase):
    def test_validation_exit_codes_and_report(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            source = Path(directory) / "input.csv"
            report = Path(directory) / "report.json"
            source.write_text("latitude,longitude\n0,0\n", encoding="utf-8")
            self.assertEqual(main(["validate", str(source), "--report", str(report)]), 1)
            self.assertEqual(json.loads(report.read_text())["summary"]["invalid"], 1)
            self.assertEqual(main(["validate", str(source), "--allow-zero"]), 0)

    def test_export_does_not_overwrite_input_or_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            source = Path(directory) / "input.csv"
            target = Path(directory) / "points.geojson"
            original = "latitude,longitude\n10,20\n"
            source.write_text(original)
            self.assertEqual(main(["export", str(source), "--output", str(target)]), 0)
            self.assertEqual(main(["export", str(source), "--output", str(target)]), 2)
            self.assertEqual(main(["validate", str(source), "--report", str(source), "--force"]), 2)
            self.assertEqual(source.read_text(), original)


if __name__ == "__main__":
    unittest.main()
