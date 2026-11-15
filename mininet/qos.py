"""Reproducible lab-only Linux HTB policy on s1's egress toward h3."""
from contextlib import contextmanager
import re
import subprocess
from topology import host_ip, run_command


def egress(cfg):
    return f'{cfg["switch"]["name"]}-eth{cfg["hosts"]["h3"]["port"]}'


def policy_commands(cfg, policy):
    if policy not in {"static", "reservation"}:
        raise ValueError(f"Invalid policy: {policy}")
    dev = egress(cfg)
    bench = cfg["benchmark"]
    cap = bench["capacity_mbps"]
    reserved = bench["reserved_mbps"]
    rate = lambda n: f"{n}mbit"
    tc = ["tc"]
    commands = [
        tc + ["qdisc", "add", "dev", dev, "root", "handle", "1:", "htb", "default", "20"],
        tc + ["class", "add", "dev", dev, "parent", "1:", "classid", "1:1",
              "htb", "rate", rate(cap), "ceil", rate(cap)],
        tc + ["class", "add", "dev", dev, "parent", "1:1", "classid", "1:20",
              "htb", "rate", rate(cap if policy == "static" else cap - reserved),
              "ceil", rate(cap), "prio", "1"],
    ]
    if policy == "reservation":
        commands.append(
            tc + ["class", "add", "dev", dev, "parent", "1:1", "classid", "1:10",
                  "htb", "rate", rate(reserved), "ceil", rate(cap), "prio", "0"]
        )
    commands.append(tc + ["qdisc", "add", "dev", dev, "parent", "1:20", "handle", "20:", "fq_codel"])
    if policy == "reservation":
        commands.append(tc + ["qdisc", "add", "dev", dev, "parent", "1:10", "handle", "10:", "fq_codel"])
        for protocol, pref in (("tcp", "10"), ("udp", "11")):
            commands.append(
                tc + ["filter", "add", "dev", dev, "protocol", "ip", "parent", "1:",
                      "pref", pref, "flower", "skip_hw", "src_ip", host_ip(cfg, "h1"),
                      "dst_ip", host_ip(cfg, "h3"), "ip_proto", protocol,
                      "dst_port", str(bench["reserved_port"]), "classid", "1:10"]
            )
    return commands


@contextmanager
def applied(cfg, policy):
    commands = policy_commands(cfg, policy)
    run_command(commands[0])
    try:
        for command in commands[1:]:
            run_command(command)
        yield
    finally:
        p = subprocess.run(["tc", "qdisc", "del", "dev", egress(cfg), "root"],
                           text=True, capture_output=True, timeout=10)
        if p.returncode:
            print(f"HTB cleanup returned {p.returncode}: {p.stderr.strip()}", flush=True)


def snapshot(cfg):
    dev = egress(cfg)
    return {item: run_command(["tc", "-s", item, "show", "dev", dev] +
                              (["parent", "1:"] if item == "filter" else []))
            for item in ("qdisc", "class", "filter")}


def class_bytes(class_dump, classid):
    pattern = rf"(?m)^class htb {re.escape(classid)}(?:\s|$)[^\n]*\n\s+Sent (\d+) bytes"
    found = re.search(pattern, class_dump)
    if not found:
        raise ValueError(f"No HTB byte counter found for {classid}")
    return int(found[1])
