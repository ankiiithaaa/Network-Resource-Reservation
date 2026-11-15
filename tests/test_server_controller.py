#!/usr/bin/env python3
"""End-to-end test of the TCP server + Person 2's controller API (TC01-TC09).

Starts Person 2's mock_controller.py (same HTTP API as the Ryu controller, no
Mininet needed) and our server.py, then drives them with real TCP clients.
Runs on Windows/macOS/Linux:

    python tests/test_server_controller.py --controller-dir controller

On the Ubuntu VM you can point it at the real Ryu controller instead:
    python3 tests/test_server_controller.py --use-running-controller http://127.0.0.1:8081

Writes results/person1_test_results.csv with the ACTUAL outcome of every test.
"""
import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))
from client import Client  # noqa: E402

SERVER_PORT = 15000
API_PORT = 18081
results = []


def record(tc, desc, expected, actual, ok):
    results.append((tc, desc, expected, actual, "PASS" if ok else "FAIL"))
    print("%-5s %-4s %-45s expected=%s | actual=%s" % (tc, "PASS" if ok else "FAIL",
                                                      desc, expected, actual), flush=True)


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("nothing listening on port %s" % port)


def api(base, path):
    with urllib.request.urlopen(base + path, timeout=3) as r:
        return json.loads(r.read().decode())


def controller_ids(base):
    # the controller lists only its ACTIVE reservations
    return sorted(r["reservation_id"] for r in api(base, "/api/reservations")["reservations"])


def ask(req):
    c = Client("127.0.0.1", SERVER_PORT)
    try:
        return c.request(req)
    finally:
        c.close()


def reserve(cid, bw, dur):
    return ask({"type": "RESERVE", "client_id": cid, "bandwidth": bw, "duration": dur})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller-dir", default=os.path.join(ROOT, "controller"),
                    help="folder containing Person 2's mock_controller.py")
    ap.add_argument("--use-running-controller", metavar="URL",
                    help="use an already running controller instead of the mock")
    args = ap.parse_args()

    procs = []
    tmp = tempfile.mkdtemp()
    out_dir = os.path.join(ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)
    try:
        if args.use_running_controller:
            base = args.use_running_controller.rstrip("/")
        else:
            # Map test clients to fake IPs (all local clients share 127.0.0.1).
            cfg = {"api_host": "127.0.0.1", "api_port": API_PORT, "openflow_port": 6653,
                   "total_bandwidth_mbps": 10, "default_duration_sec": 0,
                   "max_duration_sec": 3600, "expiry_check_interval_sec": 1,
                   "enforce_direction": "src", "reserved_flow_priority": 100,
                   "client_map": {"C%d" % i: "10.0.0.%d" % i for i in range(1, 30)}}
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump(cfg, f)
            ctl_log = open(os.path.join(out_dir, "test_controller_log.txt"), "w")
            ctl = subprocess.Popen([sys.executable, "mock_controller.py", "--config", cfg_path],
                                   cwd=args.controller_dir, stdout=ctl_log,
                                   stderr=subprocess.STDOUT)
            procs.append(ctl)
            wait_port(API_PORT)
            base = "http://127.0.0.1:%d" % API_PORT

        srv_log = open(os.path.join(out_dir, "test_server_log.txt"), "w")
        srv = subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "server", "server.py"),
                                "--host", "127.0.0.1", "--port", str(SERVER_PORT),
                                "--capacity", "10", "--controller", base,
                                "--log-csv", os.path.join(tmp, "decisions.csv")],
                               stdout=srv_log, stderr=subprocess.STDOUT)
        procs.append(srv)
        wait_port(SERVER_PORT)

        r1 = reserve("C1", 4, 60)
        record("TC01", "Request 4 Mbps", "ACCEPTED, 6 left",
               "%s, %s left" % (r1["status"], r1.get("remaining_bandwidth")),
               r1["status"] == "ACCEPTED" and r1["remaining_bandwidth"] == 6)
        r2 = reserve("C2", 3, 60)
        record("TC02", "Request another 3 Mbps", "ACCEPTED, 3 left",
               "%s, %s left" % (r2["status"], r2.get("remaining_bandwidth")),
               r2["status"] == "ACCEPTED" and r2["remaining_bandwidth"] == 3)
        r3 = reserve("C3", 5, 60)
        record("TC03", "Request another 5 Mbps", "REJECTED",
               "%s (%s)" % (r3["status"], r3.get("reason")), r3["status"] == "REJECTED")

        r4 = reserve("C4", -2, 60)
        record("TC04", "Negative bandwidth", "REJECTED",
               "%s (%s)" % (r4["status"], r4.get("reason")), r4["status"] == "REJECTED")
        r4b = ask('{"type": "RESERVE", bad json')
        record("TC04b", "Malformed JSON request", "ERROR",
               "%s (%s)" % (r4b["status"], r4b.get("reason")), r4b["status"] == "ERROR")
        r4c = reserve("C4", "abc", 60)
        record("TC04c", "Non-numeric bandwidth", "REJECTED",
               r4c["status"], r4c["status"] == "REJECTED")
        r4d = reserve("C1", 1, 60)
        record("TC04d", "Same client asks twice", "REJECTED",
               "%s (%s)" % (r4d["status"], r4d.get("reason")), r4d["status"] == "REJECTED")

        ids = controller_ids(base)
        record("TC07", "Accepted -> SDN policy installed", "controller has R001,R002",
               "controller has %s" % ",".join(ids) or "-", ids == ["R001", "R002"])

        # TC05 / TC08: reservation expiry -> resources released + policy removed
        rel = ask({"type": "RELEASE", "client_id": "C2", "reservation_id": "R002"})
        r5 = reserve("C5", 3, 3)
        time.sleep(5)
        st = ask({"type": "STATUS"})["utilization"]
        lst = {r["reservation_id"]: r["status"] for r in ask({"type": "LIST"})["reservations"]}
        record("TC05", "Reservation expiry (3 s)", "R003 EXPIRED, 6 Mbps free",
               "%s %s, %s Mbps free" % (r5.get("reservation_id"),
                                       lst.get(r5.get("reservation_id")),
                                       st["available_bandwidth"]),
               r5["status"] == "ACCEPTED" and lst.get(r5["reservation_id"]) == "EXPIRED"
               and st["available_bandwidth"] == 6)
        ids = controller_ids(base)
        record("TC08", "Expired/released -> SDN policy removed", "controller has only R001",
               "controller has %s" % ",".join(ids), ids == ["R001"] and rel["status"] == "OK")

        # TC09: client disconnects abruptly, server keeps working
        s = socket.create_connection(("127.0.0.1", SERVER_PORT))
        s.sendall(b'{"type": "RESERVE", "client_id": "C9", "band')   # half a request
        s.close()
        s = socket.create_connection(("127.0.0.1", SERVER_PORT))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()                                                     # RST
        time.sleep(0.5)
        st = ask({"type": "STATUS"})
        record("TC09", "Client disconnects", "server continues running",
               "server alive=%s, answered %s" % (srv.poll() is None, st["status"]),
               srv.poll() is None and st["status"] == "OK")

        # TC06: simultaneous requests (6 Mbps free, 8 x 2 Mbps at once -> exactly 3)
        replies = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(i):
            c = Client("127.0.0.1", SERVER_PORT)
            barrier.wait()
            resp = c.request({"type": "RESERVE", "client_id": "C%d" % (10 + i),
                              "bandwidth": 2, "duration": 60})
            c.close()
            with lock:
                replies.append(resp["status"])
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        st = ask({"type": "STATUS"})["utilization"]
        acc = replies.count("ACCEPTED")
        ctl_used = api(base, "/api/capacity")
        record("TC06", "8 simultaneous 2 Mbps requests", "3 accepted, 10/10 Mbps, no over-allocation",
               "%d accepted, %s/%s Mbps (controller %s)" % (
                   acc, st["reserved_bandwidth"], st["total_bandwidth"],
                   ctl_used.get("used_mbps")),
               acc == 3 and st["reserved_bandwidth"] == 10 and ctl_used.get("used_mbps") == 10)

        # Challenging scenario: controller fails -> rollback, no false success
        if not args.use_running_controller:
            ask({"type": "RELEASE", "client_id": "C1", "reservation_id": "R001"})
            before = ask({"type": "STATUS"})["utilization"]["available_bandwidth"]
            procs[0].terminate()
            procs[0].wait(5)
            rr = reserve("C20", 2, 30)
            after = ask({"type": "STATUS"})["utilization"]
            record("TC10", "Controller down -> rollback", "REJECTED, capacity unchanged",
                   "%s (%s); free %s -> %s" % (rr["status"], rr.get("reason"), before,
                                              after["available_bandwidth"]),
                   rr["status"] == "REJECTED" and after["available_bandwidth"] == before
                   and srv.poll() is None)
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()

    path = os.path.join(out_dir, "person1_test_results.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test", "description", "expected", "actual", "result"])
        w.writerows(results)
    failed = [r for r in results if r[4] == "FAIL"]
    print("\n%d/%d passed. Results: %s" % (len(results) - len(failed), len(results), path))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
