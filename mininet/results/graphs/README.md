# Graphs

No live data was captured, so there are no charts in the delivery. After a complete Ubuntu run, execute:

```bash
/usr/bin/python3 plot_results.py "$RUN/performance_results.csv"
```

The script writes `goodput.png` and `latency_loss.png` with means and the observed ranges. Use only charts generated from actual benchmark data.
