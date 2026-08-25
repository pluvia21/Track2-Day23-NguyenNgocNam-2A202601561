"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def _alive(region: str, timeout: float = 1.0) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[region]}/healthz", timeout=timeout)
        return response.status_code == 200, f"http_{response.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


def _last_outage(primary: str) -> float | None:
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    latest = None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("action") == "kill" and record.get("region") == primary:
            latest = record.get("ts")
    return latest


def _wait_for_health_detection(primary: str, outage_ts: float | None,
                               timeout: float = 60.0) -> dict | None:
    """Wait for the independent health checker to confirm the outage.

    The failover clock must include detection.  Cutting over immediately after
    a single operator probe would make the drill look faster than the actual
    health-check-based design and can cause two automation paths to race.
    """
    path = pathlib.Path("reports/health-events.jsonl")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (record.get("event") == "state_change"
                        and record.get("region") == primary
                        and record.get("to") == "UNHEALTHY"
                        and (outage_ts is None or record.get("ts", 0) >= outage_ts)):
                    return record
        time.sleep(0.25)
    return None


def _golden_signals(target: str, count: int = 10) -> dict:
    latencies = []
    errors = 0
    served_by = []
    with httpx.Client(timeout=3.0) as client:
        for i in range(count):
            started = time.time()
            try:
                response = client.get(f"{URL[target]}/v1/infer",
                                      params={"q": f"hoa don thang {i % 12 + 1}"})
                latencies.append((time.time() - started) * 1000)
                if response.status_code != 200:
                    errors += 1
                else:
                    served_by.append(response.json().get("region"))
            except Exception:
                latencies.append((time.time() - started) * 1000)
                errors += 1
    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, int(0.95 * len(ordered)) - 1))
    return {"requests": count, "p95_latency_ms": round(ordered[p95_index], 1),
            "error_rate": round(errors / count, 3) if count else None,
            "errors": errors, "served_by": served_by}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions")

    primary_checks, target_checks = [], []
    for _ in range(3):
        p_alive, p_reason = _alive(primary)
        t_alive, t_reason = _alive(target)
        primary_checks.append({"alive": p_alive, "reason": p_reason})
        target_checks.append({"alive": t_alive, "reason": t_reason})
    outage_confirmed = (all(not check["alive"] for check in primary_checks)
                        and target_checks[-1]["alive"])
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_checks=primary_checks, target_checks=target_checks,
         outage_confirmed=outage_confirmed)

    outage_ts = _last_outage(primary)
    incident_ts = time.time()
    step(2, "thong_bao_incident", primary=primary, target=target,
         outage_ts=outage_ts, incident_ts=incident_ts,
         notification_delay_s=(None if outage_ts is None
                                else round(incident_ts - outage_ts, 2)),
         rto_clock_started_at=incident_ts)

    detection = _wait_for_health_detection(primary, outage_ts)
    if detection is None:
        step(3, "scale_gpu_pool", ok=False, aborted=True,
             reason="health_checker_detection_timeout")
        return {"ok": False, "aborted": True,
                "reason": "health_checker_detection_timeout"}

    if not outage_confirmed:
        step(3, "scale_gpu_pool", ok=False, aborted=True,
             reason="outage_not_confirmed")
        return {"ok": False, "aborted": True, "reason": "outage_not_confirmed"}
    if not confirm(auto, f"Fail over traffic from region-{primary} to region-{target}?"):
        step(3, "scale_gpu_pool", ok=False, aborted=True,
             reason="operator_declined")
        return {"ok": False, "aborted": True, "reason": "operator_declined"}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         failover_ok=bool(failover_result.get("ok")),
         health_detection=detection,
         failover_result=failover_result)
    if not failover_result.get("ok"):
        step(4, "verify_state_replica", ok=False, reason="failover_failed")
        return {"ok": False, "failover": failover_result}

    target_state = failover_result.get("target_state", {})
    replica_ok = bool(target_state.get("count", 0) > 0
                      and target_state.get("weights"))
    step(4, "verify_state_replica", ok=replica_ok,
         vector_count=target_state.get("count"),
         weights=target_state.get("weights"), target_state=target_state)

    cutover_ok = bool(failover_result.get("cutover_ok"))
    step(5, "dns_cutover", ok=cutover_ok,
         active_region=target if cutover_ok else None,
         cutover_ts=failover_result.get("cutover_ts"))

    signals = _golden_signals(target, count=10)
    step(6, "verify_golden_signals", ok=signals["error_rate"] == 0,
         **signals)

    elapsed_s = round(time.time() - incident_ts, 2)
    step(7, "post_incident", ok=bool(replica_ok and cutover_ok),
         elapsed_s=elapsed_s,
         measure_command=("python3 tools/measure_rto.py --loadgen "
                          "reports/drill-2-withdr.jsonl --target-rto 300"))
    return {"ok": bool(replica_ok and cutover_ok and signals["error_rate"] == 0),
            "failover": failover_result, "signals": signals,
            "elapsed_s": elapsed_s}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
