# Person 2 – SDN Controller & Network Policy (Project 15: Network Resource Reservation System)

Ryu (OpenFlow 1.3) controller that receives reservation decisions from the TCP
reservation server over a local HTTP/JSON API and turns them into network policy
on Open vSwitch: **accepted reservation → OpenFlow meter + flow, release/expiry →
flow and meter removed, rejected request → nothing allocated.**

```
 TCP clients ──► TCP reservation server (Person 1) ──HTTP/JSON──► controller.py (this module)
                                                                       │ OpenFlow 1.3
                                                              Open vSwitch s1 (Mininet, Person 3)
```

## 1. Do I need Ubuntu?

| Part | Needs Linux? |
|---|---|
| `controller.py` (Ryu) + Mininet + Open vSwitch | **Yes.** Mininet and OVS only run on Linux. |
| `mock_controller.py` + unit tests + `curl` smoke test | **No.** Plain Python 3.7+, runs on Windows/macOS/Linux. |

On a Windows/Mac laptop use an **Ubuntu 20.04 VM** (VirtualBox/VMware), or the
official Mininet VM image. WSL2 can work but Open vSwitch kernel support is
unreliable there, so a VM is the safe choice. Ubuntu 20.04 is recommended because
its Python 3.8 matches Ryu 4.34 (see Limitations).

## 2. Files

| File | Purpose |
|---|---|
| `controller.py` | Ryu app: switch handling, OpenFlow meter/flow install & removal, starts the API and the expiry timer |
| `api_handler.py` | HTTP/JSON API (WSGI app) |
| `policy_store.py` | Active-reservation state, admission control (capacity), expiry; no Ryu dependency |
| `mock_controller.py` | Same API without Ryu/Mininet, for developing the TCP-server integration on any OS |
| `config/config.json` | Port, capacity, client→IP map, enforcement direction |
| `tests/` | Unit tests (reservation logic + HTTP API) |
| `mininet_tests/e2e_test.py` | Full test with Mininet + real controller (run on Ubuntu) |
| `mininet_tests/api_smoke_test.sh` | curl-based API test |
| `test_results/` | Saved outputs of the tests that were run |
| `screenshots/` | See `screenshots/README.txt` |

## 3. Installation (Ubuntu 20.04)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip mininet openvswitch-switch iperf
sudo systemctl enable --now openvswitch-switch

unzip Person2_SDN_Controller.zip && cd Person2_SDN_Controller
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# verify
python3 --version                 # 3.8.x expected on Ubuntu 20.04
ryu-manager --version             # ryu-manager 4.34
sudo ovs-vsctl show               # OVS answers
```

## 4. Running (exact commands)

**Terminal 1 – controller** (leave running):
```bash
cd Person2_SDN_Controller && source venv/bin/activate
ryu-manager controller.py
```
Expected log lines: `Controller ready: capacity 10 Mbps, API 127.0.0.1:8081` and
`Reservation API listening on http://127.0.0.1:8081`. OpenFlow listens on 6653 (Ryu default).

**Terminal 2 – network** (Person 3's topology works too; it must use a
`RemoteController` on 127.0.0.1:6653, an OVS switch with `protocols=OpenFlow13`, and
IPs 10.0.0.1–10.0.0.3 for C1–C3):
```bash
sudo mn --controller=remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13 --topo single,4 --mac
```
In the controller log you should now see `Switch connected: dpid=0000000000000001`.

**Terminal 3 – drive and inspect:**
```bash
bash mininet_tests/api_smoke_test.sh                     # API calls
sudo ovs-ofctl -O OpenFlow13 dump-flows  s1              # flows
sudo ovs-ofctl -O OpenFlow13 dump-meters s1              # meters
```

**Fully automated end-to-end test** (controller in Terminal 1 must be running; this builds its own topology, so don't run `mn` at the same time):
```bash
sudo python3 mininet_tests/e2e_test.py      # writes test_results/e2e_results.txt
```

**Without Ryu/Mininet (any OS):**
```bash
python3 mock_controller.py                  # same API on :8081
python3 -m unittest discover -s tests -v    # unit tests
```

If the TCP server runs on a different machine than the controller (e.g. server on
the Windows host, controller in a VM), set `"api_host": "0.0.0.0"` in
`config/config.json` and use the VM's IP (bridged/host-only adapter) in the URL.
There is no authentication, so only do this on a trusted lab network.

## 5. API (for Person 1)

Base URL `http://127.0.0.1:8081`, JSON in/out. **Bandwidth is in Mbps.**

### `POST /api/reservation`

Reserve (your example message works unchanged):
```json
{"action": "reserve", "client_id": "C1", "reservation_id": "R001", "bandwidth": 4}
```
Optional fields: `"duration": 30` (seconds; reservation auto-expires and the controller removes the policy; omit for no expiry) and `"client_ip": "10.0.0.7"` (overrides the `client_map` in config).

Success `200`:
```json
{"status": "ok", "action": "reserve",
 "reservation": {"reservation_id": "R001", "client_id": "C1", "client_ip": "10.0.0.1",
                 "bandwidth_mbps": 4, "meter_id": 1, "created_at": "...",
                 "expires_at": null, "installed_on_switches": ["0000000000000001"]}}
```

Release:
```json
{"action": "release", "reservation_id": "R001"}
```
→ `200 {"status":"ok","action":"release","reservation_id":"R001","released_mbps":4}`

Update the rate of an existing reservation:
```json
{"action": "update", "reservation_id": "R001", "bandwidth": 2}
```

Record a request your server rejected (optional, for the event log; allocates nothing):
```json
{"action": "reject", "client_id": "C2", "reservation_id": "R002", "reason": "no bandwidth"}
```
You do **not** need to call the controller for rejected requests; simply don't send `reserve`.

### Errors
All errors look like `{"status":"error","error":"<code>","message":"..."}`.

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `bad_request`, `unknown_client` | Bad/missing field; client not in `client_map` and no `client_ip` |
| 404 | `not_found` | Unknown `reservation_id` (for `release`: already released/expired – treat as success) |
| 409 | `insufficient_capacity` | Not enough bandwidth (`available_mbps` included) |
| 409 | `duplicate_reservation`, `client_already_reserved` | Same ID reused with different data, or client already holds a reservation |
| 500 | `dataplane_error` | Switch rejected/failed; nothing was allocated |

Repeating an identical `reserve` (same ID, client, bandwidth) returns 200 with `"idempotent": true`, so TCP-server retries are safe.

### Read-only
`GET /api/health` · `GET /api/capacity` · `GET /api/reservations` · `GET /api/reservations/<id>` · `DELETE /api/reservations/<id>` (= release) · `GET /api/events`

## 6. How the policy works

* On switch connect: all old flows/meters are deleted and a priority-0 flow `actions=NORMAL` makes OVS a plain learning switch, so unreserved traffic flows normally.
* `reserve`: adds an OpenFlow **meter** (drop band at `bandwidth × 1000` kbps) and a flow at priority 101 matching `ip,nw_src=<client IP>` with instruction `meter:<id>` then `output:NORMAL`. The flow's cookie equals the meter ID, which is how removal finds it.
* `release` / expiry: deletes flows by cookie, then the meter.
* A switch that (re)connects gets all active reservations replayed.
* `"enforce_direction"` in config: `src` (default, limits traffic the client sends), `dst` (traffic towards the client), `both` (shared meter, aggregate limit).
* Total capacity is `total_bandwidth_mbps` (default 10). The controller re-checks it, so it is a second line of defence behind Person 1's server.

### Coordination with Person 3
* Topology: OVS switch with `protocols=OpenFlow13`, `RemoteController` at 127.0.0.1:6653, client hosts IP-matching `client_map` (C1=10.0.0.1, C2=.2, C3=.3; server host e.g. 10.0.0.4).
* Enforcement here is done by OpenFlow meters, so **no `tc`/OVS QoS queues are needed**. If Person 3 prefers queue-based enforcement instead, the flow would use `set_queue` rather than `meter`; tell me and I'll change `controller.py`.
* Don't also rate-limit the same hosts with `TCLink` bandwidth or `tc` when testing, or you'll measure two limits.

## 7. Testing

What was actually run (outputs in `test_results/`):

| Test | Result | File |
|---|---|---|
| 23 unit tests: admission, capacity, release, expiry, update, duplicates, validation, data-plane failures, rejected requests allocate nothing | **23/23 pass** | `unit_test_output.txt` |
| HTTP API over real sockets incl. your two example messages, 409 rejection, 404, bad JSON | pass (part of the 23) | `unit_test_output.txt` |
| curl smoke test against `mock_controller.py` (reserve, reject, capacity, update, release, expiry) | pass | `api_smoke_test_output.txt`, `mock_controller_log.txt` |

**Live run on Ubuntu (15 Nov 2026):** `controller.py` was run with Ryu 4.34 on the
team's Ubuntu VM with Mininet and Open vSwitch. Controller start-up, switch connection,
meter and flow installation, removal on expiry and iPerf3 enforcement were all verified
live; see `../results/logs/controller.log`, `../results/screenshots/` and the Results
section of the top-level README. The automated `mininet_tests/e2e_test.py` was not run
separately; the same checks were covered by the live demo.

## 8. Known issues and limitations

* **Ryu is old.** Ryu 4.34 needs Python ≤ 3.9/3.10 and `eventlet==0.30.2`; on newer Python/eventlet it crashes at import (e.g. `ALREADY_HANDLED`). Use Ubuntu 20.04 and the pinned `requirements.txt`.
* **Meters police, they don't guarantee.** A meter caps a client at its reserved rate; it does not guarantee that rate under congestion, and unreserved traffic is not throttled. A real guarantee needs queues with a minimum rate (OVS QoS/HTB), which is Person 3's area.
* One active reservation per client (use `update` to change it). Matching is by client IPv4 address; clients must not share an IP.
* Meters need OVS with OpenFlow 1.3 meter support; the kernel datapath supports them in OVS ≥ 2.10 on Linux ≥ 4.15 (Ubuntu 20.04 is fine). If `dump-meters` is empty or flow installs error out, check `ovs-vsctl --version` and the controller log.
* State is in memory: restarting the controller forgets reservations (switch flows are cleared on reconnect, so this is consistent).
* If no switch is connected when `reserve` arrives, the reservation is accepted and replayed when a switch connects (`installed_on_switches` is empty). Check that field if you need to be sure.
* The API has no authentication and defaults to localhost only.
