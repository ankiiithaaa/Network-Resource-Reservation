# Screenshot checklist

No successful screenshots were captured in the restricted workspace. Use the real Ubuntu VM and save full terminal screenshots here.

| Filename | What the screenshot must show |
|---|---|
| 01_mininet_installation.png | `mn --version` and the installation smoke test with its actual `pingall` result |
| 02_topology_nodes_ports.png | Running `topology.py`, then `nodes`, `net`, and `dump` |
| 03_host_connectivity.png | Custom topology `pingall` and preferably an h1 → h3 ping with the packet summary |
| 04_ryu_openflow.png | Controller terminal and `ovs-vsctl --columns=target,is_connected list Controller` showing the real status |
| 05_openflow_rules.png | `ovs-ofctl -O OpenFlow13 dump-flows s1` after host traffic |
| 06_benchmark_summary.png | Finished `test_cases.py` run and paths to the real CSV results |

The README lists the exact CLI commands. Screenshot the terminal with Ubuntu's screenshot tool or your VM host's capture shortcut. Keep the actual terminal transcript as well. Do not substitute the professor's screenshots or a generated mockup.
