#!/usr/bin/env python3
"""TCP client for the Network Resource Reservation System (Person 1).

Interactive (asks for Client ID, bandwidth and duration):
    python3 client.py --server 10.0.0.4

One-shot commands (handy in Mininet: h1 python3 client.py ... reserve C1 4 60):
    python3 client.py --server 10.0.0.4 reserve C1 4 60
    python3 client.py --server 10.0.0.4 release C1 R001
    python3 client.py --server 10.0.0.4 status
    python3 client.py --server 10.0.0.4 list
    python3 client.py --server 10.0.0.4 raw '{"type": "RESERVE", "bandwidth": -3}'
"""

import argparse
import json
import socket
import sys


def fmt(value):
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return str(value)


class Client:
    def __init__(self, host, port, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")

    def request(self, obj):
        line = obj if isinstance(obj, str) else json.dumps(obj)
        self.sock.sendall((line + "\n").encode("utf-8"))
        reply = self.reader.readline()
        if not reply:
            raise ConnectionError("server closed the connection")
        return json.loads(reply)

    def close(self):
        try:
            self.request({"type": "QUIT"})
        except (OSError, ValueError, ConnectionError):
            pass
        self.reader.close()
        self.sock.close()


def show(resp):
    status = resp.get("status")
    if status == "ACCEPTED":
        print("Reservation ID: %s" % resp["reservation_id"])
        print("Status: ACCEPTED")
        print("Allocated bandwidth: %s Mbps" % fmt(resp["allocated_bandwidth"]))
        print("Duration: %s seconds (expires at %s)" % (fmt(resp["duration"]), resp["expiry_time"]))
        print("Remaining bandwidth: %s Mbps" % fmt(resp["remaining_bandwidth"]))
    elif status == "REJECTED":
        print("Status: REJECTED")
        print("Reason: %s" % resp.get("reason"))
        if "remaining_bandwidth" in resp:
            print("Remaining bandwidth: %s Mbps" % fmt(resp["remaining_bandwidth"]))
    elif "reservations" in resp:
        rows = resp["reservations"]
        print("%-6s %-8s %-10s %-9s %-10s %-10s %-9s %s" % (
            "ID", "Client", "Client IP", "Mbps", "Start", "Expiry", "Left(s)", "Status"))
        for r in rows:
            print("%-6s %-8s %-10s %-9s %-10s %-10s %-9s %s" % (
                r["reservation_id"], r["client_id"], r["client_ip"] or "-",
                fmt(r["bandwidth"]), r["start_time"] or "-", r["expiry_time"] or "-",
                "-" if r["remaining_seconds"] is None else r["remaining_seconds"],
                r["status"] + (" (%s)" % r["note"] if r["note"] else "")))
        if not rows:
            print("(no reservations yet)")
        for rej in resp.get("rejections", []):
            print("REJECTED %s %s %s Mbps: %s" % (rej["time"], rej["client_id"],
                                                 fmt(rej["bandwidth"]), rej["reason"]))
        show_util(resp["utilization"])
    elif "utilization" in resp:
        show_util(resp["utilization"])
    elif status == "OK" and "released_bandwidth" in resp:
        print("Reservation %s released (%s Mbps). Remaining bandwidth: %s Mbps" % (
            resp["reservation_id"], fmt(resp["released_bandwidth"]),
            fmt(resp["remaining_bandwidth"])))
    else:
        print(json.dumps(resp, indent=2))


def show_util(u):
    print("Capacity: %s Mbps | reserved: %s Mbps | available: %s Mbps | utilisation: %s%%" % (
        fmt(u["total_bandwidth"]), fmt(u["reserved_bandwidth"]),
        fmt(u["available_bandwidth"]), u["utilization_percent"]))
    print("Requests: %s | accepted: %s | rejected: %s | expired: %s | released: %s | "
          "SDN failures: %s" % (u["requests"], u["accepted"], u["rejected"],
                                u["expired"], u["released"], u["policy_failures"]))


def interactive(client):
    print("Commands: reserve, release, status, list, quit")
    while True:
        try:
            cmd = input("\n> ").strip().lower()
        except EOFError:
            return
        if cmd in ("quit", "exit", "q"):
            return
        if cmd in ("reserve", "r", ""):
            cid = input("Client ID: ").strip()
            bw = input("Bandwidth (Mbps): ").strip()
            dur = input("Duration (seconds): ").strip()
            req = {"type": "RESERVE", "client_id": cid, "bandwidth": number(bw),
                   "duration": number(dur)}
        elif cmd == "release":
            req = {"type": "RELEASE", "client_id": input("Client ID: ").strip(),
                   "reservation_id": input("Reservation ID: ").strip().upper()}
        elif cmd in ("status", "list"):
            req = {"type": cmd.upper()}
        else:
            print("Unknown command")
            continue
        show(client.request(req))


def number(text):
    """Send numbers as numbers, anything else unchanged (server validates)."""
    try:
        value = float(text)
        return int(value) if value.is_integer() else value
    except ValueError:
        return text


def main():
    ap = argparse.ArgumentParser(description="Bandwidth reservation client")
    ap.add_argument("--server", default="127.0.0.1", help="server IP (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--json", action="store_true", help="print raw JSON replies")
    ap.add_argument("command", nargs="*",
                    help="reserve <client> <mbps> <seconds> | release <client> <rid> | "
                         "status | list | raw <line>")
    args = ap.parse_args()

    try:
        client = Client(args.server, args.port)
    except OSError as exc:
        print("Cannot connect to server %s:%s: %s" % (args.server, args.port, exc))
        return 1

    try:
        if not args.command:
            interactive(client)
            return 0
        cmd, rest = args.command[0].lower(), args.command[1:]
        if cmd == "reserve" and len(rest) == 3:
            req = {"type": "RESERVE", "client_id": rest[0], "bandwidth": number(rest[1]),
                   "duration": number(rest[2])}
        elif cmd == "release" and len(rest) == 2:
            req = {"type": "RELEASE", "client_id": rest[0], "reservation_id": rest[1].upper()}
        elif cmd in ("status", "list") and not rest:
            req = {"type": cmd.upper()}
        elif cmd == "raw" and rest:
            req = " ".join(rest)
        else:
            ap.error("bad command")
        resp = client.request(req)
        print(json.dumps(resp, indent=2)) if args.json else show(resp)
        return 0 if resp.get("status") in ("ACCEPTED", "OK") else 2
    except (OSError, ConnectionError, ValueError) as exc:
        print("Error talking to server: %s" % exc)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
