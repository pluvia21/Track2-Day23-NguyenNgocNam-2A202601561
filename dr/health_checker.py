"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """TODO: trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ready", True):
            return True, "readyz_200"
        reasons = body.get("reasons") or []
        return False, ";".join(str(item) for item in reasons) or f"http_{response.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """TODO: vòng lặp poll + phát hiện transition + ghi JSONL."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    out.parent.mkdir(parents=True, exist_ok=True)
    states = {region: {"state": "HEALTHY", "consecutive_fails": 0} for region in URL}
    deadline = time.time() + max(0.0, duration)

    def emit(**values):
        record = {"ts": time.time(),
                  "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                  "event": "state_change", **values}
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print("HEALTH", json.dumps(record, ensure_ascii=False))

    while time.time() < deadline:
        for region in ("a", "b"):
            ready, reason = probe(region, timeout)
            current = states[region]
            if ready:
                current["consecutive_fails"] = 0
                if current["state"] == "UNHEALTHY":
                    current["state"] = "HEALTHY"
                    emit(region=region, to="HEALTHY", reason=reason,
                         interval_s=interval, threshold=threshold,
                         consecutive_fails=0)
            else:
                current["consecutive_fails"] += 1
                if (current["state"] == "HEALTHY"
                        and current["consecutive_fails"] >= threshold):
                    current["state"] = "UNHEALTHY"
                    emit(region=region, to="UNHEALTHY", reason=reason,
                         interval_s=interval, threshold=threshold,
                         consecutive_fails=current["consecutive_fails"])
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
