import http.client
import json
import threading
import unittest

from geoflow.server import create_server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.headers))
        connection.close()
        return result

    def payload(self):
        return {"csv": "id,latitude,longitude\n1,51.5,-0.12\n2,0,0\n",
                "columns": {"latitude": "latitude", "longitude": "longitude", "identifier": "id"}}

    def post(self, path, payload, extra=None):
        return self.request("POST", path, json.dumps(payload), {"Content-Type": "application/json", "X-GeoFlow": "1", **(extra or {})})

    def test_dashboard_has_security_headers_and_local_assets(self):
        status, data, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"GeoFlow", data)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_arbitrary_files_are_not_served(self):
        for path in ["/.git/config", "/../README.md", "/geoflow/core.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_api_rejects_cross_origin_and_rebound_host(self):
        self.assertEqual(self.post("/api/analyze", self.payload(), {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.post("/api/analyze", self.payload(), {"Host": "example.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/analyze", "{}", {"Content-Type": "application/json"})[0], 403)

    def test_analysis_and_geojson_export_share_the_engine(self):
        status, data, _ = self.post("/api/analyze", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["summary"]["invalid"], 1)
        status, data, headers = self.post("/api/export", {**self.payload(), "format": "geojson"})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)["features"]), 1)
        self.assertIn("geoflow_points.geojson", headers["Content-Disposition"])

    def test_invalid_mapping_and_payload_types_return_readable_errors(self):
        payload = self.payload()
        payload["columns"]["latitude"] = "not-a-column"
        self.assertEqual(self.post("/api/analyze", payload)[0], 400)
        self.assertEqual(self.post("/api/analyze", {**self.payload(), "zero_missing": "false"})[0], 400)
        self.assertEqual(self.post("/api/analyze", ["not an object"])[0], 400)


if __name__ == "__main__":
    unittest.main()
