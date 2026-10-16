# Fleet Reliability Observatory

**Trace a fleet signal through qualification, workload impact, repair, verified recovery, and capacity economics.** An independent engineering lab by Siva Babu, informed by public GPU fleet operations research.

[Explore the animated operating story](https://sivalinb.github.io/labs/fleet-reliability/) · [Architecture and decisions](docs/architecture.md) · [Public research map](docs/research-map.md) · [Principal design and code review](docs/principal-interview.md)

![Original conceptual illustration of a GPU fleet reliability workshop](web/assets/fleet-observatory.webp)

The story uses original raster illustrations with moving robots, packets, scanners and repair overlays. Its six interactive scenarios replay **executions of the Python engine**. The live local API lets you operate the same qualification and repair path yourself.

## What is real, and what is simulated?

| Executed software | Explicitly synthetic / unmeasured |
|---|---|
| SQLite transactions, tenant authorization, lifecycle gates, compare-and-swap transitions, ticket deduplication and crash recovery | GPU/fabric/thermal/storage measurements, hardware inventory and workload step/TTFT values |
| Native Prometheus → Mimir, OTLP Collector → Loki/Tempo, Alertmanager notifications, provisioned Grafana | Customer workloads, DCIM/BMC/firmware changes, real Jira tickets and physical repair |
| Real Temporal development server and an actual worker stop/restart test | Production Temporal availability, GPU/RDMA benchmarks and production-scale load |
| Local CPU digest and temporary-filesystem probes with checksum validation | Physical GPU burn-in: always **skipped**, never marked passed |

This is an independent reference implementation. Nscale's public descriptions name technologies and operating responsibilities; they do not publish the internal architecture reproduced by this lab. All identifiers and data are fictional. The private interview guide and résumé are excluded.

## Run the lab

Python 3.9+; the core has **no third-party dependencies**:

```sh
git clone https://github.com/sivalinb/fleet-reliability-lab.git
cd fleet-reliability-lab
python3 -m fleetlab.server
```

Open <http://127.0.0.1:8790>. The public story defaults to a recorded replay. Choose **Connect local backend**, leave the loopback URL, and use `alpha-operator-demo` to run synthetic scenarios. Credentials stay in browser memory. Demo tokens are intentionally public and valid only for this local lab; configure `LAB_TOKENS` before any different environment. The server defaults to loopback, and the stack has no production authentication, TLS or HA.

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 scripts/record_replays.py
python3 -m fleetlab.probes
```

## Run the native observability and workflow stack

The native profile avoids requiring a container daemon. It downloads pinned official releases, checks their published SHA-256 values, and stores everything under ignored `work/`. macOS/Linux arm64/amd64 are supported by the downloader; **the recorded integration run is macOS arm64**. Other combinations are not claimed as tested.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-temporal.txt
python3 scripts/bootstrap_native.py
# Obtain Grafana OSS 12.1.1 for your OS from grafana.com/grafana/download/12.1.1
# Extract it locally, then pass the extracted directory:
.venv/bin/python scripts/stack.py --grafana-home /path/to/grafana-12.1.1
```

In a second terminal:

```sh
PYTHONPATH=. .venv/bin/python scripts/verify_stack.py
```

The verifier creates a synthetic optic incident, runs a real Temporal workflow to the physical-repair wait, observes a real Alertmanager notification, terminates its worker, completes the simulated technician task, starts another worker, and asserts one ticket plus successful revalidation. It also queries Mimir, Loki and Tempo, checks Grafana provisioning, exercises the durable delivery outbox against an unreachable endpoint and then the real Collector, and measures local CPU/filesystem probes. A fresh runtime database is recommended for repeated verification.

| Service | Loopback port |
|---|---:|
| Story/API | 8790 |
| Grafana (`labadmin` / `local-lab-only`) | 33300 |
| Temporal UI / server | 38233 / 37233 |
| Prometheus / Alertmanager | 39090 / 39093 |
| Mimir / Loki / Tempo HTTP | 39009 / 33100 / 33200 |
| Collector OTLP HTTP / health | 24318 / 23133 |

Ctrl-C stops the launcher's own children. It does not stop other projects or delete their data. All backend settings here are **single-process local development settings**; authentication is enforced at the lab API boundary, while the observability backends are internal loopback services with multitenancy disabled. This is not backend-level tenant isolation.

## Six inspectable operations

1. **Optical gray failure:** busy devices but slow workload → persistent observations → isolate → diagnose → precise physical ticket → requalification → resume.
2. **Burn-in gate:** failed simulated diagnostic withholds capacity; rerun passes the lab policy; unsupported physical tests remain skipped.
3. **Thermal throttling:** versioned allowlisted remote procedure; fresh evidence and validation gate the result.
4. **Telemetry loss:** health becomes unknown; no unsupported hardware diagnosis or automatic repair.
5. **Worker restart:** persistent stage and stable operation IDs survive an interrupted worker; no second ticket.
6. **Storage latency:** a non-GPU bottleneck explains useful-compute loss; validate the end-to-end path.

Qualification also runs on idle re-entry. Releasing a workload cannot bypass an active repair. Ticket completion records an action; validation establishes the result.

## Evidence and review

- [Recorded backend verification](evidence/stack-verification.json), [test verification](evidence/test-verification.json), and [download manifest](evidence/binary-manifest.json) report actual runs and provenance.
- [Architecture](docs/architecture.md) explains invariants, failure handling, identity, cardinality and a production scale design.
- [Operating runbook](docs/runbook.md) includes API examples, investigations and escalation boundaries.
- [Public source map](docs/research-map.md) and [79-article corpus inventory](docs/blog-corpus.md) distinguish published facts from independent engineering choices.
- [Principal review exercises](docs/principal-interview.md) include original flawed code, prioritized findings and stronger versions.
- [Illustration provenance](web/assets/ILLUSTRATION-PROVENANCE.md) documents generated artwork. Illustrations are concepts; screenshots show actual running software.

Capacity exposure unions overlapping incident windows inside each job lifetime, multiplies by allocated GPUs, and uses an explicit illustrative **$3/GPU-hour** assumption. It is neither Nscale pricing nor measured cash savings. No energy, contract, or billing model is inferred.
