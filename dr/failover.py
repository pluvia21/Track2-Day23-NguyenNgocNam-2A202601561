"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str, timeout: float = 2.0) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def _ready(region: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        ready = response.status_code == 200 and body.get("ready", True)
        reason = ";".join(str(item) for item in (body.get("reasons") or []))
        return ready, reason or f"http_{response.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL:
        raise ValueError("target must be a or b")
    if wait <= 0:
        raise ValueError("wait must be positive")

    primary = "b" if target == "a" else "a"
    result = {"ok": False, "target": target, "backend": backend,
              "primary": primary, "cutover_ok": False}

    try:
        before = state_of(target)
        emit(step="1_verify_target", target=target, ok=True, state=before)
    except Exception as exc:
        emit(step="1_verify_target", target=target, ok=False,
             error=type(exc).__name__)
        result["error"] = f"verify_target: {exc}"
        return result

    try:
        meta = snapshot.get(target, backend)
        primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
        restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        rpo = snapshot.rpo(primary_db, restored_db)
        emit(step="2_restore_snapshot", target=target, ok=True,
             snapshot_at=meta.get("snapshot_at"), restored_at=meta.get("restored_at"),
             rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
             embed_model_version=meta.get("embed_model_version"))
        result.update(snapshot_meta=meta, rpo_seconds=rpo.get("rpo_seconds"),
                      docs_lost=rpo.get("docs_lost"),
                      embed_model_version=meta.get("embed_model_version"))
    except Exception as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             error=type(exc).__name__)
        result["error"] = f"restore_snapshot: {exc}"
        return result

    try:
        pathlib.Path(f"state/region-{target}/pool_state").write_text("full\n", encoding="utf-8")
        emit(step="3_scale_pool", target=target, ok=True, pool_state="full")
    except Exception as exc:
        emit(step="3_scale_pool", target=target, ok=False,
             error=type(exc).__name__)
        result["error"] = f"scale_pool: {exc}"
        return result

    wait_started = time.time()
    deadline = wait_started + wait
    ready = False
    last_reason = "not_checked"
    while time.time() < deadline:
        ready, last_reason = _ready(target, timeout=min(2.0, max(0.1, deadline - time.time())))
        if ready:
            break
        time.sleep(min(0.25, max(0.0, deadline - time.time())))
    waited_s = round(time.time() - wait_started, 2)
    emit(step="4_wait_ready", target=target, ok=ready, waited_s=waited_s,
         reason="readyz_200" if ready else last_reason)
    if not ready:
        result.update(error="target_not_ready", waited_s=waited_s)
        return result

    try:
        pathlib.Path("edge/active_region").write_text(target, encoding="utf-8")
        cutover = emit(step="5_dns_cutover", target=target, ok=True,
                       active_region=target)
        result.update(ok=True, cutover_ok=True, cutover_ts=cutover["ts"],
                      waited_s=waited_s, target_state=state_of(target))
        return result
    except Exception as exc:
        emit(step="5_dns_cutover", target=target, ok=False,
             error=type(exc).__name__)
        result.update(error=f"dns_cutover: {exc}", waited_s=waited_s)
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
