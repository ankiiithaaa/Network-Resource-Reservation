# Project 15 · Network Resource Reservation System
## Person 3 — Mininet, Open vSwitch and network performance

This package builds a three-host Mininet lab, connects Open vSwitch to Person 2's Ryu controller using OpenFlow 1.3, and runs a reproducible static-versus-reservation benchmark. It does not replace Person 1's TCP admission server or Person 2's dynamic reservation controller.

**Status.** Run live on the team's Ubuntu VM on 15 Nov 2026: topology, controller connection, `pingall` (0 % loss) and the full static-vs-reservation benchmark all passed. See [results/README.md](results/README.md) and [test_results/README.md](test_results/README.md). The professor's *Mininet Installation Manual on Existing Linux VM (Ubuntu)*, pp. 2–5, is reflected below.

### 1. Install and verify Mininet on the Ubuntu VM

The professor's VM recommendations are Ubuntu 20.04 or 22.04 with sudo and Internet connectivity. Prefer Ubuntu 22.04 for the bundled plotting instructions. If working from Windows, use an Ubuntu VM in VirtualBox or VMware as in the guide. WSL2 is only an alternative if its kernel and OVS setup pass the smoke test.

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install mininet -y
sudo apt install -y openvswitch-switch iperf3 iproute2 iputils-ping ethtool zip unzip python3-matplotlib
sudo systemctl start openvswitch-switch
mn --version
ovs-vsctl --version
iperf3 --version
sudo mn
```

At the Mininet prompt, run `pingall`, then `exit`. Next run:

```bash
sudo mn --topo single,3
```

At the prompt, run `nodes`, `net`, `dump`, `pingall`, then `exit`. Expect **0% packet loss**, with six directed pings for the three-host topology. These are expectations, not measurements bundled with this ZIP.

If bare `mn` complains that a default OpenFlow controller is missing, use OVS bridge mode for the installation smoke test. This is also the fallback shown in the professor's guide screenshot on p. 3.

```bash
sudo mn --switch ovsbr --test pingall
sudo mn --topo single,3 --switch ovsbr
```

For the second command, run the same Mininet commands as above. If a failed or interrupted Mininet process leaves stale interfaces, first stop other Mininet sessions, then run `sudo mn -c` **on the dedicated lab VM**. Cleanup can affect other Mininet bridges and processes.

### 2. Unzip and share the network configuration

```bash
unzip Person3_Mininet_Network.zip
cd Person3_Mininet_Network
/usr/bin/python3 -m unittest discover -s tests -v
```

| Node | IPv4 address | MAC | OVS port / interface | Suggested role |
|---|---|---|---|---|
| h1 | 10.0.0.1/24 | 02:00:00:00:00:01 | 1 / s1-eth1 | Reserved client |
| h2 | 10.0.0.2/24 | 02:00:00:00:00:02 | 2 / s1-eth2 | Competing client |
| h3 | 10.0.0.3/24 | 02:00:00:00:00:03 | 3 / s1-eth3 | TCP app or iPerf3 server |
| s1 | No data-plane IP required | — | DPID 0000000000000001 | OpenFlow 1.3 OVS switch |
| c0 | 127.0.0.1 by default | — | TCP 6653 | Ryu control plane |

The hosts are on one subnet. No router or default gateway is needed for these tests. The controller address refers to the **Ubuntu VM's root network namespace** and is independent of the Mininet host IPs. Edit `config/network.json` or use `--controller-ip` and `--controller-port`. Use the same config for the demo and benchmark.

### 3. Run the custom topology with Ryu

Terminal A, on the controller machine. Start Person 2's Ryu app using their runtime and arguments. If that environment already has a working Ryu installation, this reference app verifies basic forwarding only:

```bash
ryu-manager --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653 ryu.app.simple_switch_13
```

If the controller is on another VM, have Person 2 bind its VM/LAN address and set that address in `config/network.json`. Do not start two listeners on the same address and port. The simple switch demo is not the team's reservation controller.

Terminal B, in the project folder on the Mininet VM:

```bash
mkdir -p test_results
script -q -e -c 'sudo /usr/bin/python3 topology.py' test_results/topology_terminal.log
```

At the Mininet prompt, enter these commands one line at a time:

```text
nodes
net
dump
pingall
h1 ping -c 4 10.0.0.3
h2 ping -c 4 10.0.0.3
sh ovs-vsctl get-controller s1
sh ovs-vsctl --columns=target,is_connected list Controller
sh ovs-ofctl -O OpenFlow13 show s1
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

If the first reactive ping drops during ARP/MAC learning, run `pingall` again and record both results. The controller connection must be `is_connected: true`. The flow dump should show actual forwarding entries after traffic for the reference learning switch. Verify any team-specific meter or queue actions separately with Person 2. Exit this CLI before starting the benchmark.

To check the data plane without Ryu, run `sudo /usr/bin/python3 topology.py --mode standalone`. This is a troubleshooting mode and is not controller evidence.

### 4. Measure the two policies

With Ryu still running and the interactive topology closed, run the benchmark and plotter in one command:

```bash
bash scripts/run_benchmark.sh
```

For a quick rehearsal, use `bash scripts/run_benchmark.sh --repeats 1`. The equivalent commands if you want to keep the output directory in `$RUN` are:

```bash
RUN="results/run-$(date -u +%Y%m%dT%H%M%SZ)"
sudo /usr/bin/python3 test_cases.py --out "$RUN"
sudo chown -R "$(id -u):$(id -g)" "$RUN"
/usr/bin/python3 plot_results.py "$RUN/performance_results.csv"
```

Run the commands from the project directory. The generated directory contains `performance_results.csv`, `live_checks.csv`, raw iPerf3 JSON, ping output, OVS/flow dumps, and HTB counters. Plots go under its `graphs/` folder. The full default is three repetitions per case and should take roughly three minutes plus startup and teardown. To benchmark without Ryu, add `--mode standalone` and use a separate output folder. Don't combine results from the two modes in one comparison.

| Case | Traffic | Measurement |
|---|---|---|
| Connectivity | Every ordered host pair | `pingall`, ICMP loss and RTT in raw warm-up probes |
| Idle latency | h1 → h3 | Mean round-trip time and ICMP loss |
| Single TCP | h1 → h3 on port 5201 | Receiver goodput |
| Competing TCP | h1 → h3 on 5201 and h2 → h3 on 5202 | Each receiver goodput and a separate ICMP probe during load |
| Competing UDP | Same flows, each offering 12 Mbps by default | Receiver goodput, UDP loss and jitter, and ICMP under load |
| QoS and comparison | Both policies at the same 10 Mbps bottleneck | HTB counter deltas plus matched dual-TCP goodput |

**Static** puts all traffic into one best-effort class below a 10 Mbps parent. **Reservation** keeps that same parent and adds a 6 Mbps HTB class for h1 → h3 on TCP/UDP destination port 5201, with 4 Mbps assigned to the default class. Both can borrow idle capacity up to 10 Mbps. The shaper is on `s1-eth3`, so it governs traffic toward h3, not the reverse direction. The 6 Mbps is a configured scheduling rate, not a measured goodput or a promised application-level minimum. Actual TCP payload throughput may be lower because of headers, contention, and timing. A PASS for `compare_policies` means matched samples were successfully compared, not that a target gain was guaranteed.

The benchmark records one observation per flow or ping probe, disables segmentation/aggregation offloads on lab veths, and retains raw output rather than treating missing data as zero. TCP and UDP goodput are receiver measurements. ICMP is round-trip latency, not one-way latency. TCP retransmissions are not reported as packet loss. No graph is generated when complete matched data is missing.

### 5. Complete integration and collect submission evidence

1. Give Person 1 and Person 2 [INTEGRATION.md](INTEGRATION.md) and the network configuration. Use the same Ubuntu VM for all three modules where practical. Confirm actual controller and application settings before changing defaults.
2. Run the custom topology and take screenshots using [screenshots/README.md](screenshots/README.md). Do not use example screenshots from the professor's guide as project evidence.
3. Run the full benchmark and inspect its CSVs. Copy measured files into the top-level submission layout if your team requires it. Preserve the original run folder or its raw evidence.

```bash
cp "$RUN/performance_results.csv" results/performance_results.csv
cp "$RUN/live_checks.csv" test_results/live_checks_ubuntu.csv
cp "$RUN"/graphs/*.png results/graphs/
```

The delivered header-only CSV is replaced **only after** real measurements exist. Keep the corresponding raw logs in your final archive. To create the submission ZIP from the directory above the module:

```bash
zip -r Person3_Mininet_Network.zip Person3_Mininet_Network -x '*/__pycache__/*' '*/.venv*/*'
```

### Troubleshooting

| Symptom | What to check |
|---|---|
| `sudo` missing, veth rejected or namespace operation not permitted | Use the Ubuntu VM with sudo, not a restricted container. |
| OVS database socket missing | Run `sudo systemctl status openvswitch-switch` and start the service. |
| No controller connection | Confirm Ryu is running and the IP/port matches. On the controller machine, check `ss -ltn` for its listener. |
| Connected but no host communication | Confirm the Ryu app handles OF1.3 table-miss, ARP and forwarding. Check `dump-flows`, port mapping and app logs. |
| Existing `s1` or busy ports | Close the other Mininet run. Stop Person 1's service if it binds the same iPerf3 port. Use lab cleanup only after stopping existing sessions. |
| HTB / flower unsupported or counters not increasing | Check the kernel qdisc/classifier modules, use the recommended VM, and inspect `raw/*tc*.json`. Do not report enforcement without evidence. |
| No plots | Install `python3-matplotlib` and use the CSV from a full successful run. A partial or blocked run is not plotted. |

Upstream Mininet package/install and bridge smoke test reference — <https://mininet.org/download/>
