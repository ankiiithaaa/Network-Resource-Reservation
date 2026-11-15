# Test evidence

## Live run on the Ubuntu VM (15 Nov 2026)

Ubuntu 26.04 in VirtualBox, Mininet 2.3.0, Open vSwitch 3.7.1, Ryu 4.34, iPerf 3.20.

| Check | Result | Evidence |
|---|---|---|
| Mininet installation and smoke test | Passed | `../../results/screenshots/02_topology_pingall_h1_h4.png` |
| Topology with h1–h4, s1 connected to Ryu over OpenFlow 1.3 | Passed (`s1 is_connected=true`) | `../../results/logs/mininet_demo.log`, `../../results/screenshots/01_controller_switch_connected.png` |
| Host connectivity (`pingall`) | Passed, 0 % loss (12 / 12) | `../../results/screenshots/02_topology_pingall_h1_h4.png` |
| Static vs reservation benchmark (3 repeats) | All 10 live checks PASS | `live_checks_ubuntu.csv`, `../results/run-20261115T083403812863146Z/` |
| Integration with the TCP server and the reservation controller | Passed (accept / accept / reject, meters installed and removed) | `../../results/logs/`, `../../README.md` (Results) |
| Offline regression tests | 15 / 15 pass | `offline_tests.log` |

## Earlier attempt (before the VM)

The files `workspace_preflight.log`, `topology_attempt.log`, `benchmark_attempt.log`,
`live_checks_workspace.csv` and `plot_attempt.log` record a first attempt in a restricted
workspace without sudo, where Mininet could not start. They are kept as a record only;
the live results above replace them.
