"""Starts the real HTTP API (wsgiref) on a free port and talks to it over HTTP."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_handler import ReservationApi, make_standalone_server  # noqa: E402
from policy_store import LogOnlyDataPlane, ReservationManager  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plane = LogOnlyDataPlane()
        cls.mgr = ReservationManager(
            cls.plane, 10, {"C1": "10.0.0.1", "C2": "10.0.0.2"})
        cls.server = make_standalone_server(ReservationApi(cls.mgr), "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def setUp(self):
        for r in list(self.mgr.list()):
            self.mgr.release(r["reservation_id"])

    def test_health(self):
        code, body = self.call("GET", "/api/health")
        self.assertEqual((code, body["status"]), (200, "ok"))

    def test_reserve_message_from_brief(self):
        code, body = self.call("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C1",
            "reservation_id": "R001", "bandwidth": 4})
        self.assertEqual(code, 200)
        self.assertEqual(body["reservation"]["bandwidth_mbps"], 4)
        self.assertEqual(body["reservation"]["client_ip"], "10.0.0.1")
        code, body = self.call("GET", "/api/capacity")
        self.assertEqual(body["available_mbps"], 6)

    def test_release_message_from_brief(self):
        self.call("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C1",
            "reservation_id": "R001", "bandwidth": 4})
        code, body = self.call("POST", "/api/reservation", {
            "action": "release", "reservation_id": "R001"})
        self.assertEqual((code, body["status"]), (200, "ok"))
        self.assertEqual(self.call("GET", "/api/reservations")[1]["reservations"], [])

    def test_rejected_over_capacity(self):
        self.call("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C1",
            "reservation_id": "R001", "bandwidth": 8})
        code, body = self.call("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C2",
            "reservation_id": "R002", "bandwidth": 4})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "insufficient_capacity")
        self.assertEqual(self.call("GET", "/api/reservations/R002")[0], 404)

    def test_update_and_delete_endpoints(self):
        self.call("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C1",
            "reservation_id": "R001", "bandwidth": 2})
        code, body = self.call("POST", "/api/reservation", {
            "action": "update", "reservation_id": "R001", "bandwidth": 5})
        self.assertEqual(body["reservation"]["bandwidth_mbps"], 5)
        self.assertEqual(self.call("DELETE", "/api/reservations/R001")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/reservations/R001")[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/api/reservation", raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/reservation", {"action": "dance"})[0], 400)
        self.assertEqual(self.call("POST", "/api/reservation", {"action": "reserve"})[0], 400)
        self.assertEqual(self.call("POST", "/api/reservation", [1, 2])[0], 400)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)

    def test_reject_action_logs_without_allocating(self):
        code, body = self.call("POST", "/api/reservation", {
            "action": "reject", "client_id": "C2", "reservation_id": "R050",
            "reason": "not enough bandwidth"})
        self.assertEqual((code, body["allocated"]), (200, False))
        self.assertEqual(self.call("GET", "/api/capacity")[1]["used_mbps"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
