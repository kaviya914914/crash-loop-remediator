# crash-loop-remediator

A self-healing Kubernetes operator that detects pods stuck in `CrashLoopBackOff`, diagnoses why they crashed, and automatically remediates them — reducing manual on-call toil and MTTR (mean time to recovery).

Built from scratch with Python and [kopf](https://kopf.readthedocs.io/), running as a proper in-cluster workload with least-privilege RBAC — not a script on someone's laptop.

---

## The problem

When a pod crash-loops in production, the usual flow is: an alert fires, an engineer gets paged, they `kubectl describe` and `kubectl logs` to find the cause, then manually restart or roll back. For common, recoverable failure patterns, this is repetitive toil that a well-scoped automation can safely handle — freeing engineers for problems that actually need a human.

This operator automates the first line of response: **detect → diagnose → remediate → report.**

## Architecture

```
                 ┌─────────────────────────────────────────┐
                 │         crash-loop-remediator            │
                 │         (kopf operator, in-cluster)      │
                 └─────────────────────────────────────────┘
                                    │
        watches pods.status.containerStatuses (namespace-scoped)
                                    │
                                    ▼
                    ┌───────────────────────────┐
                    │  CrashLoopBackOff detected?│
                    └───────────────────────────┘
                                    │ yes
                                    ▼
                 ┌──────────────────────────────────┐
                 │  Diagnose: exit code, reason,     │
                 │  last 10 lines of container logs  │
                 └──────────────────────────────────┘
                                    │
                                    ▼
                 ┌──────────────────────────────────┐
                 │  Remediate: delete the pod        │
                 │  (Deployment recreates it fresh)  │
                 │  — 60s cooldown per pod            │
                 └──────────────────────────────────┘
                                    │
                                    ▼
                 ┌──────────────────────────────────┐
                 │  Report: structured JSON event    │
                 │  (crash_loop_detected,             │
                 │   remediation_attempted)          │
                 └──────────────────────────────────┘
```

## Tech stack

- **Python 3.11** + [**kopf**](https://kopf.readthedocs.io/) — Kubernetes Operator Pythonic Framework, handles the watch/reconcile loop
- **kubernetes** (official Python client) — reads pod logs, deletes pods
- **Docker** — containerizes the operator
- **Kubernetes RBAC** — ServiceAccount, Role, RoleBinding, ClusterRole, ClusterRoleBinding
- **Minikube** — local cluster for development and demo

## How it works

**1. Detect.** The operator watches `pods.status.containerStatuses` in its namespace. When a container's waiting reason is `CrashLoopBackOff`, it's flagged.

**2. Diagnose.** It pulls the container's last exit code and termination reason from `lastState.terminated`, and fetches the last 10 lines of the crashed container's logs via the Kubernetes API (`previous=True` — the log from the run *before* the current restart).

**3. Remediate.** It deletes the crashing pod. Since the target is a Deployment (not a bare pod), the ReplicaSet controller immediately creates a fresh replacement — this "delete and let the controller recreate" pattern is a safe, generic fix for a wide range of transient failures (bad node, resource spike, stale config).

**4. Report.** Every detection and remediation emits a structured JSON log line (`crash_loop_detected`, `remediation_attempted`, `remediation_skipped`) — designed to feed into a log aggregator like Loki or ELK for dashboards and alerting.

## Key design decisions

- **Least-privilege RBAC.** The operator's write permissions (`delete`, `patch`, `update` on pods) are scoped to a single namespace via a `Role`. It only holds a `ClusterRole` for read-only, cluster-wide visibility into two resources kopf needs at startup (`customresourcedefinitions`, `namespaces`) — no cluster-admin, no broader write access than the job requires.
- **Per-pod remediation cooldown.** A 60-second cooldown prevents the operator from deleting the same pod repeatedly within one reconcile burst (Kubernetes can emit several status updates in quick succession for a single event).
- **Deployment over bare Pod.** The demo target (`crashy-app`) is a Deployment specifically so "delete the pod" is a meaningful remediation — a bare pod with no controller would just disappear.
- **Structured logging over free text.** JSON events (not prose log lines) make the operator's actions queryable and dashboard-ready from day one, rather than needing a rewrite later.

## Demo

Real output from a live run against a deliberately crash-looping test pod:

```json
{"timestamp": "2026-09-27T19:34:24.516688+00:00", "event": "crash_loop_detected", "namespace": "default", "pod": "crashy-app-6c65798c76-ntqpl", "container": "crashy-app", "restarts": 1, "exit_code": 1, "reason": "Error", "logs": "Starting up...\nCrashing now!"}
{"timestamp": "2026-09-27T19:34:24.532005+00:00", "event": "remediation_attempted", "namespace": "default", "pod": "crashy-app-6c65798c76-ntqpl", "success": true, "message": "Deleted pod; controller will recreate it."}
```

Corresponding pod lifecycle, watched live via `kubectl get pods -w`:

```
crashy-app-6c65798c76-ntqpl   0/1   CrashLoopBackOff   1 (2s ago)   17s
crashy-app-6c65798c76-ntqpl   0/1   Terminating        1 (2s ago)   17s
crashy-app-6c65798c76-p9n26   0/1   Pending            0            0s
crashy-app-6c65798c76-p9n26   1/1   Running            0            4s
```

Detection to remediation to a fresh, running replacement pod — all within a couple of seconds, with zero human involvement.

## Running it locally

**Prerequisites:** Minikube, kubectl, Docker Desktop, Python 3.11+

```bash
# 1. Start the cluster
minikube start

# 2. Point Docker at Minikube's daemon
@FOR /f "tokens=*" %i IN ('minikube -p minikube docker-env --shell cmd') DO @%i   # Windows
# or: eval $(minikube docker-env)                                                 # macOS/Linux

# 3. Build the operator image
docker build -t crash-loop-remediator:v1 .

# 4. Apply RBAC
kubectl apply -f rbac.yaml

# 5. Deploy the operator
kubectl apply -f operator-deployment.yaml

# 6. Deploy a test target that crash-loops on purpose
kubectl apply -f broken-deployment.yaml

# 7. Watch it heal itself
kubectl logs -f deployment/crash-loop-remediator
kubectl get pods -w
```

## Future improvements

- **Slack/Teams alerting** — post remediation events to a channel in real time
- **Smarter diagnosis** — branch remediation strategy by failure type (`OOMKilled` → suggest resource limit increase; bad image → alert instead of restart-looping forever)
- **Metrics endpoint** — expose Prometheus metrics (`remediations_total`, `detection_latency_seconds`) for dashboarding alongside the [observability-platform](#) project
- **Rollback support** — for Deployments with a bad rollout, roll back to the previous revision instead of just restarting the pod
- **Unit + integration tests** — envtest-based tests for the reconcile logic

---

*Built as a hands-on SRE/DevOps portfolio project — from first `kubectl apply` to a working in-cluster operator with scoped RBAC and structured observability.*
