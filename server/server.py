#!/usr/bin/env python3
"""TCP reservation server (Person 1).

Clients connect over TCP and send one JSON object per line. The server admits
or rejects bandwidth requests (reservation_manager.py) and tells the SDN
controller (Person 2, HTTP API on port 8081) to install / remove the network
policy for every accepted / released / expired reservation.

Run:
    python3 server.py                                   # with controller at 127.0.0.1:8081
    python3 server.py --no-controller                   # stand-alone (no SDN)
    python3 server.py --host 10.0.0.4 --port 5000 --capacity 10

Protocol (newline-delimited JSON, UTF-8)
    -> {"type": "RESERVE", "client_id": "C1", "bandwidth": 4, "duration": 60}
    <- {"status": "ACCEPTED", "reservation_id": "R001", "allocated_bandwidth": 4,
        "remaining_bandwidth": 6, ...}
    -> {"type": "RELEASE", "client_id": "C1", "reservation_id": "R001"}
    -> {"type": "STATUS"}       utilisation + counters
    -> {"type": "LIST"}         every reservation and the rejection log
    -> {"type": "QUIT"}
A plain-text form also works, e.g. "RESERVE C1 4 60", for testing with nc/telnet.
"""

import argparse
import csv
import ipaddress
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from reservation_manager import PolicyError, ReservationManager, _fmt_bw

MAX_LINE = 4096            # bytes per request line
IDLE_TIMEOUT = 300         # seconds a client may stay silent before we close it
PRINT_LOCK = threading.Lock()


def log(message):
    with PRINT_LOCK:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


# ----------------------------------------------------------------------
# SDN controller interface (Person 2's HTTP API)
# ----------------------------------------------------------------------
class ControllerPolicy:
    """Installs/removes reservation policies through the controller HTTP API.

    POST {url}/api/reservation  {"action": "reserve", "reservation_id", "client_id",
                                 "bandwidth", ["client_ip"]}
    POST {url}/api/reservation  {"action": "release", "reservation_id"}
    The server owns expiry, so no "duration" is sent: the controller keeps the
    policy until we explicitly release it.
    """

    def __init__(self, base_url, timeout=3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, body):
        req = urllib.request.Request(
            self.base_url + "/api/reservation",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read().decode() or "{}")
            except ValueError:
                payload = {}
            return err.code, payload
        except (urllib.error.URLError, OSError, socket.timeout) as err:
            reason = getattr(err, "reason", err)
            raise PolicyError("controller unreachable at %s (%s)" % (self.base_url, reason))

    def _get(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def install(self, res):
        body = {"action": "reserve", "reservation_id": res.reservation_id,
                "client_id": res.client_id, "bandwidth": res.bandwidth}
        if res.client_ip:
            body["client_ip"] = res.client_ip
        status, payload = self._post(body)
        if status != 200:
            raise PolicyError("controller returned HTTP %s %s: %s" % (
                status, payload.get("error", ""), payload.get("message", "")))
        info = payload.get("reservation", {})
        log("SDN policy INSTALLED for %s (client %s, ip %s, meter %s, switches %s)" % (
            res.reservation_id, res.client_id, info.get("client_ip"),
            info.get("meter_id"), info.get("installed_on_switches")))

    def remove(self, res):
        status, payload = self._post({"action": "release",
                                      "reservation_id": res.reservation_id})
        if status == 404:      # controller already forgot it -> nothing to remove
            log("SDN policy for %s was already gone on the controller" % res.reservation_id)
            return
        if status != 200:
            raise PolicyError("controller returned HTTP %s %s: %s" % (
                status, payload.get("error", ""), payload.get("message", "")))
        log("SDN policy REMOVED for %s" % res.reservation_id)

    def check(self):
        return self._get("/api/health")

    def clear_stale(self):
        """Release reservations left on the controller by a previous server run."""
        data = self._get("/api/reservations")
        stale = [r["reservation_id"] for r in data.get("reservations", [])
                 if r.get("reservation_id")]
        for rid in stale:
            self._post({"action": "release", "reservation_id": rid})
        return stale


# ----------------------------------------------------------------------
# Decision log (CSV) used for evaluating accepted / rejected requests
# ----------------------------------------------------------------------
class DecisionLog:
    FIELDS = ["time", "client_addr", "client_id", "bandwidth", "duration",
              "decision", "reservation_id", "remaining_bandwidth", "reason"]

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(self.FIELDS)

    def write(self, addr, request, result):
        if not self.path:
            return
        row = [time.strftime("%Y-%m-%d %H:%M:%S"), addr, request.get("client_id"),
               request.get("bandwidth"), request.get("duration"), result.get("status"),
               result.get("reservation_id", ""), result.get("remaining_bandwidth"),
               result.get("reason", "")]
        with self.lock, open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)


# ----------------------------------------------------------------------
# Request handling
# ----------------------------------------------------------------------
def parse_request(line):
    """Turn one request line into a dict. Raises ValueError if malformed."""
    line = line.strip()
    if not line:
        raise ValueError("empty request")
    if line.startswith("{"):
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed JSON (%s)" % exc.msg)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
    else:                                     # plain text: RESERVE C1 4 60
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "RESERVE" and len(parts) == 4:
            req = {"type": cmd, "client_id": parts[1],
                   "bandwidth": parts[2], "duration": parts[3]}
        elif cmd == "RELEASE" and len(parts) == 3:
            req = {"type": cmd, "client_id": parts[1], "reservation_id": parts[2]}
        elif cmd in ("STATUS", "LIST", "QUIT") and len(parts) == 1:
            req = {"type": cmd}
        else:
            raise ValueError("unknown command; use RESERVE <id> <mbps> <seconds>, "
                             "RELEASE <id> <reservation>, STATUS, LIST or QUIT")
    req["type"] = str(req.get("type", "")).upper()
    return req


class ReservationServer:
    def __init__(self, host, port, manager, decision_log):
        self.host, self.port = host, port
        self.manager = manager
        self.decisions = decision_log
        self.sock = None
        self.running = threading.Event()
        self.clients = 0
        self.clients_lock = threading.Lock()

    @staticmethod
    def policy_ip(addr_ip):
        """IP to send to the controller: the client's real IP, unless it is
        loopback (local testing) - then the controller's client_map is used."""
        try:
            return None if ipaddress.ip_address(addr_ip).is_loopback else addr_ip
        except ValueError:
            return None

    def handle(self, req, addr):
        kind = req["type"]
        if kind == "RESERVE":
            # Missing/invalid fields are rejected (and counted) by the manager.
            result = self.manager.request_reservation(
                req.get("client_id"), req.get("bandwidth"), req.get("duration"),
                client_ip=self.policy_ip(addr[0]))
            self.decisions.write("%s:%s" % addr, req, result)
            if result["status"] == "ACCEPTED":
                log("ACCEPTED %s: %s reserved %s Mbps for %ss -> remaining %s Mbps" % (
                    result["reservation_id"], result["client_id"],
                    _fmt_bw(result["allocated_bandwidth"]), _fmt_bw(result["duration"]),
                    _fmt_bw(result["remaining_bandwidth"])))
            else:
                log("REJECTED %s (%s Mbps): %s" % (
                    req.get("client_id"), req.get("bandwidth"), result["reason"]))
            return result
        if kind == "RELEASE":
            if "reservation_id" not in req or "client_id" not in req:
                return {"status": "ERROR",
                        "reason": "RELEASE needs client_id and reservation_id"}
            return self.manager.release_reservation(
                str(req["reservation_id"]), client_id=str(req["client_id"]))
        if kind == "STATUS":
            return {"status": "OK", "utilization": self.manager.get_utilization()}
        if kind == "LIST":
            return {"status": "OK", "reservations": self.manager.get_reservations(),
                    "rejections": self.manager.get_rejections(),
                    "utilization": self.manager.get_utilization()}
        if kind == "QUIT":
            return {"status": "OK", "message": "bye"}
        return {"status": "ERROR",
                "reason": "unknown request type '%s' (use RESERVE, RELEASE, STATUS, "
                          "LIST or QUIT)" % req["type"]}

    def serve_client(self, conn, addr):
        with self.clients_lock:
            self.clients += 1
            n = self.clients
        log("Client connected from %s:%s (%d connection(s) so far)" % (addr[0], addr[1], n))
        conn.settimeout(IDLE_TIMEOUT)
        buf = b""
        try:
            while self.running.is_set():
                try:
                    chunk = conn.recv(1024)
                except socket.timeout:
                    log("Client %s:%s idle for %ss, closing" % (addr[0], addr[1], IDLE_TIMEOUT))
                    break
                if not chunk:                          # client closed the connection
                    break
                buf += chunk
                if len(buf) > MAX_LINE and b"\n" not in buf:
                    self.send(conn, {"status": "ERROR", "reason": "request too long"})
                    break
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        req = parse_request(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        log("Invalid request from %s:%s: %s" % (addr[0], addr[1], exc))
                        self.send(conn, {"status": "ERROR", "reason": "Invalid request: %s" % exc})
                        continue
                    self.send(conn, self.handle(req, addr))
                    if req["type"] == "QUIT":
                        return
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError) as exc:
            log("Client %s:%s connection error: %s" % (addr[0], addr[1], exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            log("Client %s:%s disconnected (server keeps running; its reservations "
                "stay until they expire or are released)" % addr)

    @staticmethod
    def send(conn, obj):
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def serve_forever(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(32)
        self.sock.settimeout(1.0)          # wake up regularly so Ctrl+C works
        self.running.set()
        log("Reservation server listening on %s:%s (TCP)" % (self.host, self.port))
        while self.running.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve_client, args=(conn, addr),
                             name="client-%s:%s" % addr, daemon=True).start()

    def shutdown(self):
        self.running.clear()
        if self.sock:
            self.sock.close()


def main():
    ap = argparse.ArgumentParser(description="TCP network resource reservation server")
    ap.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=5000, help="TCP port (default 5000)")
    ap.add_argument("--capacity", type=float, default=10.0,
                    help="total bandwidth in Mbps (default 10, must match the controller)")
    ap.add_argument("--max-duration", type=float, default=3600,
                    help="longest reservation in seconds (default 3600)")
    ap.add_argument("--controller", default="http://127.0.0.1:8081",
                    help="SDN controller API base URL (default http://127.0.0.1:8081)")
    ap.add_argument("--no-controller", action="store_true",
                    help="run without an SDN controller (testing only)")
    ap.add_argument("--controller-timeout", type=float, default=3.0)
    ap.add_argument("--log-csv", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results", "reservation_log.csv"),
        help="CSV file for every accept/reject decision ('' to disable)")
    args = ap.parse_args()

    if args.no_controller:
        policy = None
        log("Running WITHOUT an SDN controller (no network policies will be installed)")
    else:
        policy = ControllerPolicy(args.controller, args.controller_timeout)
        try:
            health = policy.check()
            log("SDN controller reachable at %s, switches: %s"
                % (args.controller, health.get("switches")))
            stale = policy.clear_stale()
            if stale:
                log("Released stale reservations left on the controller: %s" % ", ".join(stale))
        except Exception as exc:
            log("WARNING: SDN controller not reachable at %s (%s). Requests will be "
                "REJECTED until it is up." % (args.controller, exc))

    manager = ReservationManager(args.capacity, policy, args.max_duration, on_event=log)
    manager.start_expiry_thread(1.0)
    server = ReservationServer(args.host, args.port, manager, DecisionLog(args.log_csv))

    def stop(*_):
        server.shutdown()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    log("Capacity: %s Mbps" % _fmt_bw(args.capacity))
    try:
        server.serve_forever()
    finally:
        log("Shutting down: releasing active reservations ...")
        manager.stop()
        manager.release_all()
        util = manager.get_utilization()
        log("Final stats: %s" % json.dumps(util))


if __name__ == "__main__":
    sys.exit(main())
