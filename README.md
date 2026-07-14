# Achieving True Zero-Downtime Kubernetes Deployments: A Statistically Rigorous Comparison of Blue-Green, Canary, Rolling, and Recreate Strategies

## Tech Stack

![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?style=flat&logo=kubernetes&logoColor=white)
![Ansible](https://img.shields.io/badge/Ansible-EE0000?style=flat&logo=ansible&logoColor=white)
![Nginx](https://img.shields.io/badge/Nginx-009639?style=flat&logo=nginx&logoColor=white)
![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Pandas](https://img.shields.io/badge/Pandas-150458?style=flat&logo=pandas&logoColor=white)
![SciPy](https://img.shields.io/badge/SciPy-8CAAE6?style=flat&logo=scipy&logoColor=white)

A fully automated ecosystem that benchmarks four Kubernetes deployment strategies —
**Blue-Green**, **Canary**, **Rolling Update**, and **Recreate** — using containerised Flask
microservices, Ansible orchestration, an Nginx traffic-switching layer, a precision `wrk2`
load-testing harness, and a Pandas/SciPy statistical analysis pipeline.

The central question: *can a proxy-based Blue-Green switchover reduce client-visible
downtime relative to Kubernetes' native rollout strategies, under a controlled, repeatable,
statistically analyzed load test — and where does that comparison start to break down?*

---

## Why This Exists

Kubernetes' native rolling updates are often assumed to be "zero downtime" once
`maxUnavailable: 0` is set. In practice, a structural propagation delay between a pod
beginning termination and `kube-proxy` updating routing rules cluster-wide means requests
can still land on a dying pod. This project:

- Builds an application-layer proxy switchover (Nginx, atomic `nginx -s reload`) that
  bypasses that propagation gap entirely.
- Measures **actual downtime** (largest contiguous window of non-2xx responses in the raw
  per-request stream), not just deployment/rollout duration.
- Runs Rolling Update and a deliberately-broken Recreate strategy as native-Kubernetes
  and negative-control baselines respectively.
- Applies rigorous statistics (bootstrap confidence intervals, non-parametric significance
  tests, effect sizes) across N=10 randomized runs per strategy, instead of eyeballing a
  handful of runs.

---

## Repository Structure

```
zero-downtime-k8s/
├── microservices/          # 5 containerised Flask services (application layer)
│   ├── api-gateway/
│   ├── catalog-service/
│   ├── price-service/
│   ├── inventory-service/
│   └── shipping-service/
├── ansible/                 # orchestration engine
│   ├── group_vars/all.yml   # replica_count, gate thresholds, Docker Hub user
│   ├── inventory.ini        # local execution inventory (kubectl → Minikube)
│   ├── playbooks/           # 00-09: cluster bootstrap → blue/green/canary/rolling/recreate
│   └── roles/               # 9 reusable roles (cluster_setup, switch_traffic, etc.)
├── wrk2/                    # load generation + precision measurement harness
│   ├── downtime-test.lua    # microsecond-precision response logger
│   ├── run-strategy-test.sh # unified test runner
│   ├── run-baseline-test.sh
│   ├── run-downtime-test.sh
│   ├── run-rollback-test.sh
│   └── parse_results.py
├── experiment/              # statistical rigor layer
│   ├── run-experiment.sh    # N-run orchestrator, randomized schedule across 4 strategies
│   ├── measure_rollbacks.sh # rollback/MTTR timing harness (10 runs × 4 strategies)
│   ├── raw/                 # per-run CSVs, switch logs, rollback-duration files (included)
│   └── logs/                # run schedule + per-run logs
├── analysis/
│   ├── analyze.py           # Pandas/SciPy pipeline: bootstrap CIs, Kruskal-Wallis,
│   │                        # Mann-Whitney U (Holm-Bonferroni), Cliff's Delta, plots
│   └── output/              # generated tables (table_*.csv) and plots (boxplot_*.png, cdf_*.png)
├── scripts/                 # environment bootstrap utilities
│   ├── 01-install-prereqs.sh
│   ├── 02-build-and-push-images.sh
│   ├── 03-load-images-into-minikube.sh
│   └── 04-install-analysis-deps.sh
└── rollback_measure.log
```

---

## How It Works

### 1. Application Layer
Five Python Flask services (`python:3.11-slim`, Gunicorn, 2 workers), each pushed to Docker
Hub as `<user>/<service>:v1` / `:v2`. Every pod echoes `APP_VERSION` and `DEPLOY_ENV` in
response headers/body, so `curl /health` alone proves which environment served a request.

| Service | Role | Key Endpoints |
|---|---|---|
| api-gateway | Entry point, fans out to the other 4 | `/`, `/health`, `/catalog`, `/price/<id>`, `/inventory/<id>`, `/shipping` |
| catalog-service | Product catalog | `/catalog`, `/health` |
| price-service | Product pricing | `/price/<id>`, `/health` |
| inventory-service | Stock status | `/inventory/<id>`, `/health` |
| shipping-service | Shipping estimate | `/shipping`, `/health` |

### 2. Cluster Topology
A 3-node Minikube cluster (`minikube start --nodes=3 --driver=docker --cpus=2
--memory=2200`). Nodes are labelled `env=blue` / `env=green` for visibility only — no
`nodeSelector` is enforced, so pods are freely scheduled across all nodes, keeping the
comparison against Rolling Update and Recreate fair.

### 3. Deployment Strategies

- **Blue-Green** — Ansible's `switch_traffic` role pre-flight-checks the target
  environment, writes a new Nginx config directly into each router pod's writable
  `emptyDir` via `kubectl exec`, validates with `nginx -t`, then performs an atomic
  `nginx -s reload`. Old worker processes drain in-flight connections while new workers
  pick up the new upstream — the listening socket never closes.
- **Canary** — Reuses the *same* Nginx-injection mechanism with `split_clients` for
  deterministic percentage-based routing: 90/10 → 50/50 → 0/100, with 10-second soaks
  between stages, using the identical atomic-reload primitive as Blue-Green at every stage.
  Because the underlying switch mechanism is shared, Canary's downtime behavior is **not an
  independent result** — it is the same atomic-reload guarantee confirmed under a staged
  traffic split rather than a single cutover, and is reported here as such.
- **Rolling Update** — Native Kubernetes `Deployment` controller (`maxSurge: 1,
  maxUnavailable: 0`). Included as the "native Kubernetes best practice" baseline.
- **Recreate** — Native Kubernetes `Recreate` strategy (kills all old pods before
  starting new ones). Included as a deliberate negative control to validate the
  measurement harness.

### 4. Equal-Footing Methodology
Blue-Green and Canary maintain a standby namespace that would otherwise contend for CPU
with the active namespace on a resource-constrained test VM. Before each run, the idle
namespace is scaled to 0 replicas and restored ~20s before the switch event — eliminating
client-side timeout artifacts so that every measured millisecond of downtime reflects the
deployment strategy's own routing logic, not cluster exhaustion.

### 5. Measurement
A custom Lua script (`downtime-test.lua`) loaded into `wrk2` logs the exact microsecond
timestamp and HTTP status of every individual response. Downtime is computed as the
largest contiguous window of non-2xx responses in that raw stream — robust to throughput
fluctuations, since it doesn't rely on aggregated request-rate assumptions.

### 6. Statistical Analysis
`analysis/analyze.py` ingests all raw CSVs and computes bootstrap 95% confidence
intervals, a Kruskal-Wallis omnibus test across all four strategies, pairwise Mann-Whitney
U tests against the Blue-Green baseline (Holm-Bonferroni corrected), and Cliff's Delta
effect sizes, then generates all plots and tables in `analysis/output/`.

---

## Scope and Limitations

This is a single-cluster, local measurement study, and its conclusions are reported strictly
within that scope:

- **Cluster scale.** All results come from one 3-node Minikube deployment on a single VM,
  not a multi-node production cloud cluster. Absolute timings (and possibly relative
  ordering under different network topologies) may not transfer directly to a
  multi-availability-zone or multi-region deployment. Validating on a production-scale
  cluster under realistic node counts and network latency is planned future work, not a
  claim made here.
- **Load density.** Load is generated with a deliberately light `wrk` configuration
  (2 threads, 4 connections) so client-side CPU never becomes the bottleneck and masks true
  cluster-side downtime. This is a conservative choice for detecting downtime, not a
  claim about behavior at production-level request rates: a lighter request stream can, in
  principle, under-sample a very short outage window. The fact that the harness still
  reliably detects Rolling Update's small (~10 ms) real downtime and Recreate's large
  outage indicates the instrument is sensitive enough for the effect sizes observed here;
  higher-RPS testing would only be expected to strengthen (not weaken) the zero-downtime
  finding for Blue-Green/Canary, but that has not been tested and isn't claimed.
---

## Reproducing the Experiment

Run from a fresh Ubuntu machine, from the project root:

```bash
# 1. Install OS prerequisites (Docker, Minikube, kubectl, Ansible, wrk2)
./scripts/01-install-prereqs.sh

# 2. Build and push v1/v2 images for all 5 microservices to Docker Hub
./scripts/02-build-and-push-images.sh

# 3. Bootstrap the Minikube cluster, namespaces, and baseline deployments
cd ansible
ansible-playbook -i inventory.ini playbooks/01-setup-cluster.yml
cd ..

# 4. Pre-load images into Minikube's internal Docker daemon (avoids pull delays/rate limits)
./scripts/03-load-images-into-minikube.sh

# 5. Install analysis dependencies (numpy, scipy, matplotlib, pandas) inside a venv
./scripts/04-install-analysis-deps.sh

# 6. Run the randomized N=10-per-strategy experiment suite
#    args: 10 runs, 5 req/sec (legacy arg), 120s duration, switch at t=40s
./experiment/run-experiment.sh 10 5 120 40

# 7. Measure rollback / MTTR timings (10 runs per strategy)
./experiment/measure_rollbacks.sh 10

# 8. Generate statistical tables and plots
python3 analysis/analyze.py
```

Outputs land in `analysis/output/` as `table_*.csv`, `boxplot_*.png`, `cdf_*.png`, and
`summary.json`. Raw per-run data (response CSVs, switch logs, rollback-duration files) for
all 40 (+40 rollback) runs is included in this repository under `experiment/raw/`.

---


## Author

**Rao Shruti Mohankumar**
