#!/usr/bin/env python3
"""End-to-end test: real Ryu controller + Open vSwitch + Mininet.

Prerequisite: the controller is already running in another terminal
    ryu-manager controller.py
Then run (needs root for Mininet):
    sudo python3 mininet_tests/e2e_test.py

It builds h1,h2,h3 (clients) + h4 (server) behind switch s1, drives the HTTP API
and checks with ovs-ofctl that flows/meters appear and disappear, and (with
iperf) that the reserved rate is actually enforced. Output is also written to
test_results/e2e_results.txt.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController

API = os.environ.get("SDN_API", "http://127.0.0.1:8081")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "test_results", "e2e_results.txt")
LINES, FAILS = [], []


def say(msg=""):
    print(msg)
    LINES.append(msg)


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    say("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def dump(s1, what):
    cmd = "ovs-ofctl -O OpenFlow13 %s s1" % what
    out = s1.cmd(cmd)
    say("$ " + cmd)
    say(out.strip())
    return out


def mbps(text):
    m = re.search(r"([\d.]+)\s*([KMG])bits/sec", text)
    if not m:
        return None
    return float(m.group(1)) * {"K": 0.001, "M": 1, "G": 1000}[m.group(2)]


def iperf(net, client, server, secs=5):
    server.cmd("pkill iperf; iperf -s -f m > /dev/null 2>&1 &")
    time.sleep(1)
    out = client.cmd("iperf -c %s -t %d -f m" % (server.IP(), secs))
    server.cmd("pkill iperf")
    # Use the last line that carries a rate: newer iperf versions can print
    # further lines after the summary, so the very last line is not reliable.
    rates = [r for r in (mbps(line) for line in out.splitlines()) if r is not None]
    if not rates:
        say("iperf output (no rate found):")
        say(out.strip() or "(empty)")
        return None
    return rates[-1]


def main():
    setLogLevel("warning")
    net = Mininet(controller=None, switch=OVSKernelSwitch, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    h1, h2, h3, h4 = [net.addHost("h%d" % i, ip="10.0.0.%d/24" % i)
                      for i in range(1, 5)]
    for h in (h1, h2, h3, h4):
        net.addLink(h, s1)
    net.start()
    try:
        time.sleep(3)  # let the switch connect
        say("=== 1. Controller + switch connection ===")
        code, body = api("GET", "/api/health")
        check("API reachable", code == 200, str(body))
        check("switch connected to controller", len(body.get("switches", [])) >= 1)
        check("base flow installed", "actions=NORMAL" in dump(s1, "dump-flows"))
        check("connectivity (pingAll)", net.pingAll() == 0)

        say("\n=== 2. Reserve 4 Mbps for C1 (h1) ===")
        base = iperf(net, h1, h4)
        say("baseline h1->h4 throughput without reservation: %s Mbps" % base)
        code, body = api("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C1",
            "reservation_id": "R001", "bandwidth": 4})
        check("reserve accepted", code == 200, json.dumps(body.get("reservation", {})))
        flows = dump(s1, "dump-flows")
        meters = dump(s1, "dump-meters")
        check("reserved flow present (ipv4_src=10.0.0.1 + meter)",
              "nw_src=10.0.0.1" in flows and "meter:" in flows)
        check("meter present at 4000 kbps", "rate=4000" in meters)
        rate = iperf(net, h1, h4)
        say("measured h1->h4 with reservation: %s Mbps" % rate)
        check("traffic limited to ~4 Mbps", rate is not None and 2.5 <= rate <= 5.5,
              "(%s Mbps)" % rate)

        say("\n=== 3. Over-capacity request is rejected, nothing installed ===")
        before = dump(s1, "dump-flows")
        code, body = api("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C2",
            "reservation_id": "R002", "bandwidth": 8})
        check("rejected with 409", code == 409, body.get("error", ""))
        check("no new flow for rejected request",
              "nw_src=10.0.0.2" not in dump(s1, "dump-flows")
              and before.count("nw_src") == dump(s1, "dump-flows").count("nw_src"))

        say("\n=== 4. Update R001 to 2 Mbps ===")
        code, _ = api("POST", "/api/reservation", {
            "action": "update", "reservation_id": "R001", "bandwidth": 2})
        check("update accepted", code == 200)
        check("meter now 2000 kbps", "rate=2000" in dump(s1, "dump-meters"))

        say("\n=== 5. Release R001 ===")
        code, _ = api("POST", "/api/reservation", {
            "action": "release", "reservation_id": "R001"})
        check("release accepted", code == 200)
        check("reserved flow removed", "nw_src=10.0.0.1" not in dump(s1, "dump-flows"))
        check("meter removed", "meter=" not in dump(s1, "dump-meters").replace(
            "OFPST_METER_CONFIG", ""))
        check("base flow still there", "actions=NORMAL" in dump(s1, "dump-flows"))

        say("\n=== 6. Expiry: reserve C3 for 5 s ===")
        code, _ = api("POST", "/api/reservation", {
            "action": "reserve", "client_id": "C3", "reservation_id": "R003",
            "bandwidth": 3, "duration": 5})
        check("timed reserve accepted", code == 200)
        check("flow present", "nw_src=10.0.0.3" in dump(s1, "dump-flows"))
        time.sleep(8)
        check("flow auto-removed after expiry",
              "nw_src=10.0.0.3" not in dump(s1, "dump-flows"))
        check("reservation gone from controller",
              api("GET", "/api/reservations/R003")[0] == 404)
    finally:
        net.stop()
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        say("\nRESULT: %s" % ("ALL PASSED" if not FAILS else "FAILED: %s" % FAILS))
        with open(OUT, "w") as fh:
            fh.write("\n".join(LINES) + "\n")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
