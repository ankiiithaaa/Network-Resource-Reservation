#!/usr/bin/env python3
"""Compare only measured, matched runs and plot mean with observed min/max."""
import argparse
import csv
import math
from pathlib import Path
from statistics import mean
import sys

POLICIES = ("static", "reservation")
COLORS = ("#64748b", "#087f71")


def samples(csv_path, scenario, field, sources):
    with Path(csv_path).open(newline="") as f:
        rows = list(csv.DictReader(f))
    selected = {p: {s: {} for s in sources} for p in POLICIES}
    modes = set()
    for row in rows:
        p, s = row["policy"], row["source"]
        if p not in selected or s not in selected[p] or row["scenario"] != scenario:
            continue
        modes.add(row["switch_mode"])
        repeat = int(row["repeat"])
        if repeat in selected[p][s]:
            raise ValueError(f"Duplicate repeat: {scenario} {p}/{s}/{repeat}")
        value = float(row[field]) if row[field] else None
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"Invalid measured value for {field}")
        selected[p][s][repeat] = value
    groups = [selected[p][s] for p in POLICIES for s in sources]
    expected = set(groups[0])
    if not expected or any(set(g) != expected for g in groups) or len(modes) != 1:
        raise ValueError(f"No complete matched observations for {scenario}/{field}")
    return selected, sorted(expected), modes.pop()


def compare_tcp(csv_path):
    groups, repeats, mode = samples(csv_path, "dual_tcp", "throughput_mbps", ("h1", "h2"))
    if any(v is None for p in POLICIES for group in groups[p].values() for v in group.values()):
        raise ValueError("Missing TCP goodput observations")
    return {
        "switch_mode": mode, "repeats": len(repeats), "scenario": "dual_tcp",
        "mean_mbps": {
            p: {**{s: mean(groups[p][s].values()) for s in ("h1", "h2")},
                "combined": mean(groups[p]["h1"][r] + groups[p]["h2"][r] for r in repeats)}
            for p in POLICIES
        },
    }


def write_graphs(csv_path, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    compare_tcp(csv_path)
    figures = [
        ("goodput.png", "Receiver goodput", (
            ("dual_tcp", "throughput_mbps", ("h1", "h2"), "Concurrent TCP", "Mbps"),
            ("dual_udp", "throughput_mbps", ("h1", "h2"), "Concurrent UDP", "Mbps"),
        )),
        ("latency_loss.png", "Latency and packet loss", (
            ("idle_ping", "rtt_avg_ms", ("h1",), "Idle ICMP · h1 → h3", "Mean RTT (ms)"),
            ("dual_tcp_ping", "rtt_avg_ms", ("h2",), "Under TCP load · h2 → h3", "Mean RTT (ms)"),
            ("dual_udp_ping", "icmp_loss_pct", ("h2",), "Under UDP load · h2 → h3", "ICMP loss (%)"),
            ("dual_udp", "udp_loss_pct", ("h1", "h2"), "Concurrent UDP", "UDP loss (%)"),
        )),
    ]
    output.mkdir(parents=True, exist_ok=True)
    for filename, title, panels in figures:
        fig, axes = plt.subplots(1 if len(panels) == 2 else 2, 2,
                                 figsize=(10, 4.6 if len(panels) == 2 else 7),
                                 squeeze=False)
        mode, repeats = "", []
        for ax, (scenario, field, sources, label, unit) in zip(axes.flat, panels):
            groups, repeats, mode = samples(csv_path, scenario, field, sources)
            for j, policy in enumerate(POLICIES):
                for i, source in enumerate(sources):
                    vals = [v for v in groups[policy][source].values() if v is not None]
                    x = i + (j - 0.5) * 0.35
                    if vals:
                        avg = mean(vals)
                        ax.bar(x, avg, width=0.29, color=COLORS[j], zorder=3,
                               yerr=[[avg - min(vals)], [max(vals) - avg]], capsize=3)
                        ax.annotate(f"{avg:.2f}" + (f" (n={len(vals)})" if len(vals) < len(repeats) else ""),
                                    (x, max(vals)), xytext=(0, 5), textcoords="offset points",
                                    ha="center", fontsize=8, color=COLORS[j])
                    else:
                        ax.text(x, 0.07, "N/A", transform=ax.get_xaxis_transform(),
                                ha="center", fontsize=8, color=COLORS[j])
            ax.set_xticks(range(len(sources)))
            ax.set_xticklabels([s + " → h3" for s in sources])
            ax.set_xlim(-0.55, len(sources) - 0.45)
            ax.set_ylim(bottom=0, top=ax.get_ylim()[1] * 1.18)
            ax.set_title(label, loc="left", fontsize=11, weight="bold")
            ax.set_ylabel(unit, fontsize=9)
            ax.grid(axis="y", color="#e2e8f0")
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        from matplotlib.patches import Patch
        fig.suptitle(title, fontsize=16, weight="bold", y=0.98)
        fig.legend([Patch(color=c) for c in COLORS], ["Static", "Reservation"],
                   loc="upper center", bbox_to_anchor=(0.5, 0.93), ncol=2, frameon=False)
        fig.text(0.5, 0.025, f"Mode: {mode}  ·  {len(repeats)} repeats per case  ·  "
                 "Bars show means, whiskers show observed min–max",
                 ha="center", fontsize=9, color="#475569")
        fig.tight_layout(rect=(0.02, 0.07, 0.98, 0.85))
        fig.savefig(output / filename, dpi=160)
        plt.close(fig)
        print(output / filename)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        write_graphs(args.csv, args.out or args.csv.parent / "graphs")
    except (ImportError, ValueError, OSError, KeyError) as exc:
        print(f"No complete plots produced: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
