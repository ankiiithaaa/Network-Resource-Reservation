# Mininet evidence

All screenshots and logs from the live run on the Ubuntu VM (15 Nov 2026) are kept in one
place, `results/` at the top of the repository. This table shows where each part of the
Mininet module is proven.

| What | Evidence |
|---|---|
| Topology h1–h4 + s1 starts and connects to Ryu (`s1 is_connected=true`) | [`02_topology_pingall_h1_h4.png`](../../results/screenshots/02_topology_pingall_h1_h4.png), [`mininet_demo.log`](../../results/logs/mininet_demo.log) |
| Host connectivity: `pingall` 0 % loss (12 / 12) | [`02_topology_pingall_h1_h4.png`](../../results/screenshots/02_topology_pingall_h1_h4.png) |
| Controller connection | [`01_controller_switch_connected.png`](../../results/screenshots/01_controller_switch_connected.png) |
| OpenFlow rules on s1 (`dump-flows`, `dump-meters`) | [`05_demo_TC01_TC02_TC03_TC07_flows_meters.png`](../../results/screenshots/05_demo_TC01_TC02_TC03_TC07_flows_meters.png), [`mininet_demo.log`](../../results/logs/mininet_demo.log) |
| Bandwidth tests with iPerf3 | [`07_iperf_reserved_vs_unreserved.png`](../../results/screenshots/07_iperf_reserved_vs_unreserved.png), [`09_iperf_udp_meter_cap_4mbps.png`](../../results/screenshots/09_iperf_udp_meter_cap_4mbps.png) |
| Static vs reservation benchmark (all checks PASS) | [`live_checks_ubuntu.csv`](../test_results/live_checks_ubuntu.csv), [`performance_results.csv`](../results/performance_results.csv), graphs in [`../results/graphs/`](../results/graphs/) |
| Analysis of the results | [`results/ANALYSIS.md`](../../results/ANALYSIS.md) |
