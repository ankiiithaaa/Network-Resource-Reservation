#!/usr/bin/env python3
"""Three client hosts (h1-h3) + reservation-server host h4 on one OVS switch.

h4 is created in the VM's ROOT network namespace (inNamespace=False). That is
what lets the TCP reservation server on h4 reach both the clients (10.0.0.x via
s1) and the Ryu controller's HTTP API on 127.0.0.1:8081. A normal Mininet host
has its own isolated namespace and could not reach 127.0.0.1 of the VM.

Run in the Ubuntu VM using its system Python:  sudo python3 topology.py
"""
import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config/network.json"
HOSTS = ("h1", "h2", "h3")


def load_config(path=DEFAULT_CONFIG):
    cfg = json.loads(Path(path).read_text())
    if set(cfg["hosts"]) != set(HOSTS):
        raise ValueError("Expected exactly h1, h2, h3")
    hosts = [cfg["hosts"][name] for name in HOSTS]
    interfaces = [ipaddress.IPv4Interface(host["ip"]) for host in hosts]
    if (len({i.network for i in interfaces}) != 1 or
            len({i.ip for i in interfaces}) != 3 or
            any(i.ip in (i.network.network_address, i.network.broadcast_address)
                for i in interfaces)):
        raise ValueError("Hosts must have distinct usable IPv4 addresses on one subnet")
    macs = [h["mac"].lower() for h in hosts]
    if len(set(macs)) != 3 or any(
        not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", m)
        or int(m[:2], 16) & 1 or m == "00:00:00:00:00:00" for m in macs
    ):
        raise ValueError("Expected three distinct, nonzero unicast MACs")
    if [h["port"] for h in hosts] != [1, 2, 3]:
        raise ValueError("Expected h1, h2, h3 on switch ports 1, 2, 3")
    server = cfg.get("server_host")
    if server is not None:
        sip = ipaddress.IPv4Interface(server["ip"])
        if (sip.network != interfaces[0].network or sip.ip in {i.ip for i in interfaces}
                or sip.ip in (sip.network.network_address, sip.network.broadcast_address)):
            raise ValueError("server_host needs its own usable IPv4 address in the hosts' subnet")
        if server["mac"].lower() in macs or not re.fullmatch(
                r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", server["mac"].lower()):
            raise ValueError("server_host needs a distinct unicast MAC")
        bounded_int(server["port"], "server_host port", 4, 64)
        if server["name"] in HOSTS:
            raise ValueError("server_host name must differ from h1, h2, h3")
    sw = cfg["switch"]
    if (sw["name"] != "s1" or sw["protocol"] != "OpenFlow13" or
            not re.fullmatch(r"[0-9a-fA-F]{16}", sw["dpid"]) or int(sw["dpid"], 16) == 0):
        raise ValueError("Expected s1, OpenFlow13 and a nonzero 16-digit hex DPID")
    ipaddress.IPv4Address(cfg["controller"]["ip"])
    bounded_int(cfg["controller"]["port"], "controller port", 1, 65535)
    bounded_int(cfg["controller"]["wait_seconds"], "controller wait", 1, 120)
    bench = cfg["benchmark"]
    cap = bounded_int(bench["capacity_mbps"], "capacity", 2, 10000)
    bounded_int(bench["reserved_mbps"], "reservation", 1, cap - 1)
    bounded_int(bench["duration_seconds"], "duration", 5, 60)
    bounded_int(bench["repeats"], "repeats", 1, 20)
    bounded_int(bench["udp_offer_mbps_per_flow"], "UDP offered Mbps", cap + 1, 20000)
    ports = [bounded_int(bench[k], k, 1024, 65535) for k in
             ("reserved_port", "competing_port")]
    if ports[0] == ports[1]:
        raise ValueError("The two iPerf3 ports must differ")
    return cfg


def bounded_int(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")
    return value


def host_ip(cfg, name):
    return str(ipaddress.IPv4Interface(cfg["hosts"][name]["ip"]).ip)


def common_args(parser):
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=("controller", "standalone"), default="controller")
    parser.add_argument("--controller-ip", type=ipaddress.IPv4Address)
    parser.add_argument("--controller-port", type=int)


def config_from_args(args):
    cfg = load_config(args.config)
    if args.controller_ip is not None:
        cfg["controller"]["ip"] = str(args.controller_ip)
    if args.controller_port is not None:
        cfg["controller"]["port"] = bounded_int(args.controller_port, "controller port", 1, 65535)
    return cfg


def run_command(argv, timeout=10):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        message = (p.stderr or p.stdout).strip()
        raise RuntimeError(f"{' '.join(argv)} (exit {p.returncode}): {message}")
    return p.stdout.strip()


def controller_target(cfg):
    c = cfg["controller"]
    return f"tcp:{c['ip']}:{c['port']}"


def controller_connected(cfg):
    result = json.loads(run_command([
        "ovs-vsctl", "--timeout=5", "--format=json", "--columns=target,is_connected",
        "list", "Controller"
    ]))
    target_index = result["headings"].index("target")
    conn_index = result["headings"].index("is_connected")
    return any(row[target_index] == controller_target(cfg) and row[conn_index] is True
               for row in result["data"])


def verify_switch(cfg, mode):
    sw = cfg["switch"]["name"]
    if mode == "controller":
        attached = run_command(["ovs-vsctl", "--timeout=5", "get-controller", sw]).splitlines()
        if attached != [controller_target(cfg)]:
            raise RuntimeError(f"Unexpected controller target on {sw}: {attached}")
        deadline = time.monotonic() + cfg["controller"]["wait_seconds"]
        while not controller_connected(cfg):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"No OVS/OpenFlow connection to {controller_target(cfg)}")
            time.sleep(0.25)
        print(f"{sw} is_connected=true to {controller_target(cfg)}", flush=True)
    for name in HOSTS:
        port = cfg["hosts"][name]["port"]
        seen = run_command(["ovs-vsctl", "--timeout=5", "get", "Interface",
                            f"{sw}-eth{port}", "ofport"])
        if seen != str(port):
            raise RuntimeError(f"Expected {sw}-eth{port} on OpenFlow port {port}, got {seen}")


def flow_dump(cfg):
    return run_command(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", cfg["switch"]["name"]])


def preflight(cfg, benchmark=False):
    problems = []
    if os.geteuid() != 0:
        problems.append("requires sudo/root inside an Ubuntu VM")
    caps = re.search(r"^CapEff:\s+([0-9a-f]+)", Path("/proc/self/status").read_text(), re.M)
    for bit, label in ((12, "CAP_NET_ADMIN"), (21, "CAP_SYS_ADMIN")):
        if caps and not int(caps[1], 16) & (1 << bit):
            problems.append(f"missing {label}")
    required = ["mnexec", "ip", "ping", "ovs-vsctl", "ovs-ofctl"]
    if benchmark:
        required += ["mn", "tc", "iperf3", "ss", "ethtool"]
    missing = [exe for exe in required if not shutil.which(exe)]
    if missing:
        problems.append("missing commands: " + ", ".join(missing))
    if problems:
        raise RuntimeError("; ".join(problems))
    try:
        from mininet.net import Mininet  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Mininet's Python package is not on this interpreter; use /usr/bin/python3") from exc
    run_command(["ovs-vsctl", "--timeout=5", "show"])
    sw = cfg["switch"]["name"]
    bridge = subprocess.run(["ovs-vsctl", "--timeout=5", "br-exists", sw], timeout=8)
    if bridge.returncode == 0:
        raise RuntimeError(f"Bridge {sw} already exists; stop the other lab before running")
    if bridge.returncode != 2:
        raise RuntimeError(f"OVS bridge precheck failed (exit {bridge.returncode})")


@contextmanager
def start_network(cfg, mode, with_server=False):
    from mininet.link import Link
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    net = Mininet(controller=None, switch=OVSSwitch, link=Link, build=False)
    try:
        if mode == "controller":
            c = cfg["controller"]
            net.addController("c0", controller=RemoteController, ip=c["ip"], port=c["port"])
        sw = cfg["switch"]
        switch = net.addSwitch(sw["name"], dpid=sw["dpid"], protocols=sw["protocol"],
                               failMode="secure" if mode == "controller" else "standalone",
                               inband=False)
        for name in HOSTS:
            info = cfg["hosts"][name]
            node = net.addHost(name, ip=info["ip"], mac=info["mac"])
            net.addLink(node, switch, port1=0, port2=info["port"])
        server = cfg.get("server_host")
        if with_server and server:
            node = net.addHost(server["name"], ip=server["ip"], mac=server["mac"],
                               inNamespace=not server.get("root_namespace", True))
            net.addLink(node, switch, port1=0, port2=server["port"])
        net.build()
        net.start()
        yield net
    finally:
        net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    common_args(parser)
    parser.add_argument("--no-server-host", action="store_true",
                        help="build only h1-h3 (no reservation-server host h4)")
    args = parser.parse_args()
    try:
        cfg = config_from_args(args)
        preflight(cfg)
        from mininet.cli import CLI
        from mininet.log import setLogLevel
        setLogLevel("info")
        with start_network(cfg, args.mode, with_server=not args.no_server_host) as net:
            verify_switch(cfg, args.mode)
            server = cfg.get("server_host")
            if server and not args.no_server_host:
                sip = str(ipaddress.IPv4Interface(server["ip"]).ip)
                print(f"Reservation server host {server['name']} = {sip} "
                      f"(root namespace, s1 port {server['port']}). Start the server in "
                      f"another VM terminal:  python3 server/server.py --host {sip}",
                      flush=True)
            CLI(net)
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Topology stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
