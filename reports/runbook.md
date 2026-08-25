# Runbook - Primary region down

This runbook is for the on-call engineer operating the local two-region AI
serving stack. The primary is normally region A and the standby is region B.
Use the commands from the repository root.

| # | Step | Copy-paste command | Completion signal | Owner |
|---|---|---|---|---|
| 1 | Confirm outage | `python3 chaos/kill_region.py status` | Primary `/healthz` is unavailable in repeated probes and the standby remains alive; see `reports/runbook-run.jsonl:1`. | On-call |
| 2 | Announce incident and start clock | `Write-Output "Incident started: $(Get-Date -Format o)"` | Incident and outage timestamps are recorded in `reports/runbook-run.jsonl:2`. | Incident commander |
| 3 | Restore state and fail over | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | `reports/failover-events.jsonl:1` through `:5` appear in order after the health checker marks A `UNHEALTHY`. | DR operator |
| 4 | Verify standby state | `curl http://127.0.0.1:8002/v1/state` | Region B has a non-zero vector count, model weights are present, and the pool is `full`; see `reports/runbook-run.jsonl:4`. | DR operator |
| 5 | Verify DNS/LB cutover | `curl http://127.0.0.1:8080/edge/state` | `active_region` is `b`; the authoritative cutover event is `reports/failover-events.jsonl:5`. | Network/LB owner |
| 6 | Verify golden signals | `1..10 | ForEach-Object { curl.exe -s http://127.0.0.1:8002/v1/infer }` | Ten requests return HTTP 200 from B with no errors; the automated drill recorded this in `reports/runbook-run.jsonl:6`. | Service owner |
| 7 | Measure and document | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output is `valid: true`, `warnings: []`, and `rto_verdict: PASS`; see `reports/drill-2-withdr.jsonl:36` for recovery. | Incident commander |

## Rollback / failback

Do not return traffic to A merely because its process responds to `/healthz`.
Fail back only when A has restored state and model weights, `/readyz` returns
200 continuously for at least three probes, and the incident commander plus
the DR operator agree that B is no longer needed. The incident commander has
authority to trigger failback; the DR operator executes it after validation.

If B returns errors, has an empty vector database, lacks weights, or fails the
golden-signal check, keep traffic on the last known-good region and abort the
failback. Never edit `edge/active_region` before readiness is confirmed.
