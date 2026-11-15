#!/usr/bin/env python3
"""Run real Mininet, OpenFlow and iPerf3 checks, preserving logs and CSV evidence."""
import argparse
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from measurements import parse_iperf, parse_ping
import qos
from topology import (HOSTS, common_args, config_from_args, flow_dump, host_ip, preflight,
                      run_command, start_network, verify_switch)

METRICS = ("utc", "switch_mode", "policy", "scenario", "repeat", "protocol",
           "source", "destination", "port", "duration_s", "offered_mbps",
           "throughput_mbps", "rtt_avg_ms", "icmp_loss_pct", "ping_sent",
           "ping_received", "udp_loss_pct", "udp_jitter_ms", "evidence")
CHECKS = ("check", "status", "detail")
EXPECTED_LIVE = ("topology_ports", "controller_openflow", "all_pairs_reachable",
                 "openflow_rules", "static_matrix", "reservation_matrix",
                 "reservation_classification", "compare_policies")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Records:
    def __init__(self, out, checks):
        self.out, self.checks = out, checks

    def check(self, name, status, detail):
        with self.checks.open("a", newline="") as file:
            csv.writer(file).writerow((name, status, detail))
        print(f"{status:7} {name} | {detail}", flush=True)

    def metric(self, **row):
        with (self.out / "performance_results.csv").open("a", newline="") as file:
            csv.DictWriter(file, METRICS).writerow({"utc": now(), **row})

    def raw(self, label, content):
        path = self.out / "raw" / label
        path.write_text(content)
        return str(path.relative_to(self.out))


def create_records(out, checks):
    csv_paths = ((out / "performance_results.csv", METRICS), (checks, CHECKS))
    if any(p.exists() for p, _ in csv_paths) or (out / "raw").exists():
        raise ValueError("Output already contains evidence. Choose a new --out directory.")
    (out / "raw").mkdir(parents=True)
    for path, fields in csv_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", newline="") as file:
            csv.writer(file).writerow(fields)
    return Records(out, checks)


def stop_process(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)


def host_popen(stack, node, args, output):
    file = stack.enter_context(output.open("w"))
    proc = node.popen(args, stdout=file, stderr=subprocess.STDOUT,
                      env={**os.environ, "LC_ALL": "C"})
    stack.callback(stop_process, proc)
    return proc


def port_ready(node, port, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"iPerf3 listener on port {port} exited early")
        with ExitStack() as stack:
            p = node.popen(["ss", "-Hltn", "sport", "=", f":{port}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stack.callback(stop_process, p)
            stdout, stderr = p.communicate(timeout=2)
        if p.returncode:
            raise RuntimeError(f"Listener probe on {port} failed: {stderr.strip()}")
        if stdout.strip():
            return
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for iPerf3 listener on port {port}")


def ping_args(dst, count, interval):
    return ["ping", "-n", "-i", str(interval), "-W", "1", "-c", str(count), dst]


def ping_once(net, cfg, rec, mode, policy, scenario, repeat, source, dest, count=5):
    label = f"{policy}_{scenario}_{repeat}_{source}_{dest}.ping.txt"
    path = rec.out / "raw" / label
    with ExitStack() as stack:
        proc = host_popen(stack, net[source], ping_args(host_ip(cfg, dest), count, 0.2), path)
        if proc.wait(timeout=count + 5) not in (0, 1):
            raise RuntimeError(f"Ping failed to run, see {path}")
    result = parse_ping(path.read_text())
    rec.metric(switch_mode=mode, policy=policy, scenario=scenario, repeat=repeat,
               protocol="ICMP", source=source, destination=dest, evidence=f"raw/{label}", **result)
    return result


def run_load(net, cfg, rec, mode, policy, protocol, repeat, concurrent):
    bench = cfg["benchmark"]
    duration = bench["duration_seconds"]
    target = host_ip(cfg, "h3")
    scenario = f"{'dual' if concurrent else 'single'}_{protocol.lower()}"
    label = f"{policy}_{scenario}_{repeat}"
    flows = [("h1", bench["reserved_port"])]
    if concurrent:
        flows += [("h2", bench["competing_port"])]
    udp_offer = bench["udp_offer_mbps_per_flow"]
    with ExitStack() as stack:
        work = []
        for source, port in flows:
            srv = rec.out / "raw" / f"{label}_{source}.server.json"
            cli = rec.out / "raw" / f"{label}_{source}.client.json"
            args = ["iperf3", "-s", "-1", "-J", "-B", target, "-p", str(port)]
            server = host_popen(stack, net["h3"], args, srv)
            work.append((source, port, srv, cli, server))
        for _, port, _, _, server in work:
            port_ready(net["h3"], port, server)

        clients = []
        for source, port, _, cli, _ in work:
            args = ["iperf3", "-c", target, "-B", host_ip(cfg, source),
                    "-p", str(port), "-t", str(duration), "-J"]
            if protocol == "UDP":
                args += ["-u", "-b", f"{udp_offer}M", "-l", "1200"]
            clients.append(host_popen(stack, net[source], args, cli))
        probe = None
        if concurrent:
            probe_path = rec.out / "raw" / f"{label}_h2_h3.ping.txt"
            time.sleep(0.75)
            if any(p.poll() is not None for p in clients):
                raise RuntimeError(f"iPerf3 client exited before the under-load probe in {label}")
            probe = host_popen(stack, net["h2"], ping_args(target, duration - 1, 1), probe_path)

        deadline = time.monotonic() + duration + 12
        for process in clients + [item[4] for item in work]:
            if process.wait(timeout=max(0.2, deadline - time.monotonic())):
                raise RuntimeError(f"iPerf3 process failed in {label}, see raw client/server logs")
        for source, port, srv, _, _ in work:
            result = parse_iperf(json.loads(srv.read_text()), protocol)
            rec.metric(switch_mode=mode, policy=policy, scenario=scenario, repeat=repeat,
                       protocol=protocol, source=source, destination="h3", port=port,
                       duration_s=duration, offered_mbps=udp_offer if protocol == "UDP" else None,
                       evidence=f"raw/{srv.name}", **result)
        if probe is not None:
            if probe.wait(timeout=4) not in (0, 1):
                raise RuntimeError(f"Under-load ping command failed, see {probe_path}")
            rec.metric(switch_mode=mode, policy=policy, scenario=f"{scenario}_ping",
                       repeat=repeat, protocol="ICMP", source="h2", destination="h3",
                       evidence=f"raw/{probe_path.name}", **parse_ping(probe_path.read_text()))


def run_policy(net, cfg, rec, mode, policy):
    with qos.applied(cfg, policy):
        initial = qos.snapshot(cfg)
        rec.raw(f"{policy}_tc_before.json", json.dumps(initial, indent=2))
        for repeat in range(1, cfg["benchmark"]["repeats"] + 1):
            ping_once(net, cfg, rec, mode, policy, "idle_ping", repeat, "h1", "h3")
            run_load(net, cfg, rec, mode, policy, "TCP", repeat, False)
            run_load(net, cfg, rec, mode, policy, "TCP", repeat, True)
            run_load(net, cfg, rec, mode, policy, "UDP", repeat, True)
        final = qos.snapshot(cfg)
        rec.raw(f"{policy}_tc_after.json", json.dumps(final, indent=2))
    rows = cfg["benchmark"]["repeats"] * 8
    rec.check(f"{policy}_matrix", "PASS", f"{rows} measured rows from idle/single/dual scenarios")
    if policy == "reservation":
        deltas = {c: qos.class_bytes(final["class"], c) - qos.class_bytes(initial["class"], c)
                  for c in ("1:10", "1:20")}
        if min(deltas.values()) <= 0:
            raise RuntimeError(f"Both reserved and default HTB classes must carry traffic: {deltas}")
        rec.check("reservation_classification", "PASS",
                  f"Observed byte increases on both classes: {deltas}; see raw counters")


def inspect_network(net, cfg, rec, mode):
    sw = cfg["switch"]["name"]
    verify_switch(cfg, mode)
    show = run_command(["ovs-vsctl", "--timeout=5", "show"])
    ports = run_command(["ovs-ofctl", "-O", "OpenFlow13", "show", sw])
    target = run_command(["ovs-vsctl", "--timeout=5", "list", "Controller"])
    rec.raw("switch_and_controller.txt", f"{show}\n\n{target}\n\n{ports}\n")
    rec.check("topology_ports", "PASS", "s1 ports 1–3 match h1–h3; see raw/switch_and_controller.txt")
    rec.check("controller_openflow", "PASS" if mode == "controller" else "SKIPPED",
              "OVS reports connected to requested controller" if mode == "controller"
              else "Standalone OVS, no Ryu connection tested")
    pre = flow_dump(cfg)
    rec.raw("flows_before_ping.txt", pre + "\n")
    for source in HOSTS:
        for destination in HOSTS:
            if source != destination:
                ping_once(net, cfg, rec, mode, "unshaped", "connectivity_warmup",
                          1, source, destination, count=3)
    loss = net.pingAll(timeout=1)
    rec.check("all_pairs_reachable", "PASS" if loss == 0 else "FAIL",
              f"Mininet pingall after warm-up: {loss:g}% packet loss")
    post = flow_dump(cfg)
    rec.raw("flows_after_ping.txt", post + "\n")
    if mode == "controller":
        if "actions=" not in post:
            rec.check("openflow_rules", "FAIL", "No OpenFlow entries found after traffic")
            raise RuntimeError("No OpenFlow entries found after traffic")
        rec.check("openflow_rules", "PASS", "Recorded OpenFlow 1.3 entries; inspect raw/flows_after_ping.txt")
    else:
        rec.check("openflow_rules", "SKIPPED", "Standalone flow dump is not evidence of Ryu programming")
    if loss != 0:
        raise RuntimeError("At least one ordered host pair failed pingall")


def runtime_metadata(args, cfg, rec):
    meta = {"started_utc": now(), "switch_mode": args.mode, "config": cfg,
            "python": sys.version.split()[0], "kernel": os.uname().release}
    if Path("/etc/os-release").exists():
        meta["os_release"] = Path("/etc/os-release").read_text()
    rec.raw("run_metadata.json", json.dumps(meta, indent=2) + "\n")


def offloads(net, cfg, rec):
    commands = []
    with ExitStack() as stack:
        for name in HOSTS:
            host_device = str(net[name].defaultIntf())
            host_args = ["ethtool", "-K", host_device, "tso", "off", "gso", "off", "gro", "off"]
            path = rec.out / "raw" / f"offload_{name}.txt"
            p = host_popen(stack, net[name], host_args, path)
            if p.wait(timeout=6):
                raise RuntimeError(f"Offload setup failed on {host_device}: {path.read_text().strip()}")
            switch_device = f'{cfg["switch"]["name"]}-eth{cfg["hosts"][name]["port"]}'
            switch_args = ["ethtool", "-K", switch_device, "tso", "off", "gso", "off", "gro", "off"]
            run_command(switch_args)
            commands += [shlex.join(host_args), shlex.join(switch_args)]
    rec.raw("offload_commands.txt", "\n".join(commands) + "\n")


def record_comparison(rec, cfg):
    from plot_results import compare_tcp
    result = compare_tcp(rec.out / "performance_results.csv")
    rec.raw("dual_tcp_comparison.json", json.dumps(result, indent=2) + "\n")
    means = result["mean_mbps"]
    detail = (f"h1 static {means['static']['h1']:.3f} Mbps, reserved "
              f"{means['reservation']['h1']:.3f} Mbps; configured h1 rate "
              f"{cfg['benchmark']['reserved_mbps']} Mbps. See measured receiver goodput and HTB counters.")
    rec.check("compare_policies", "PASS", detail)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    common_args(parser)
    parser.add_argument("--out", type=Path, default=Path("results") / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--checks", type=Path, help="defaults to OUT/live_checks.csv")
    parser.add_argument("--repeats", type=int, help="override configured repetitions (1..20)")
    args = parser.parse_args(argv)
    try:
        rec = create_records(args.out, args.checks or args.out / "live_checks.csv")
    except (ValueError, OSError) as exc:
        print(f"Cannot create evidence files: {exc}", file=sys.stderr)
        return 2
    try:
        cfg = config_from_args(args)
        if args.repeats is not None:
            from topology import bounded_int
            cfg["benchmark"]["repeats"] = bounded_int(args.repeats, "repeats", 1, 20)
        runtime_metadata(args, cfg, rec)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        rec.check("configuration", "FAIL", str(exc))
        for check in EXPECTED_LIVE:
            rec.check(check, "NOT_RUN", "Invalid configuration")
        return 2
    try:
        preflight(cfg, benchmark=True)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        rec.check("environment_preflight", "BLOCKED", str(exc))
        for check in EXPECTED_LIVE:
            rec.check(check, "NOT_RUN", "Environment preflight blocked live execution")
        return 2
    rec.check("environment_preflight", "PASS", "Root, capabilities, Mininet, OVS and measurement tools available")
    try:
        versions = []
        for command in (["mn", "--version"], ["ovs-vsctl", "--version"], ["iperf3", "--version"], ["tc", "-V"]):
            versions.append(f"{shlex.join(command)}\n{run_command(command)}")
        rec.raw("versions.txt", "\n\n".join(versions) + "\n")
        from mininet.log import setLogLevel
        setLogLevel("info")
        with start_network(cfg, args.mode) as net:
            inspect_network(net, cfg, rec, args.mode)
            offloads(net, cfg, rec)
            for policy in ("static", "reservation"):
                run_policy(net, cfg, rec, args.mode, policy)
            if args.mode == "controller":
                verify_switch(cfg, args.mode)
            record_comparison(rec, cfg)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        rec.check("live_run", "FAIL", str(exc))
        with rec.checks.open(newline="") as file:
            completed = {r["check"] for r in csv.DictReader(file)}
        for check in EXPECTED_LIVE:
            if check not in completed:
                rec.check(check, "NOT_RUN", "Live run stopped early")
        return 1
    rec.check("live_run", "PASS", f"Results in {args.out / 'performance_results.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
