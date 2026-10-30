# Benchmark summary

Latest run per machine. *Usable users*: highest tested concurrency where the median user gets ≥ 10 tok/s and p95 TTFT ≤ 5s. *$/1M tok*: hourly price ÷ aggregate output throughput at that level (machine kept busy).

| Machine | Hardware | Model | pp512 tok/s | tg128 tok/s | TTFT (1 user) | Peak agg tok/s | Usable users | Spot $/h | $/1M tok (spot) | $/1M tok (on-demand) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| local | 6 vCPU, 4 GB RAM | qwen2.5-0.5b-q4 | 289 | 67.2 | 0.02s | 59 | 1 | – | – | – |
