#!/usr/bin/env python3
"""
Parses wrk/wrk2 results into a Blue-Green downtime report.

Downtime methodology (timestamp-based — works with both wrk and wrk2):
  Each response is logged with a microsecond-resolution timestamp from
  /proc/uptime. Downtime = the wall-clock gap from the first failed response
  to the last failed response in the contiguous error window during the switch.
  This is accurate regardless of request rate.

  Secondary metric: error_count / total_count * 100 = error rate %

Usage:
  python3 parse_results.py [--rate 100]
  (--rate is now only used for display, not for downtime calculation)

Outputs: results/blue-green-report.json and results/blue-green-report.md
"""
import argparse
import glob
import json
import os
from datetime import datetime, timezone

TMP = "/tmp"
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results"
)
os.makedirs(OUT_DIR, exist_ok=True)


def load_responses():
    """Load all per-thread CSVs and return sorted (timestamp_us, status) list."""
    rows = []
    for path in glob.glob(os.path.join(TMP, "wrk2-results-*.csv")):
        with open(path) as f:
            next(f, None)  # skip header
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) == 2:
                    try:
                        rows.append((int(parts[0]), int(parts[1])))
                    except ValueError:
                        pass
    rows.sort(key=lambda r: r[0])
    return rows


def load_summary():
    summary = {}
    path = os.path.join(TMP, "wrk2-summary.txt")
    if not os.path.exists(path):
        return summary
    with open(path) as f:
        for line in f:
            for token in line.strip().split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    try:
                        summary[k] = float(v)
                    except ValueError:
                        summary[k] = v
    return summary


def compute_downtime_ms(rows):
    """
    Find the contiguous error window closest to the middle of the test
    (that's where the switch happens). Downtime = last_error_ts - first_error_ts.
    Also returns total error count and error rate.
    """
    if not rows:
        return 0.0, 0, 0.0

    total = len(rows)
    errors = [(ts, st) for ts, st in rows if not (200 <= st < 300)]
    error_count = len(errors)
    error_rate = error_count / total * 100 if total else 0.0

    if not errors:
        return 0.0, 0, 0.0

    # Find the largest contiguous block of failures
    # (errors during switch will be clustered together)
    test_start = rows[0][0]
    test_end = rows[-1][0]
    test_mid = (test_start + test_end) // 2

    # Group consecutive error timestamps that are within 2s of each other
    GAP_US = 2_000_000  # 2 seconds
    clusters = []
    cluster_start = errors[0][0]
    cluster_end = errors[0][0]

    for i in range(1, len(errors)):
        if errors[i][0] - errors[i-1][0] <= GAP_US:
            cluster_end = errors[i][0]
        else:
            clusters.append((cluster_start, cluster_end, cluster_end - cluster_start))
            cluster_start = errors[i][0]
            cluster_end = errors[i][0]
    clusters.append((cluster_start, cluster_end, cluster_end - cluster_start))

    # Pick the largest cluster (that's the switch window)
    best = max(clusters, key=lambda c: c[2])
    downtime_ms = round(best[2] / 1000.0, 2)

    # Minimum: if errors are all in one instant, use 1 request * 10ms as floor
    if downtime_ms == 0.0 and error_count > 0:
        downtime_ms = round(error_count * 10.0, 2)

    return downtime_ms, error_count, error_rate


def latency_block(summary):
    def us_to_ms(key):
        return round(summary[key] / 1000, 3) if key in summary else None
    return {
        "min_ms":    us_to_ms("latency_min_us"),
        "mean_ms":   us_to_ms("latency_mean_us"),
        "p50_ms":    us_to_ms("latency_p50_us"),
        "p75_ms":    us_to_ms("latency_p75_us"),
        "p90_ms":    us_to_ms("latency_p90_us"),
        "p99_ms":    us_to_ms("latency_p99_us"),
        "p99_9_ms":  us_to_ms("latency_p99.9_us"),
        "max_ms":    us_to_ms("latency_max_us"),
        "stdev_ms":  us_to_ms("latency_stdev_us"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=100,
                    help="Configured req/s rate (for display only)")
    args = ap.parse_args()

    rows = load_responses()
    summary = load_summary()

    downtime_ms, error_count, error_rate = compute_downtime_ms(rows)
    latency = latency_block(summary)

    total_requests = int(summary.get("requests", len(rows)))
    duration_s = summary.get("duration_us", 0) / 1_000_000 or None
    throughput = round(total_requests / duration_s, 2) if duration_s else None

    LATENCY_GATE = 200
    DOWNTIME_GATE = 100

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "configured_rate_req_per_sec": args.rate,
        "actual_throughput_req_per_sec": throughput,
        "total_requests": total_requests,
        "total_failed_requests": error_count,
        "error_rate_pct": round(error_rate, 4),
        "measured_downtime_ms": downtime_ms,
        "downtime_method": "timestamp-window (first_error to last_error in switch cluster)",
        "latency": latency,
        "pass_fail_gate": {
            "latency_threshold_ms": LATENCY_GATE,
            "downtime_threshold_ms": DOWNTIME_GATE,
            "p99_within_threshold": (
                latency["p99_ms"] is not None and latency["p99_ms"] <= LATENCY_GATE
            ),
            "downtime_under_threshold": downtime_ms <= DOWNTIME_GATE,
        },
    }

    json_path = os.path.join(OUT_DIR, "blue-green-report.json")
    md_path   = os.path.join(OUT_DIR, "blue-green-report.md")

    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    with open(md_path, "w") as f:
        f.write("# Blue-Green Deployment — Measured Results\n\n")
        f.write(f"Generated: {report['generated_at_utc']}\n\n")
        f.write("## Downtime\n\n")
        f.write(
            f"- **Measured downtime during switch:** {downtime_ms} ms "
            f"(timestamp-window method: first→last error in switch cluster)\n\n"
        )
        f.write("## Traffic & Errors\n\n")
        f.write(f"- Total requests    : {total_requests}\n")
        f.write(f"- Failed requests   : {error_count}\n")
        f.write(f"- Error rate        : {round(error_rate, 4)}%\n")
        f.write(f"- Configured rate   : {args.rate} req/s\n")
        f.write(f"- Actual throughput : {throughput} req/s\n\n")
        f.write("## Latency (ms)\n\n")
        for k, v in latency.items():
            f.write(f"- {k}: {v}\n")
        f.write("\n## Gate Result\n\n")
        gate = report["pass_fail_gate"]
        p = "PASS" if gate["p99_within_threshold"]     else "FAIL"
        d = "PASS" if gate["downtime_under_threshold"] else "FAIL"
        f.write(f"- p99 <= {LATENCY_GATE}ms  : {p}\n")
        f.write(f"- downtime <= {DOWNTIME_GATE}ms: {d}\n")

    print(json.dumps(report, indent=2))
    print(f"\nWritten: {json_path}\nWritten: {md_path}")


if __name__ == "__main__":
    main()
