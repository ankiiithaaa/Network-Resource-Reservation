"""Parse measured results without converting missing data to zero."""
import math
import re


def number(value, label, minimum=0, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value) or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def parse_ping(text):
    summary = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,\s*"
                        r"(?:\+\d+ errors,\s*)?([\d.]+)% packet loss", text)
    if not summary:
        raise ValueError("ping output did not include a packet summary")
    sent, received = map(int, summary.group(1, 2))
    loss = number(float(summary[3]), "ICMP loss", maximum=100)
    if sent <= 0 or not 0 <= received <= sent:
        raise ValueError("Invalid ping packet counts")
    times = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
                      r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    if received and not times:
        raise ValueError("ping received responses but RTT is missing")
    return {"ping_sent": sent, "ping_received": received, "icmp_loss_pct": loss,
            "rtt_avg_ms": float(times[2]) if times else None}


def parse_iperf(server, protocol):
    """Only read the receiver side of these forward tests, including on old iPerf3."""
    if server.get("error"):
        raise ValueError(server["error"])
    end = server["end"]
    if protocol == "TCP":
        summary = end["sum_received"]
    elif protocol == "UDP":
        summary = end.get("sum_received") or end["sum"]
    else:
        raise ValueError(f"Unsupported iPerf protocol: {protocol}")
    if summary.get("sender") is True:
        raise ValueError("Receiver summary unexpectedly marked as sender")
    if protocol == "UDP":
        # Legacy UDP end.sum can report sender bytes even in a server-side
        # document. Server intervals carry the actual bytes received.
        receiver = end.get("sum_received")
        if receiver:
            goodput_bps = number(receiver["bits_per_second"], "receiver bit/s")
        else:
            totals = [i["sum"] for i in server["intervals"] if not i["sum"].get("omitted")]
            if any(i.get("sender") is True for i in totals):
                raise ValueError("UDP intervals unexpectedly marked as sender")
            seconds = sum(number(i["seconds"], "UDP interval seconds") for i in totals)
            if seconds <= 0:
                raise ValueError("No timed UDP receiver intervals")
            goodput_bps = 8 * sum(number(i["bytes"], "UDP interval bytes") for i in totals) / seconds
        loss = summary if "lost_percent" in summary else end["sum"]
        if number(loss["packets"], "UDP packet count") <= 0:
            raise ValueError("No UDP packets counted")
        return {"throughput_mbps": round(goodput_bps / 1e6, 5),
                "udp_loss_pct": number(loss["lost_percent"], "UDP loss", maximum=100),
                "udp_jitter_ms": number(loss["jitter_ms"], "UDP jitter")}
    return {"throughput_mbps": round(number(summary["bits_per_second"], "receiver bit/s") / 1e6, 5)}
