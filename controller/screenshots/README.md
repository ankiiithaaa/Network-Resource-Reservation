# SDN controller evidence

All screenshots and logs from the live run on the Ubuntu VM (15 Nov 2026) are kept in one
place, `results/` at the top of the repository. This table shows where each part of the
controller is proven.

| What | Evidence |
|---|---|
| Controller starts, API listening on 127.0.0.1:8081 | [`01_controller_switch_connected.png`](../../results/screenshots/01_controller_switch_connected.png), [`controller.log`](../../results/logs/controller.log) |
| Switch connects (`Switch connected: dpid=0000000000000001`) | [`01_controller_switch_connected.png`](../../results/screenshots/01_controller_switch_connected.png), [`controller.log`](../../results/logs/controller.log) |
| Reservation accepted → `Installed meter 1 + flows for R001` | [`controller.log`](../../results/logs/controller.log), [`06_server_log_accept_reject_expiry.png`](../../results/screenshots/06_server_log_accept_reject_expiry.png) |
| Flows and meters on s1 (`meter:1`, `rate=4000`; `meter:2`, `rate=3000`) | [`05_demo_TC01_TC02_TC03_TC07_flows_meters.png`](../../results/screenshots/05_demo_TC01_TC02_TC03_TC07_flows_meters.png) |
| Over-capacity request rejected, nothing installed | [`05_demo_TC01_TC02_TC03_TC07_flows_meters.png`](../../results/screenshots/05_demo_TC01_TC02_TC03_TC07_flows_meters.png), [`server.log`](../../results/logs/server.log) |
| Expiry removes flow and meter (`Removed meter 2 + flows for R002`) | [`controller.log`](../../results/logs/controller.log), [`mininet_demo.log`](../../results/logs/mininet_demo.log) |
| Traffic limited by the meter (UDP 10 → 3.92 Mbps) | [`09_iperf_udp_meter_cap_4mbps.png`](../../results/screenshots/09_iperf_udp_meter_cap_4mbps.png) |
