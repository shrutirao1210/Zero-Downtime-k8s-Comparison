#!/usr/bin/env python3
"""
analyze.py — Full statistical analysis for the 4-strategy deployment comparison.

Fixes applied vs previous version:
  1. p99 latency: read from wrk summary file (latency_p99_us) NOT from
     inter-response gap CDF which hits the 800ms histogram cap
  2. Canary NaN p-value: handled explicitly as "identical distributions"
  3. Recreate resource overhead: shows "availability gap" not pod overhead
  4. Rolling overhead: correctly uses replica_count=1 (actual experiment value)
"""

import argparse, glob, json, os
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

STRATEGIES      = ["blue-green", "rolling", "canary", "recreate"]
STRATEGY_LABELS = {
    "blue-green": "Blue-Green",
    "rolling":    "Rolling Update",
    "canary":     "Canary",
    "recreate":   "Recreate (control)",
}
COLORS = {
    "blue-green": "#2196F3",
    "rolling":    "#4CAF50",
    "canary":     "#FF9800",
    "recreate":   "#F44336",
}
N_BOOTSTRAP = 10_000
ALPHA        = 0.05


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_responses(raw_dir, strategy, run_id):
    rows = []
    for path in glob.glob(os.path.join(raw_dir,
                          f"{strategy}_run{run_id}_responses_*.csv")):
        with open(path) as f:
            next(f, None)
            for line in f:
                p = line.strip().split(",")
                if len(p) == 2:
                    try: rows.append((int(p[0]), int(p[1])))
                    except: pass
    rows.sort()
    return rows


def load_summary(raw_dir, strategy, run_id):
    path = os.path.join(raw_dir, f"{strategy}_run{run_id}_summary.txt")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            for tok in line.strip().split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:    out[k] = float(v)
                    except: out[k] = v
    return out


def load_wrk_stdout(raw_dir, strategy, run_id):
    path = os.path.join(raw_dir,
                        f"{strategy}_run{run_id}_wrk_stdout.txt")
    data = {"requests": 0, "timeouts": 0, "non2xx": 0}
    if not os.path.exists(path):
        return data
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "requests in" in line:
                try:    data["requests"] = int(line.split()[0])
                except: pass
            if "Socket errors" in line:
                for part in line.split(","):
                    part = part.strip()
                    for key in ("connect", "read", "write", "timeout"):
                        if key in part:
                            try:
                                val = int(part.split()[-1])
                                if key == "timeout":
                                    data["timeouts"] += val
                            except: pass
            if "Non-2xx" in line:
                try:    data["non2xx"] = int(line.split()[-1])
                except: pass
    return data


def load_switch_ms(raw_dir, strategy, run_id):
    path = os.path.join(raw_dir,
                        f"{strategy}_run{run_id}_switch_duration_ms.txt")
    try:    return float(open(path).read().strip())
    except: return float("nan")

def load_rollback_ms(raw_dir, strategy, run_id):
    path = os.path.join(raw_dir,
                        f"{strategy}_run{run_id}_rollback_duration_ms.txt")
    try:    return float(open(path).read().strip())
    except: return float("nan")


def discover_runs(raw_dir, strategy):
    ids = set()
    for p in glob.glob(os.path.join(raw_dir,
                       f"{strategy}_run*_responses_*.csv")):
        try:
            ids.add(int(os.path.basename(p).split("_run")[1].split("_")[0]))
        except: pass
    return sorted(ids)


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def downtime_ms(rows):
    errors = [(ts, st) for ts, st in rows if not (200 <= st < 300)]
    if not errors:
        return 0.0
    GAP = 2_000_000
    clusters, cs, ce = [], errors[0][0], errors[0][0]
    for i in range(1, len(errors)):
        if errors[i][0] - errors[i-1][0] <= GAP:
            ce = errors[i][0]
        else:
            clusters.append(ce - cs)
            cs, ce = errors[i][0], errors[i][0]
    clusters.append(ce - cs)
    best = max(clusters)
    return round(best / 1000.0 if best > 0 else len(errors) * 10.0, 3)


def timeout_rate_pct(wrk_data):
    t = int(wrk_data.get("timeouts", 0) or 0)
    completed = int(wrk_data.get("requests", 0) or 0)
    total = completed + t
    return round(t / total * 100, 4) if total > 0 else 0.0


def http_error_rate_pct(rows):
    if not rows:
        return 0.0
    errs = sum(1 for _, st in rows if not (200 <= st < 300))
    return round(errs / len(rows) * 100, 4)


def get_merged_global_p99(raw_dir, strategy):
    """
    Merges the HdrHistograms across all N runs by parsing the Detailed Percentile spectrum
    and calculating the true global P99 across all requests.
    """
    global_hist = defaultdict(int)
    total_requests = 0
    
    for path in glob.glob(os.path.join(raw_dir, f"{strategy}_run*_wrk_stdout.txt")):
        parsing = False
        prev_count = 0
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("Value   Percentile   TotalCount"):
                    parsing = True
                    continue
                if parsing:
                    if not line:
                        continue
                    if line.startswith("#"):
                        parsing = False
                        continue
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            val = float(parts[0])
                            total_count = int(parts[2])
                            count_in_bucket = total_count - prev_count
                            if count_in_bucket > 0:
                                global_hist[val] += count_in_bucket
                            prev_count = total_count
                        except ValueError:
                            pass
    
    total_requests = sum(global_hist.values())
    if total_requests == 0:
        return float("nan")
        
    target_count = total_requests * 0.99
    cumulative = 0
    for val in sorted(global_hist.keys()):
        cumulative += global_hist[val]
        if cumulative >= target_count:
            return round(val, 3)
            
    return float("nan")


def get_merged_global_hist(raw_dir, strategy):
    """
    Returns the merged global histogram (value -> count) for a strategy.
    """
    global_hist = defaultdict(int)
    for path in glob.glob(os.path.join(raw_dir, f"{strategy}_run*_wrk_stdout.txt")):
        parsing = False
        prev_count = 0
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("Value   Percentile   TotalCount"):
                    parsing = True
                    continue
                if parsing:
                    if not line:
                        continue
                    if line.startswith("#"):
                        parsing = False
                        continue
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            val = float(parts[0])
                            total_count = int(parts[2])
                            count_in_bucket = total_count - prev_count
                            if count_in_bucket > 0:
                                global_hist[val] += count_in_bucket
                            prev_count = total_count
                        except ValueError:
                            pass
    return global_hist


def resource_overhead(strategy, n_services=5):
    """
    Pod overhead during upgrade.
    Reconciled with raw cluster logs:
    - blue-green & canary ran with 1 replica per service (scale_and_wait 1 in run-experiment.sh)
    - rolling & recreate ran with 2 replicas per service (from group_vars/all.yml)
    """
    if strategy == "blue-green":
        base = 1 * n_services
        peak = 2 * base
        idle = base
        note = "Standby namespace maintained continuously"
    elif strategy == "rolling":
        base = 2 * n_services
        peak = base + n_services  # maxSurge=1 per service * 5 services
        idle = base  # using idle variable as "Additional Pods" in output
        note = "Temporary surge during rollout"
    elif strategy == "canary":
        base = 1 * n_services
        peak = 2 * base
        idle = base
        note = "Additional canary replicas during staged rollout"
    else:  # recreate
        base = 2 * n_services
        peak = base
        idle = 0
        note = "No additional replicas; temporary service interruption"
    
    overhead = round((peak - base) / base * 100, 1)
    
    # We will repurpose "idle_pods" key in the JSON to mean "additional_pods" 
    # to match the reviewer's requested table column.
    additional = peak - base
    return {
        "base_pods":    base,
        "peak_pods":    peak,
        "idle_pods":    additional,
        "overhead_pct": overhead,
        "note":         note,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

def bootstrap_ci(data, fn=np.median, n=N_BOOTSTRAP, alpha=ALPHA):
    data = np.asarray(data)
    if len(data) == 0:
        return (float("nan"), float("nan"))
    rng   = np.random.default_rng(42)
    boots = np.array([fn(rng.choice(data, len(data), replace=True))
                      for _ in range(n)])
    return (round(float(np.percentile(boots, 100*alpha/2)), 3),
            round(float(np.percentile(boots, 100*(1-alpha/2))), 3))


def cliffs_delta(x, y):
    x, y = np.asarray(x), np.asarray(y)
    if not len(x) or not len(y):
        return float("nan")
    more = sum(xi > yj for xi in x for yj in y)
    less = sum(xi < yj for xi in x for yj in y)
    return round((more - less) / (len(x) * len(y)), 4)


def cliff_label(d):
    a = abs(d)
    if a < 0.147:  return "negligible"
    if a < 0.33:   return "small"
    if a < 0.474:  return "medium"
    return "large"


def kruskal_wallis(groups):
    arrs = [np.asarray(v) for v in groups.values() if len(v) > 1]
    if len(arrs) < 2:
        return float("nan"), float("nan")
    # Skip if all arrays are identical constants (e.g. all zeros)
    if all(np.all(a == arrs[0][0]) for a in arrs):
        return float("nan"), float("nan")
    try:
        H, p = stats.kruskal(*arrs)
        return round(float(H), 4), round(float(p), 6)
    except:
        return float("nan"), float("nan")


def mann_whitney_holm(reference, others):
    """
    Mann-Whitney U with Holm-Bonferroni correction.
    Handles identical constant distributions gracefully (returns N/A).
    """
    raw = {}
    ref = np.asarray(reference)
    for s, arr in others.items():
        arr = np.asarray(arr)
        if not len(arr) or not len(ref):
            raw[s] = {"U": float("nan"), "p_raw": float("nan"),
                      "note": "insufficient data"}
            continue
        # Check if distributions are identical constants
        if (np.all(arr == arr[0]) and np.all(ref == ref[0])
                and arr[0] == ref[0]):
            raw[s] = {"U": float("nan"), "p_raw": float("nan"),
                      "note": "identical distributions — no test applicable"}
            continue
        try:
            U, p = stats.mannwhitneyu(ref, arr, alternative="two-sided")
            raw[s] = {"U": round(float(U), 2), "p_raw": round(float(p), 6)}
        except:
            raw[s] = {"U": float("nan"), "p_raw": float("nan"),
                      "note": "test failed"}

    # Holm-Bonferroni on testable comparisons only
    testable = {s: v for s, v in raw.items()
                if not np.isnan(v.get("p_raw", float("nan")))}
    strats  = list(testable)
    p_vals  = [testable[s]["p_raw"] for s in strats]
    order   = sorted(range(len(strats)), key=lambda i: p_vals[i])
    m       = len(strats)
    p_corr  = [float("nan")] * m
    rej     = [False] * m
    for rank, idx in enumerate(order):
        pc = min(p_vals[idx] * (m - rank), 1.0)
        p_corr[idx], rej[idx] = round(pc, 6), pc < ALPHA
    for i, s in enumerate(strats):
        raw[s]["p_corrected"] = p_corr[i]
        raw[s]["reject_h0"]   = rej[i]
    # Non-testable get explicit label
    for s in raw:
        if s not in testable:
            raw[s]["p_corrected"] = float("nan")
            raw[s]["reject_h0"]   = False
    return raw


# ══════════════════════════════════════════════════════════════════════════════
# PLOTS
# ══════════════════════════════════════════════════════════════════════════════

def boxplot(data_dict, title, ylabel, path, log_scale=False):
    present = [s for s in STRATEGIES
               if s in data_dict and len(data_dict[s]) > 0]
    if not present:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    bp = ax.boxplot([data_dict[s] for s in present],
                    patch_artist=True, widths=0.5,
                    medianprops=dict(color="black", linewidth=2))
    rng = np.random.default_rng(0)
    for i, (s, patch) in enumerate(zip(present, bp["boxes"]), 1):
        patch.set_facecolor(COLORS[s])
        patch.set_alpha(0.7)
        ax.scatter(rng.normal(i, 0.06, len(data_dict[s])),
                   data_dict[s], alpha=0.55, s=22,
                   color=COLORS[s], zorder=3)
    ax.set_xticks(range(1, len(present)+1))
    ax.set_xticklabels([STRATEGY_LABELS[s] for s in present], fontsize=10)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    if log_scale:
        pos_data = [np.asarray(data_dict[s]) for s in present]
        if all(np.any(d > 0) for d in pos_data):
            ax.set_yscale("log")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  Saved: {path}")


def cdf_plot(raw_dir, path):
    """
    True CDF of latency across ALL requests in all 10 runs for each strategy.
    """
    fig, ax = plt.subplots(figsize=(9, 5))
    for s in STRATEGIES:
        hist = get_merged_global_hist(raw_dir, s)
        if not hist:
            continue
        
        # Sort values and compute cumulative probabilities
        sorted_vals = np.array(sorted(hist.keys()))
        counts = np.array([hist[v] for v in sorted_vals])
        cumulative = np.cumsum(counts)
        total = cumulative[-1]
        probs = cumulative / total
        
        ax.plot(sorted_vals, probs,
                label=STRATEGY_LABELS[s], color=COLORS[s],
                linewidth=2)

    ax.set_xlabel("Latency (ms) - Log Scale", fontsize=11)
    ax.set_xscale('log')
    ax.set_ylabel("Cumulative Probability", fontsize=11)
    ax.set_title(
        "True Global Latency CDF (All Requests)\n"
        "(Merged across all 10 runs from wrk2 HdrHistogram)",
        fontsize=11, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_ylim(0, 1.05)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  Saved: {path}")



def resource_plot(path):
    fig, ax = plt.subplots(figsize=(10, 5))
    x     = np.arange(len(STRATEGIES))
    width = 0.28
    base  = [resource_overhead(s)["base_pods"]  for s in STRATEGIES]
    peak  = [resource_overhead(s)["peak_pods"]  for s in STRATEGIES]
    idle  = [resource_overhead(s)["idle_pods"]  for s in STRATEGIES]

    ax.bar(x - width, base, width, label="Baseline pods",
           color="#90CAF9")
    ax.bar(x,         peak, width, label="Peak pods during upgrade",
           color="#1565C0")
    ax.bar(x + width, idle, width, label="Additional pods",
           color="#EF9A9A")

    # Annotate recreate as special case
    recreate_idx = STRATEGIES.index("recreate")
    ax.annotate("Outage gap\n(0 active pods)",
                xy=(x[recreate_idx], 0), xytext=(x[recreate_idx]+0.3, 1.5),
                arrowprops=dict(arrowstyle="->", color="red"),
                color="red", fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels([STRATEGY_LABELS[s] for s in STRATEGIES], fontsize=10)
    ax.set_ylabel("Pod count (1 replica × 5 services)", fontsize=11)
    ax.set_title("Resource Overhead During Upgrade", fontsize=12, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  Saved: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# TABLE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def make_table(per_run, bg_arr, others, metric_label):
    mw = mann_whitney_holm(bg_arr, others)
    rows = []
    for s in STRATEGIES:
        arr = np.asarray(per_run.get(s, []))
        if not len(arr):
            rows.append({"Strategy": STRATEGY_LABELS[s],
                         **{k: "—" for k in
                            ["n","Mean","SD","Median","IQR",
                             "95% CI","p (Holm)","Cliff δ","Note"]}})
            continue
        ci = bootstrap_ci(arr)
        if s == "blue-green":
            p_str, cd_str = "reference", "reference"
            note = ""
        else:
            mwr   = mw.get(s, {})
            note  = mwr.get("note", "")
            p_adj = mwr.get("p_corrected", float("nan"))
            if note:
                p_str = f"N/A ({note})"
            elif np.isnan(p_adj):
                p_str = "—"
            else:
                sig   = " *" if mwr.get("reject_h0") else ""
                p_str = f"{p_adj:.4f}{sig}"
            cd    = cliffs_delta(bg_arr, arr)
            cd_str = (f"{cd:.3f} ({cliff_label(cd)})"
                      if not np.isnan(cd) else "—")

        rows.append({
            "Strategy": STRATEGY_LABELS[s],
            "n":        len(arr),
            "Mean":     round(float(np.mean(arr)), 3),
            "SD":       round(float(np.std(arr, ddof=1)), 3)
                        if len(arr) > 1 else 0.0,
            "Median":   round(float(np.median(arr)), 3),
            "IQR":      f"[{round(float(np.percentile(arr,25)),3)}, "
                        f"{round(float(np.percentile(arr,75)),3)}]",
            "95% CI":   f"[{ci[0]}, {ci[1]}]",
            "p (Holm)": p_str,
            "Cliff δ":  cd_str,
            "Note":     note,
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="experiment/raw")
    ap.add_argument("--out-dir", default="analysis/output")
    args = ap.parse_args()

    root    = Path(__file__).resolve().parent.parent
    raw_dir = root / args.raw_dir
    out_dir = root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Raw data : {raw_dir}\nOutput   : {out_dir}\n")

    # ── Load all runs ──────────────────────────────────────────────────────────
    DT  = defaultdict(list)   # downtime ms
    TR  = defaultdict(list)   # timeout rate %
    HER = defaultdict(list)   # http error rate %
    SW  = defaultdict(list)   # switch duration ms
    RB  = defaultdict(list)   # rollback duration ms
    P99 = defaultdict(list)   # true p99 latency ms (from wrk summary)

    for s in STRATEGIES:
        run_ids = discover_runs(str(raw_dir), s)
        if not run_ids:
            print(f"  WARNING: no data for '{s}'")
            continue
        print(f"  {s}: {len(run_ids)} runs")
        for rid in run_ids:
            rows    = load_responses(str(raw_dir), s, rid)
            summary = load_summary(str(raw_dir), s, rid)
            wrkd    = load_wrk_stdout(str(raw_dir), s, rid)
            sw_ms   = load_switch_ms(str(raw_dir), s, rid)
            rb_ms   = load_rollback_ms(str(raw_dir), s, rid)
            DT[s].append(downtime_ms(rows))
            TR[s].append(timeout_rate_pct(wrkd))
            HER[s].append(http_error_rate_pct(rows))
            if not np.isnan(sw_ms):
                SW[s].append(sw_ms)
            if not np.isnan(rb_ms):
                RB[s].append(rb_ms)
                
        # Calculate merged global P99 directly from stdout histograms
        merged_p99 = get_merged_global_p99(str(raw_dir), s)
        if not np.isnan(merged_p99):
            P99[s] = [merged_p99] # Stored as a single-element list for downstream formatting

    DT_np  = {s: np.array(v) for s, v in DT.items()}
    TR_np  = {s: np.array(v) for s, v in TR.items()}
    HER_np = {s: np.array(v) for s, v in HER.items()}
    SW_np  = {s: np.array(v) for s, v in SW.items()}
    RB_np  = {s: np.array(v) for s, v in RB.items()}
    P99_np = {s: np.array(v) for s, v in P99.items()}

    # ── Kruskal-Wallis ────────────────────────────────────────────────────────
    print("\n── Kruskal-Wallis ───────────────────────────────────────────────")
    for label, npd in [("Downtime (ms)", DT_np),
                       ("HTTP error rate (%)", HER_np),
                       ("Switch duration (ms)", SW_np),
                       ("Rollback duration (ms)", RB_np),
                       ("p99 latency (ms)", P99_np)]:
        H, p = kruskal_wallis(npd)
        status = f"H={H:.3f}  p={p:.6f}" if not np.isnan(H) else \
                 "N/A (all distributions identical)"
        print(f"  {label:<28}  {status}")

    # ── Results tables ────────────────────────────────────────────────────────
    print("\n── Results Tables ───────────────────────────────────────────────")
    bg_dt  = DT_np.get("blue-green",  np.array([]))
    bg_her = HER_np.get("blue-green", np.array([]))
    bg_sw  = SW_np.get("blue-green",  np.array([]))
    bg_rb  = RB_np.get("blue-green",  np.array([]))
    bg_p99 = P99_np.get("blue-green", np.array([]))

    for label, npd, bg, fname in [
        ("Downtime (ms)",         DT_np,  bg_dt,  "table_downtime.csv"),
        ("Timeout rate (%)",      TR_np,  bg_dt,  "table_timeout_rate.csv"),
        ("HTTP error rate (%)",   HER_np, bg_her, "table_http_error_rate.csv"),
        ("Switch duration (ms)",  SW_np,  bg_sw,  "table_switch_duration.csv"),
        ("Rollback duration (ms)",RB_np,  bg_rb,  "table_rollback_duration.csv"),
    ]:
        others = {s: npd[s] for s in STRATEGIES
                  if s != "blue-green" and s in npd}
        tbl    = make_table(npd, bg, others, label)
        print(f"\n  {label}")
        print(tbl.to_string(index=False))
        tbl.to_csv(out_dir / fname, index=False)

    print("\n  True Global P99 Latency (Merged Histograms)")
    p99_rows = []
    for s in STRATEGIES:
        arr = P99_np.get(s, np.array([]))
        if len(arr):
            p99_rows.append({
                "Strategy": STRATEGY_LABELS[s],
                "Global P99 (ms)": arr[0]
            })
    if p99_rows:
        p99_df = pd.DataFrame(p99_rows)
        print(p99_df.to_string(index=False))
        p99_df.to_csv(out_dir / "table_p99_latency_global.csv", index=False)

    # ── Timeout diagnostic ────────────────────────────────────────────────────
    print("\n── Timeout Rate (equal-footing fix validation) ──────────────────")
    for s in STRATEGIES:
        arr = TR_np.get(s, np.array([]))
        if len(arr):
            flag = "✅" if np.mean(arr) < 1.0 else "⚠  still throttling"
            print(f"  {STRATEGY_LABELS[s]:<28}  "
                  f"mean = {np.mean(arr):.2f}%  {flag}")

    # ── Resource overhead ─────────────────────────────────────────────────────
    print("\n── Resource Overhead ────────────────────────────────────────────")
    res_rows = []
    for s in STRATEGIES:
        r = resource_overhead(s)
        res_rows.append({
            "Strategy":         STRATEGY_LABELS[s],
            "Base Pods":        r["base_pods"],
            "Peak Pods":        r["peak_pods"],
            "Additional Pods":  r["idle_pods"],
            "Overhead %":       r["overhead_pct"],
            "Remarks":          r["note"],
        })
    res_df = pd.DataFrame(res_rows)
    print(res_df.to_string(index=False))
    res_df.to_csv(out_dir / "table_resource_overhead.csv", index=False)

    # ── Plots ──────────────────────────────────────────────────────────────────
    print("\n── Generating plots ──────────────────────────────────────────────")
    if any(len(v) for v in DT_np.values()):
        boxplot(DT_np, "Downtime by Strategy", "Downtime (ms)",
                str(out_dir / "boxplot_downtime.png"), log_scale=True)
    if any(len(v) for v in TR_np.values()):
        boxplot(TR_np, "Timeout Rate by Strategy", "Timeout rate (%)",
                str(out_dir / "boxplot_timeout_rate.png"))
    if any(len(v) for v in HER_np.values()):
        boxplot(HER_np, "HTTP Error Rate by Strategy", "HTTP error rate (%)",
                str(out_dir / "boxplot_http_error_rate.png"))
    if any(len(v) for v in SW_np.values()):
        boxplot(SW_np, "Switch / Rollout Duration", "Duration (ms)",
                str(out_dir / "boxplot_switch_duration.png"))
    cdf_plot(raw_dir, str(out_dir / "cdf_p99_latency.png"))
    resource_plot(str(out_dir / "resource_overhead.png"))

    # ── Summary JSON ──────────────────────────────────────────────────────────
    out = {}
    for s in STRATEGIES:
        dt_arr  = DT_np.get(s,  np.array([]))
        p99_arr = P99_np.get(s, np.array([]))
        sw_arr  = SW_np.get(s,  np.array([]))
        rb_arr  = RB_np.get(s,  np.array([]))
        tr_arr  = TR_np.get(s,  np.array([]))
        her_arr = HER_np.get(s, np.array([]))
        if not len(dt_arr):
            continue
        ci = bootstrap_ci(dt_arr)
        out[s] = {
            "n_runs":                  len(dt_arr),
            "downtime_mean_ms":        round(float(np.mean(dt_arr)), 3),
            "downtime_sd_ms":          round(float(np.std(dt_arr, ddof=1)), 3)
                                       if len(dt_arr) > 1 else 0.0,
            "downtime_median_ms":      round(float(np.median(dt_arr)), 3),
            "downtime_iqr_ms":         [round(float(np.percentile(dt_arr,25)),3),
                                        round(float(np.percentile(dt_arr,75)),3)],
            "downtime_95ci":           list(ci),
            "timeout_rate_mean_pct":   round(float(np.mean(tr_arr)), 4)
                                       if len(tr_arr) else 0.0,
            "http_error_rate_mean_pct":round(float(np.mean(her_arr)), 4)
                                       if len(her_arr) else 0.0,
            "p99_latency_global_ms":   p99_arr[0] if len(p99_arr) else float("nan"),
            "p99_latency_source":      "Mathematically Merged HdrHistograms",
            "switch_duration_mean_ms": round(float(np.mean(sw_arr)), 1)
                                       if len(sw_arr) else float("nan"),
            "rollback_duration_mean_ms":round(float(np.mean(rb_arr)), 1)
                                       if len(rb_arr) else float("nan"),
            "resource":                resource_overhead(s),
        }

    with open(out_dir / "summary.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  Summary JSON: {out_dir/'summary.json'}")

    print("\n" + "="*60 + "\n  Analysis complete.\n" + "="*60)


if __name__ == "__main__":
    main()
