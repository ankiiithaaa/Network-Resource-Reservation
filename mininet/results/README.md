# Results

`performance_results.csv` holds the measured results of the benchmark run on the Ubuntu VM
on 15 Nov 2026 (3 repeats per case, static vs reservation policy, 10 Mbps bottleneck).
The graphs are in `graphs/` (`goodput.png`, `latency_loss.png`).

The complete run, with raw iPerf3 JSON, ping output, OpenFlow flow dumps and HTB
counters, is in `run-20261115T083403812863146Z/`. Blanks in the CSV mean not applicable
or not observed, never zero.
