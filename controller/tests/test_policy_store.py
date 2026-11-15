import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from policy_store import (LogOnlyDataPlane, ReservationError,  # noqa: E402
                                 ReservationManager)

CLIENTS = {"C1": "10.0.0.1", "C2": "10.0.0.2", "C3": "10.0.0.3"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FailingPlane(LogOnlyDataPlane):
    fail_install = False
    fail_remove = False

    def install(self, res):
        if self.fail_install:
            raise RuntimeError("switch unreachable")
        return super().install(res)

    def remove(self, res):
        if self.fail_remove:
            raise RuntimeError("switch unreachable")
        return super().remove(res)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.plane = FailingPlane()
        self.mgr = ReservationManager(self.plane, 10, CLIENTS, max_duration=600,
                                      clock=self.clock)

    def err(self, fn, *a, **kw):
        with self.assertRaises(ReservationError) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def test_accept_installs_policy(self):
        out = self.mgr.reserve("R001", "C1", 4)
        self.assertEqual(out["client_ip"], "10.0.0.1")
        self.assertEqual(out["meter_id"], 1)
        self.assertEqual(self.plane.calls, [("install", "R001", 4)])
        self.assertEqual(self.mgr.status()["used_mbps"], 4)

    def test_reject_over_capacity_allocates_nothing(self):
        self.mgr.reserve("R001", "C1", 7)
        e = self.err(self.mgr.reserve, "R002", "C2", 4)
        self.assertEqual((e.http_status, e.code), (409, "insufficient_capacity"))
        self.assertEqual(e.extra["available_mbps"], 3)
        self.assertEqual(len(self.plane.calls), 1)      # only R001 was installed
        self.assertEqual(self.mgr.status()["used_mbps"], 7)

    def test_exact_capacity_is_accepted(self):
        self.mgr.reserve("R001", "C1", 6)
        self.mgr.reserve("R002", "C2", 4)
        self.assertEqual(self.mgr.status()["available_mbps"], 0)

    def test_release_removes_policy_and_frees_bandwidth(self):
        self.mgr.reserve("R001", "C1", 4)
        self.mgr.release("R001")
        self.assertEqual(self.plane.calls[-1], ("remove", "R001", 4))
        self.assertEqual(self.mgr.status()["used_mbps"], 0)
        self.mgr.reserve("R002", "C1", 4)     # client can reserve again

    def test_release_unknown_is_404(self):
        self.assertEqual(self.err(self.mgr.release, "nope").http_status, 404)

    def test_expiry_removes_policy(self):
        self.mgr.reserve("R001", "C1", 4, duration=30)
        self.clock.now += 29
        self.assertEqual(self.mgr.expire_due(), [])
        self.clock.now += 2
        self.assertEqual(self.mgr.expire_due(), ["R001"])
        self.assertEqual(self.plane.calls[-1][0], "remove")
        self.assertEqual(self.mgr.status()["active_count"], 0)
        self.assertEqual(self.mgr.stats["expired"], 1)

    def test_no_duration_never_expires(self):
        self.mgr.reserve("R001", "C1", 4)
        self.clock.now += 10 ** 6
        self.assertEqual(self.mgr.expire_due(), [])

    def test_update_changes_rate_and_checks_capacity(self):
        self.mgr.reserve("R001", "C1", 4)
        self.mgr.reserve("R002", "C2", 5)
        self.assertEqual(self.mgr.update("R001", 5)["bandwidth_mbps"], 5)
        e = self.err(self.mgr.update, "R001", 6)
        self.assertEqual(e.code, "insufficient_capacity")
        self.assertEqual(self.mgr.get("R001")["bandwidth_mbps"], 5)

    def test_duplicate_and_idempotent_reserve(self):
        self.mgr.reserve("R001", "C1", 4)
        again = self.mgr.reserve("R001", "C1", 4)      # TCP server retry
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.plane.calls), 1)
        e = self.err(self.mgr.reserve, "R001", "C2", 4)
        self.assertEqual(e.code, "duplicate_reservation")

    def test_one_reservation_per_client(self):
        self.mgr.reserve("R001", "C1", 2)
        e = self.err(self.mgr.reserve, "R002", "C1", 2)
        self.assertEqual(e.code, "client_already_reserved")

    def test_validation(self):
        for bw in (0, -1, "4", None, True, float("nan")):
            self.assertEqual(self.err(self.mgr.reserve, "R", "C1", bw).http_status, 400)
        self.assertEqual(self.err(self.mgr.reserve, "", "C1", 1).http_status, 400)
        self.assertEqual(self.err(self.mgr.reserve, "R", "C9", 1).code, "unknown_client")
        self.assertEqual(self.err(self.mgr.reserve, "R", "C1", 1, client_ip="bad").http_status, 400)
        self.assertEqual(self.err(self.mgr.reserve, "R", "C1", 1, duration=99999).http_status, 400)
        self.assertEqual(self.plane.calls, [])

    def test_explicit_client_ip(self):
        out = self.mgr.reserve("R001", "C9", 1, client_ip="10.0.0.9")
        self.assertEqual(out["client_ip"], "10.0.0.9")

    def test_dataplane_install_failure_rolls_back(self):
        self.plane.fail_install = True
        e = self.err(self.mgr.reserve, "R001", "C1", 4)
        self.assertEqual(e.http_status, 500)
        self.assertEqual(self.mgr.status()["active_count"], 0)

    def test_dataplane_remove_failure_keeps_reservation(self):
        self.mgr.reserve("R001", "C1", 4)
        self.plane.fail_remove = True
        self.assertEqual(self.err(self.mgr.release, "R001").http_status, 500)
        self.assertEqual(self.mgr.status()["active_count"], 1)
        self.plane.fail_remove = False
        self.mgr.release("R001")

    def test_meter_ids_are_reused(self):
        self.mgr.reserve("R001", "C1", 1)
        self.mgr.reserve("R002", "C2", 1)
        self.mgr.release("R001")
        self.assertEqual(self.mgr.reserve("R003", "C3", 1)["meter_id"], 1)

    def test_note_rejected_allocates_nothing(self):
        self.mgr.note_rejected("R009", "C2", "server said no")
        self.assertEqual(self.mgr.status()["used_mbps"], 0)
        self.assertEqual(self.plane.calls, [])
        self.assertEqual(self.mgr.events()[-1]["event"], "rejected_by_server")


if __name__ == "__main__":
    unittest.main(verbosity=2)
