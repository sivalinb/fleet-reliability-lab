# Principal observability: system design and code review

These original exercises evaluate engineering judgment: trustworthy signals, practical scale, controlled actions and understandable operations. They do not reproduce an employer's interview questions or internal implementation.

Use the [research map](research-map.md) for source-grounded context. The exercises and answer keys below are an independent reference design.

## 1. A 45-minute system-design exercise

**Prompt:** Design observability and safe repair for a multi-site, multi-tenant GPU fleet. The fleet runs distributed training and latency-sensitive inference. It must grow without requiring a person to interpret every symptom manually.

### Minutes 0-5: clarify requirements and authority

Ask for site/host/GPU count, workload classes, growth, signal volume, retention, data-residency requirements, tenant boundaries and failure tolerance. Establish who owns inventory, scheduler decisions, diagnostic procedures and disruptive actions.

Define a concrete customer outcome. Examples are a successful response within a latency bound, a job making expected progress, or qualified capacity available within a reservation. Do not choose arbitrary contractual SLO targets without an operating baseline.

**Probe:** What should happen when the observability backend is unavailable? A strong answer separates loss of central visibility from loss of local service and avoids turning uncertainty into broad disruptive action.

### Minutes 5-12: establish data and control contracts

Describe collection, ingestion, storage/query, investigation and the separate action path. Define stable asset IDs, tenant identity, allocation history, timestamps, schema versions and evidence provenance.

Explain the difference between observed health, lifecycle eligibility and workload progress. Sketch an incident that moves from a slow job to a fabric endpoint and a repair procedure.

**Probe:** Can a hostname identify the asset for its whole lifetime? Usually not; the design should handle replacement, renaming and historical ownership explicitly.

### Minutes 12-20: size from measurements

Hypothetical assumptions:

```text
100,000 GPUs
200 realized active series per GPU
15-second sampling interval

20,000,000 active series
~1,333,333 samples/second
```

This excludes host, fabric and workload telemetry, replication and churn. Estimate bytes only after measuring label sizes, compression and actual backend overhead. Model query mix and tenant skew separately from ingestion. Histogram buckets and label combinations multiply realized series.

**Probe:** Why is counting metric names insufficient? It ignores dimensions, target heterogeneity, histograms and churn. A good sizing model is validated against real per-target distributions.

### Minutes 20-28: define failure semantics

Specify bounded queues, persistence, backpressure, retry and recovery. Name the boundary between accepted, persisted, exported, ingested and query-visible data. Discuss site/WAN loss, backend outage, disk exhaustion, duplicates, late events and clock skew.

Protect necessary evidence during an outage. Expose freshness and expected-source coverage; distinguish no data from a valid zero. Use a known end-to-end canary instead of relying only on process health.

**Probe:** Can persistent queues still lose data? Yes. Their guarantees depend on supported configuration, storage durability, exhaustion, outage length and recovery behavior. State and test the remaining loss conditions.

### Minutes 28-36: design the safe action loop

Start or update a deduplicated incident. Establish the affected allocation. Obtain the permitted workload-protection decision. Choose a versioned procedure. Persist action intent and external-effect identity. Wait durably for human work where necessary. Revalidate before restoring eligibility.

Use tenant-scoped authorization, conditional state versions and idempotent or reconciled effects. Bound retries and concurrent disruption. Retain uncertain outcomes for reconciliation rather than classifying a timeout as an action failure.

**Probe:** A worker crashes after creating a ticket but before recording success. How do you prevent another ticket? Use a stable external idempotency key where supported, or reconcile the existing external operation before retrying.

### Minutes 36-41: control growth and operating cost

Choose practical partition boundaries, ingestion/query quotas, aggregation and retention by purpose. Account for active series, churn, bytes, query scans, tenant skew and on-call burden. Add infrastructure when it solves a measured requirement, not because it appears in a reference stack.

**Probe:** Would you add Kafka? Explain the replay, decoupling or outage requirement first. Compare it with a simpler bounded collector queue and include the cost of running another service.

### Minutes 41-45: close the design

Walk through one incident. State invariants, rollout stages, verification tests, operational ownership and the first measurement that could change your decision. Make the scope of simulation and real hardware evidence explicit.

**Probe:** What changes at ten times the fleet? Target discovery, query fan-out, incident bursts, inventory joins and technician throughput can scale differently. Identify a likely bottleneck and one focused experiment.

### A useful review rubric

| Dimension | Evidence of a strong answer |
|---|---|
| Correctness | Units, timing, population, unknown state and schema changes are explicit. |
| Scale | Realized series, churn, skew and query work are measured separately. |
| Failure behavior | Loss, duplication, retries and recovery boundaries are stated. |
| Safety | Authorization, workload protection, concurrency and verification gates exist. |
| Economics | Cost is tied to useful decisions and output, with explicit assumptions. |
| Leadership | Ownership, rollout, adoption and the simplest viable operating model are clear. |

## 2. Code review: telemetry correctness and cardinality

### Buggy implementation

Assume the Python Prometheus client and a streaming inference response:

```python
LATENCY = Histogram("ttft", "first token", ["request_id", "model"])

def observe(req, output):
    started = time.time()
    for token in output:
        if token:
            LATENCY.labels(req.id, req.model).observe(
                (time.time() - started) * 1000
            )
    return "ok"

# Grafana panel, labeled "fleet p99 TTFT (ms)":
# avg(histogram_quantile(0.99, rate(ttft_bucket[5m])))

def healthy(query_result):
    try:
        return float(query_result[0]["value"][1]) < 500
    except Exception:
        return True
```

### Prioritized review comments

**[P1] Record one first-token event at a defined boundary.** A two-token stream records two values, so the distribution includes later-token delay. Timing starts after `output` was supplied and may omit earlier request work. Define admission/client boundaries and use a monotonic elapsed clock. Do not silently change the meaning of an existing metric.

**[P1] Remove unbounded request identity from ordinary metric labels.** A unique request ID produces a new histogram family per request. Each family has bucket, sum and count series. Model values also need a bounded contract. Put request-level identity in traces or appropriate structured events.

**[P1] Fix units and bucket semantics together.** The metric records milliseconds into default histogram buckets typically used for seconds, while its name gives no unit. Changing the dashboard label cannot repair already-recorded bucket values. Define seconds in the metric name and producer, and choose explicit workload-appropriate buckets.

**[P1] Aggregate distributions before calculating a fleet quantile.** An average of per-series p99 values is not a request-weighted fleet p99. Aggregate compatible bucket counters over the desired dimensions, then apply `histogram_quantile`.

**[P1] Preserve missing or invalid evidence as unknown.** An empty response, parse failure, query error or non-finite value must not return healthy. Keep execution error, missing population, stale data and valid measurements distinguishable.

### Corrected pattern

```python
TTFT = Histogram(
    "inference_ttft_seconds",
    "server admission to first emitted token",
    ["model_family"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

def stream(req, backend, trace):
    family = allowed_model_family(req.model)  # bounded schema
    started = time.perf_counter()
    first = True
    for token in backend.stream(req):
        if token and first:
            elapsed = time.perf_counter() - started
            TTFT.labels(family).observe(elapsed)
            trace.set_attribute("ttft_seconds", elapsed)
            first = False
        yield token
    # Instrument empty, error and cancelled outcomes separately.
```

For a per-model, request-weighted p99 over compatible classic histogram buckets:

```promql
histogram_quantile(
  0.99,
  sum by (le, model_family) (
    rate(inference_ttft_seconds_bucket[5m])
  )
)
```

The query does not establish source coverage, freshness, enough observations, successful completion or client-observed latency. Display count/rate, errors and freshness alongside it. Define whether a tenant boundary requires separate storage or authorization rather than indiscriminately adding labels.

**Meaningful tests:** a two-token stream increments histogram count once; an empty stream has no successful TTFT observation; failures/cancellations remain visible; model dimensions are bounded; non-finite/empty/stale/query-error responses remain unknown; unequal request volumes aggregate correctly; and schema migration does not mix incompatible bucket layouts or units.

**Tradeoffs to discuss:** classic versus native histograms in the actual backend, desired tail accuracy, query cost, bucket migration and how to preserve per-request detail without exploding series.

## 3. Code review: idempotent repair, concurrency and tenancy

### Buggy implementation

```python
def complete_repair(request, db, device):
    node = db.get_node(request.json["node_id"])
    if request.headers["X-Tenant"] != node.tenant:
        raise Forbidden()
    if node.state != "repairing":
        return {"status": "already done"}

    device.reboot(node.address)
    db.execute("UPDATE nodes SET state='ready' WHERE id=?", [node.id])
    db.insert_history(node.id, "repair complete")
    return {"status": "ready"}
```

Assume the HTTP client retries after a timeout, two workers can receive the same completion, and another incident can acquire the node.

### Prioritized review comments

**[P0] Authorize from verified identity, not a caller-controlled tenant header.** A guessed node ID and forged header can authorize another tenant's device action. Scope the lookup and operation to the authenticated principal, including asset-level policy where applicable. Protect reads and evidence links as well as writes.

**[P1] Make the state transition conditional and incident-specific.** The read/act/write sequence races. Two workers can reboot, or an old completion can overwrite a newer repair. Require the expected incident and state version, and enforce the condition in the database write.

**[P1] Persist action intent and reconcile uncertain effects.** A timeout does not prove that a reboot failed. A process can crash after an external effect and before recording it. Use a stable idempotency key or reconcile external state before retrying. An ordinary database transaction cannot encompass a physical device effect.

**[P1] Revalidate before restoring eligibility.** A reboot is not proof of recovery. Protect the workload before disruption, record required checks and keep the asset withheld on failed, skipped-required, errored or incomplete verification.

**[P1] Commit evidence and transition coherently.** State updates and history insertion can diverge. Store durable command/event/intent records transactionally, then let a bounded worker perform and reconcile external work.

### Corrected pattern

This is pseudocode for the contract, not a complete distributed-lock or hardware API implementation:

```python
def accept_completion(principal, cmd, db):
    require_role(principal, "repair:complete")
    with db.transaction():
        # Unique(tenant, command_key). Payload includes scoped asset,
        # incident, operation and expected version.
        prior = db.find_command(principal.tenant, cmd.key)
        if prior:
            return validate_authorization_payload_and_replay(
                principal, prior, cmd
            )

        node = db.get_node_for_tenant(cmd.node, principal.tenant)
        require_asset_permission(principal, node)
        require(node.incident_id == cmd.incident)
        require(node.version == cmd.expected_version)
        require(node.state == "awaiting_physical_work")

        db.record_command_and_verification_intent(node, cmd)
        db.transition(node, "verifying", expected=node.version)
    return {"status": "verification queued"}

def verifier(intent):
    # Claim and reconcile with lease/fencing; enforce workload policy.
    evidence = run_required_checks(intent.asset)
    # Transactionally recheck tenant, incident, version and claim.
    # Persist evidence and event. Advance only when policy passes.
    commit_result_conditionally(intent, evidence)
```

Replay a prior authorized command before requiring its original state/version, because the first attempt may already have advanced them. The stored payload must match; key reuse with a different asset or action is an error. A database uniqueness constraint and a defined conflict/replay path are still required when simultaneous requests both miss the initial lookup.

A lease alone does not prevent an expired worker from touching hardware. Where possible, use API idempotency, fencing, preconditions or reconciliation. Do not keep a database transaction open around a remote action. Record uncertain outcomes explicitly and preserve a safe operator path.

**Meaningful tests:** duplicate same payload; same key with changed payload; simultaneous workers; crash after intent; crash after external effect; stale completion for another incident; wrong tenant; permission revoked before replay; failed verification; missing required checks; expired worker; and history/state atomicity. Assert one valid logical transition and complete evidence, not just an HTTP success response.

**Tradeoffs to discuss:** synchronous versus queued validation, how long to retain idempotency records, procedure version changes, operator overrides, eventual consistency with external ticketing, and the smallest safe unit of parallel repair.

## 4. Review communication matters

Lead with a concrete trigger and consequence, then propose the smallest defensible fix. Distinguish a release-blocking authorization or correctness defect from a follow-up design improvement. Ask for missing assumptions before declaring a bug that depends on them.

After identifying defects, describe the observable contract the corrected design must satisfy. Prefer tests that demonstrate a failure boundary over tests that merely mirror an implementation. Explain who will operate the result, how it rolls out and what evidence would justify more automation.

Primary technical references: [Prometheus histogram guidance](https://prometheus.io/docs/practices/histograms/), [OpenTelemetry Collector resiliency](https://opentelemetry.io/docs/collector/resiliency/), [Temporal activity idempotency](https://docs.temporal.io/activity-definition), [Loki label cardinality](https://grafana.com/docs/loki/latest/get-started/labels/cardinality/).
