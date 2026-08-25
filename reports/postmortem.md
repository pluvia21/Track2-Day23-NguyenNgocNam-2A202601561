# Postmortem - DR Drill Lab 23

This is a blameless review of the final Region A outage drill.

## 1. Timeline

| ISO time | Event | Evidence |
|---|---|---|
| 2026-08-25T11:33:29Z | Region A outage begins | `chaos/chaos-events.jsonl:4` |
| 2026-08-25T11:33:29Z | First user request fails | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T11:33:50Z | Health checker marks A `UNHEALTHY` | `reports/health-events.jsonl:1` |
| 2026-08-25T11:33:51Z | Snapshot restored and B becomes ready | `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:4` |
| 2026-08-25T11:33:51Z | DNS cutover to B | `reports/failover-events.jsonl:5` |
| 2026-08-25T11:33:53Z | First successful request from B; incident technically recovered | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO versus target and gap

- RTO target: 300s; measured RTO: 24.1s; gap: 275.9s under target.
- RPO target: 300s; measured RPO: 10.0s and 5 documents lost; gap: 290.0s under target.
- Largest contributor: health-check detection at 20.9s. The configured
  detection floor is 15.0s (`interval_s=5.0`, `threshold=3`), with the extra
  time coming from the 2s probe timeout and polling alignment.

## 3. Root cause - five whys

1. Users received errors because the edge still pointed to Region A after A
   stopped responding.
2. The edge did not move immediately because failover is intentionally gated
   by consecutive readiness failures to prevent flapping.
3. Detection took 20.9s because the checker uses three consecutive failures,
   a 5s interval, and a bounded request timeout.
4. The system could recover because replication had produced a filesystem
   snapshot before the outage and the runbook restored it into B.
5. The remaining user-visible delay was caused by the health-check safety
   threshold and the edge cache, not by an unmeasured manual guess.

## 4. Action items

| # | Action item | Owner | Deadline | Expected improvement |
|---|---|---|---|---|
| 1 | Keep the independent health checker and alert on missing `UNHEALTHY` events. | SRE | Next game day | Prevents silent failover delay and preserves anti-flap behavior. |
| 2 | Measure whether reducing interval from 5s to 2s is safe under noisy network conditions. | Platform | Next sprint | Potentially removes up to 9s from detection, with flapping risk to evaluate. |
| 3 | Keep replication interval below the 10.0s observed RPO target for production-like workloads. | Data platform | Next sprint | Reduces possible document loss. |

## 5. Required reflection

1. `interval * threshold = 5.0s * 3 = 15.0s`; the measured detection phase
   was 20.9s and represented most of the 24.1s RTO.
2. If the interval were reduced to 1s, the theoretical floor would drop from
   15.0s to 3.0s, but the system would pay with more probes and a greater
   risk of false failover/flapping.
3. If an outage lasted six hours and primary data were permanently lost,
   `docs_lost` would represent customer documents absent from the latest
   recoverable snapshot, not merely an abstract age of the backup.
