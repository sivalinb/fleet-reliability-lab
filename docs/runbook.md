# Operate and investigate the lab

Start the core or full native profile as described in the [README](../README.md). All examples below use intentionally public loopback demo credentials and fictional assets.

## A complete repair through the API

```sh
curl -s http://127.0.0.1:8790/api/scenarios \
  -H 'Authorization: Bearer alpha-operator-demo' \
  -H 'Content-Type: application/json' -d '{"scenario":"optic"}'
curl -s http://127.0.0.1:8790/api/fleet \
  -H 'Authorization: Bearer alpha-viewer-demo'
```

Read the returned incident ID and current `version`. To advance exactly one stage:

```sh
curl -s http://127.0.0.1:8790/api/incidents/INCIDENT_ID/advance \
  -H 'Authorization: Bearer alpha-operator-demo' \
  -H 'Content-Type: application/json' -d '{"expected_version":0}'
```

Refresh the current version after every response. A stale version returns 409 and does no further action. At `wait_repair`, complete the returned ticket through `POST /api/tickets/TICKET_ID/complete` with `{"success":true}`. Then advance the wait and validation stages. Completing the ticket alone leaves the incident unresolved. The UI exposes these same operations.

Freshness expires after 30 seconds. Use `POST /api/assets/ASSET_ID/observe` with `{}` to collect another explicitly simulated sample. This does not claim to measure hardware. Use `fresh:false` to demonstrate loss. Workload release uses `POST /api/workloads/JOB_ID/release` with `{}` and reruns the idle baseline. Commissioning can rerun a failed lab qualification once its injected test is removed. It cannot bypass an active repair.

## Investigate across the actual native services

In Grafana at <http://127.0.0.1:33300>, open **Fleet Reliability · synthetic hardware / real operations**. The nine panels query Mimir and Loki. Open Tempo in Explore for the incident's trace ID from the API event record.

| Question | Query / evidence | Decision |
|---|---|---|
| Is the observation trustworthy now? | `fleet_telemetry_age_seconds`; expected inventory vs observed sources | Unknown/stale blocks repair; investigate telemetry path |
| Is the fleet withholding capacity? | `sum by (tenant,state) (fleet_asset_state == 1)` | Identify scope, policy reason and maintenance context |
| Did fabric behavior change? | `rate(lab_fabric_errors_total[30s])` | Correlate with workload phase and topology; counters alone do not prove causality |
| Which procedure and task? | Loki `{service_name="fleet-reliability-lab"} | incident_id="INCIDENT_ID"` | Inspect selected SOP, location, ticket and validation history |
| Did control execution survive interruption? | Temporal workflow history and engine effect ledger | Reconcile committed stage; retry with stable effect identity |
| Did useful work recover? | Workload `step_ms`, `progress`, qualification records and final incident event | Validate the outcome before returning capacity |
| Did the telemetry plane fail? | `fleet_outbox_pending_events`, Collector health/logs, Loki/Tempo queries | Keep durable history; replay; verify backend retrieval |

Loki's `incident_id` is structured metadata, not a stream label. Metrics and backend tenant settings are local internal lab settings; do not treat the Grafana instance as a customer tenant boundary.

## Failure handling

- **Collector unavailable:** committed events remain pending in SQLite. The export loop retries. Verify queued count falls and records are actually queryable, not just that an HTTP accept occurred.
- **Backend unavailable after Collector acceptance:** Collector's persistent queues retry; app outbox may already be acknowledged. Permanent backend loss or a failed queue disk is beyond the lab's delivery guarantee.
- **Worker dies after a committed effect:** restart the worker against the same database and Temporal server. The committed version wins. Do not invent a new incident ID to retry the same action.
- **Technician reports failure:** retain isolation and escalate. A replacement task must not falsely certify capacity.
- **Validation fails:** stay quarantined, retain every failed check and bound the retries. Review hypothesis, placement/topology and baseline before expanding repair scope.
- **Cross-tenant or unauthorized request:** return 401/403/404 according to scope without exposing another tenant's records. Audit identity at a production gateway; demo credentials are not deployable identity infrastructure.
- **No idle nodes:** finish the active repair, release its workload, and recheck the baseline. For repeated integration runs use a fresh `--runtime` directory and the same matching argument to the verifier.

## Physical qualification extension (not executed)

For real hardware, create an isolated staging pool and collect signed/versioned artifacts for asset identity, approved BIOS/BMC/firmware, topology, temperatures/power, CPU/memory, GPU diagnostics, storage, fabric and distributed workload performance. DCGM diagnostics and NCCL tests are candidate sources; validate required device/driver permissions and disruptive effects against vendor documentation. Correlate baselines by hardware SKU, firmware, topology and test version. A pass threshold without a comparable baseline can admit a slow node.

Store raw result, exit status, observed time, version, threshold, environment and provenance. Unsupported tests remain skipped. Restrict noisy/disruptive tests to an approved maintenance or qualification window; do not run an improvised burn-in against customer work. After physical repair, test the suspect path and the whole useful-work contract. This repository does not invoke privileged hardware commands.
