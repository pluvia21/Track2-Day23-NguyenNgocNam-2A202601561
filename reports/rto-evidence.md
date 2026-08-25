# RTO/RPO Evidence - Lab 23

All values below come from the final local drill. Evidence references use the
format `path:line` and point to actual generated log lines.

## 1. Drill 1 - no DR

| Metric | Measured result | Evidence |
|---|---:|---|
| Outage start | 2026-08-25T11:29:25Z | `chaos/chaos-events.jsonl:1` |
| First failed request | +0.2s | `reports/drill-1-nodr.jsonl:13` |
| Successful request after outage | None | `reports/drill-1-nodr.jsonl:13` |
| RTO verdict | `NO_RECOVERY` | `reports/drill-1-nodr.jsonl:13` and `tools/measure_rto.py` |

The baseline had failed requests after the kill and no recovery served by a
surviving region.

## 2. Drill 2 - DR enabled

| Milestone | Seconds from outage | Evidence |
|---|---:|---|
| Outage starts | 0.0 | `chaos/chaos-events.jsonl:4` |
| User sees first error | 0.1 | `reports/drill-2-withdr.jsonl:25` |
| Health checker detects A unhealthy | 20.9 | `reports/health-events.jsonl:1` |
| Snapshot restore completes | 21.3 | `reports/failover-events.jsonl:2` |
| Region B ready | 21.5 | `reports/failover-events.jsonl:4` |
| DNS cutover | 21.5 | `reports/failover-events.jsonl:5` |
| First successful request from B | 24.1 | `reports/drill-2-withdr.jsonl:36` |

| Metric | Measured | Target | Verdict |
|---|---:|---:|---|
| RTO - inference API | 24.1s | 300s | PASS |
| RPO - vector DB | 10.0s / 5 docs | 300s | PASS |

`tools/measure_rto.py` returned `valid: true`, `warnings: []`,
`rto_verdict: PASS`, and recovery served by region B.

## 3. RTO component breakdown

The measured components sum to 24.1s. The health-check configuration floor is
`interval_s * threshold = 5.0s * 3 = 15.0s`; the measured detection interval
also includes request timeout and polling alignment.

| Component | Measured seconds | How it was measured | Evidence |
|---|---:|---|---|
| Health-check detection | 20.9s | `t_detect - t_outage`; configured floor is 15.0s | `reports/health-events.jsonl:1` |
| Snapshot restore | 0.4s | health detection to restore event | `reports/failover-events.jsonl:2` |
| GPU pool warm-up | 0.2s | scale pool to `/readyz` 200; `waited_s` was 0.18s | `reports/failover-events.jsonl:4` |
| DNS/LB TTL cache | 2.6s | first successful request minus cutover | `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:36` |
| **Total RTO** | **24.1s** | Sum of the four measured components | `reports/drill-2-withdr.jsonl:36` |

The standby snapshot contained model version `embed-model=vi-e5-base@v3` and
the restore event recorded both `rpo_seconds: 10.0` and `docs_lost: 5` in
`reports/failover-events.jsonl:2`.
