"""Transactional lifecycle, diagnosis and verified repair, with no external deps.

All GPU and fabric signals are synthetic. State changes, SQLite transactions,
tenant authorization, qualification records and workflow recovery are real code.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

POLICY_VERSION = "repair-policy/v1"
GOLDEN = "lab-baseline/2026.10"
FRESHNESS_SECONDS = 30
KINDS = {"optic", "thermal", "storage", "gpu-memory"}
STATES = ("discovered", "qualifying", "available", "allocated", "quarantined", "repairing", "validating", "retired")
SOPS = {
    "optic": {"id": "optic-replace/v1", "path": "physical", "instruction": "Inspect both link endpoints; replace the simulated faulty optic; retain the old serial; run fabric and collective validation."},
    "thermal": {"id": "thermal-profile/v1", "path": "remote", "instruction": "Apply the allowlisted simulated cooling profile after isolation; validate temperature and workload progress."},
    "storage": {"id": "storage-replace/v1", "path": "physical", "instruction": "Replace the simulated degraded scratch device; validate storage latency and checkpoint performance."},
    "gpu-memory": {"id": "gpu-memory-replace/v1", "path": "physical", "instruction": "Escalate the simulated memory fault for component replacement; rerun device and workload validation."},
}

class LabError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status

class Engine:
    def __init__(self, db_path, clock=time.time):
        self.path = str(db_path)
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, site TEXT NOT NULL,
              rack TEXT NOT NULL, slot INTEGER NOT NULL, cluster TEXT NOT NULL,
              serial TEXT NOT NULL, gpu_count INTEGER NOT NULL,
              firmware TEXT NOT NULL, lifecycle TEXT NOT NULL,
              health TEXT NOT NULL, last_seen REAL NOT NULL,
              temperature REAL NOT NULL DEFAULT 45,
              utilization REAL NOT NULL DEFAULT 0,
              fabric_errors INTEGER NOT NULL DEFAULT 0,
              storage_latency_ms REAL NOT NULL DEFAULT 2,
              fault TEXT, streak INTEGER NOT NULL DEFAULT 0,
              allocated_at REAL, commissioned_at REAL);
            CREATE TABLE IF NOT EXISTS incidents (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(id),
              kind TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
              created_at REAL NOT NULL, isolated_at REAL, resolved_at REAL,
              attempts INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
              policy TEXT NOT NULL, last_error TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident
              ON incidents(tenant,asset_id) WHERE status NOT IN ('resolved','retired');
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY, incident_id TEXT NOT NULL UNIQUE REFERENCES incidents(id),
              tenant TEXT NOT NULL, location TEXT NOT NULL, instruction TEXT NOT NULL,
              status TEXT NOT NULL, success INTEGER, created_at REAL NOT NULL, completed_at REAL);
            CREATE TABLE IF NOT EXISTS actions (
              operation_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
              incident_id TEXT NOT NULL, stage TEXT NOT NULL,
              result TEXT NOT NULL, completed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
              asset_id TEXT, incident_id TEXT, kind TEXT NOT NULL,
              observed_at REAL NOT NULL, ingested_at REAL NOT NULL,
              data TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              sequence INTEGER PRIMARY KEY REFERENCES events(sequence),
              logs_sent INTEGER NOT NULL DEFAULT 0, traces_sent INTEGER NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS qualification (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, asset_id TEXT NOT NULL,
              suite TEXT NOT NULL, test TEXT NOT NULL, status TEXT NOT NULL,
              value REAL, unit TEXT, expected TEXT NOT NULL,
              provenance TEXT NOT NULL, observed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS workloads (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, assets TEXT NOT NULL,
              status TEXT NOT NULL, baseline_step_ms REAL NOT NULL,
              step_ms REAL NOT NULL, ttft_ms REAL NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
              allocated_gpus INTEGER NOT NULL, created_at REAL NOT NULL, ended_at REAL);
            CREATE TABLE IF NOT EXISTS requests (
              tenant TEXT NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL,
              payload TEXT NOT NULL, result TEXT NOT NULL,
              PRIMARY KEY(tenant,request_id));
            CREATE TABLE IF NOT EXISTS alert_receipts (
              receipt_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextlib.contextmanager
    def tx(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, tenant, kind, asset_id=None, incident_id=None, data=None, observed=None):
        now = self.clock()
        trace = hashlib.sha256((incident_id or asset_id or tenant).encode()).hexdigest()[:32]
        span = uuid.uuid4().hex[:16]
        cur = db.execute("INSERT INTO events(tenant,asset_id,incident_id,kind,observed_at,ingested_at,data,trace_id,span_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         (tenant, asset_id, incident_id, kind, now if observed is None else observed, now, json.dumps(data or {}, sort_keys=True), trace, span))
        db.execute("INSERT INTO outbox(sequence) VALUES(?)", (cur.lastrowid,))

    def asset(self, db, tenant, asset_id):
        row = db.execute("SELECT * FROM assets WHERE tenant=? AND id=?", (tenant, asset_id)).fetchone()
        if not row:
            raise LabError("Asset not found", 404)
        return row

    def seed(self):
        with self.tx() as db:
            for tenant in ("alpha", "bravo"):
                for i in range(1, 5):
                    aid = f"{tenant}-gpu-{i:02}"
                    cur = db.execute("INSERT OR IGNORE INTO assets(id,tenant,site,rack,slot,cluster,serial,gpu_count,firmware,lifecycle,health,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, tenant, "lab-north", f"rack-{(i-1)//2+1:02}", i*2, f"{tenant}-training", f"SIM-{tenant.upper()}-{i:04}", 8, GOLDEN, "discovered", "unknown", self.clock()))
                    if cur.rowcount:
                        self.event(db, tenant, "asset.discovered", aid, data={"source": "synthetic", "gpus": 8})

    def commission(self, tenant, asset_id, fail_test=None):
        if fail_test not in (None, "gpu_diagnostics", "fabric_collectives", "storage", "firmware"):
            raise LabError("Unsupported qualification failure", 400)
        with self.tx() as db:
            a = self.asset(db, tenant, asset_id)
            if a["lifecycle"] not in ("discovered", "quarantined", "available"):
                raise LabError("Qualification requires idle or withheld capacity")
            active = db.execute("SELECT 1 FROM incidents WHERE tenant=? AND asset_id=? AND status NOT IN ('resolved','retired')", (tenant,asset_id)).fetchone()
            if active:
                raise LabError("Use the incident validation workflow while a repair is active")
            self._qualify(db, a, fail_test)
        return self.snapshot(tenant)

    def _qualify(self, db, a, fail_test=None, incident_id=None):
        aid, tenant = a["id"], a["tenant"]
        db.execute("UPDATE assets SET lifecycle='qualifying' WHERE id=?", (aid,))
        self.event(db, tenant, "qualification.started", aid, incident_id, {"suite": GOLDEN})
        tests = [("firmware", 1, "match", "equals approved lab baseline"),
                 ("cpu_memory", 1, "check", "present and consistent"),
                 ("gpu_diagnostics", 1, "check", "no injected device fault"),
                 ("fabric_collectives", 160, "GB/s", ">= 140 GB/s synthetic baseline"),
                 ("storage", 2, "ms", "<= 5 ms synthetic baseline"),
                 ("thermal", 45, "C", "<= 80 C synthetic baseline")]
        failures = []
        for name, value, unit, expected in tests:
            bad = name == fail_test or (name == "firmware" and a["firmware"] != GOLDEN)
            bad |= bool(a["fault"] == "optic" and name == "fabric_collectives")
            bad |= bool(a["fault"] == "storage" and name == "storage")
            bad |= bool(a["fault"] == "gpu-memory" and name == "gpu_diagnostics")
            bad |= bool(a["fault"] == "thermal" and name == "thermal")
            if bad:
                failures.append(name)
                # Preserve an interpretable failed measurement: zero latency or
                # temperature would contradict a maximum-threshold failure.
                value = {"storage":42,"thermal":94}.get(name,0)
            db.execute("INSERT INTO qualification VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, tenant, aid, GOLDEN, name, "fail" if bad else "pass", value, unit, expected, "simulated hardware / real policy evaluation", self.clock()))
        # Hardware qualification remains explicitly unmeasured on a CPU laptop.
        db.execute("INSERT INTO qualification VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex,tenant,aid,GOLDEN,"physical_gpu_burnin","skipped",None,"", "requires real GPU hardware", "not executed",self.clock()))
        state = "quarantined" if failures else "available"
        db.execute("UPDATE assets SET lifecycle=?,health=?,commissioned_at=?,last_seen=? WHERE id=?", (state,"degraded" if failures else "healthy",self.clock(),self.clock(),aid))
        self.event(db, tenant, "qualification.failed" if failures else "qualification.passed", aid, incident_id, {"failed_tests": failures, "physical_gpu_burnin": "skipped", "released": not failures})
        return not failures

    def allocate(self, tenant, asset_ids=None):
        with self.tx() as db:
            rows = db.execute("SELECT * FROM assets WHERE tenant=? ORDER BY id", (tenant,)).fetchall()
            ids = asset_ids or [a["id"] for a in rows if a["lifecycle"] == "available"]
            if not ids or len(ids) != len(set(ids)):
                raise LabError("Choose at least one unique available asset", 400)
            selected = [self.asset(db, tenant, aid) for aid in ids]
            if any(a["lifecycle"] != "available" or a["health"] != "healthy" or self.clock()-a["last_seen"] > FRESHNESS_SECONDS for a in selected):
                raise LabError("All selected assets must be healthy, fresh and available")
            wid = "job-" + uuid.uuid4().hex[:10]
            ngpu = sum(a["gpu_count"] for a in selected)
            db.execute("INSERT INTO workloads VALUES(?,?,?,?,?,?,?,?,?,?,?)", (wid,tenant,json.dumps(ids),"running",1000,1000,180,0,ngpu,self.clock(),None))
            for a in selected:
                db.execute("UPDATE assets SET lifecycle='allocated',utilization=0.82,allocated_at=? WHERE id=?", (self.clock(),a["id"]))
                self.event(db,tenant,"workload.allocated",a["id"],data={"job_id":wid,"allocated_gpus":ngpu})
        return self.snapshot(tenant)

    def inject(self, tenant, asset_id, kind):
        if kind not in KINDS:
            raise LabError("Unknown fault kind",400)
        with self.tx() as db:
            a = self.asset(db,tenant,asset_id)
            if a["lifecycle"] != "allocated":
                raise LabError("Inject faults only into allocated lab capacity")
            if a["fault"]:
                raise LabError("Asset already has an injected fault")
            db.execute("UPDATE assets SET fault=?,streak=0 WHERE id=?", (kind,asset_id))
            self.event(db,tenant,"fault.injected",asset_id,data={"kind":kind,"source":"simulated"})
        return self.snapshot(tenant)

    def observe(self, tenant, asset_id, fresh=True, healthy=False, observed_at=None):
        with self.tx() as db:
            a = self.asset(db,tenant,asset_id)
            now = self.clock()
            ts = now if observed_at is None else observed_at
            if ts > now + 2:
                raise LabError("Future-dated telemetry rejected",400)
            if ts < a["last_seen"]:
                self.event(db,tenant,"telemetry.out_of_order",asset_id,data={"ignored":True},observed=ts)
            elif not fresh or now-ts > FRESHNESS_SECONDS:
                db.execute("UPDATE assets SET health='unknown',streak=0 WHERE id=?", (asset_id,))
                self.event(db,tenant,"telemetry.stale",asset_id,data={"automation_withheld":True},observed=ts)
            else:
                fault = None if healthy else a["fault"]
                streak = a["streak"]+1 if fault else 0
                temp = 94 if fault == "thermal" else 45
                errors = a["fabric_errors"] + (12 if fault == "optic" else 0)
                latency = 42 if fault == "storage" else 2
                db.execute("UPDATE assets SET health=?,last_seen=?,streak=?,temperature=?,fabric_errors=?,storage_latency_ms=? WHERE id=?", ("degraded" if fault else "healthy",ts,streak,temp,errors,latency,asset_id))
                self.event(db,tenant,"telemetry.observed",asset_id,data={"fault":fault,"consecutive_samples":streak,"source":"simulated"},observed=ts)
                if fault and streak >= 3:
                    existing = db.execute("SELECT * FROM incidents WHERE tenant=? AND asset_id=? AND status NOT IN ('resolved','retired')", (tenant,asset_id)).fetchone()
                    if existing:
                        self.event(db,tenant,"incident.symptom_deduplicated",asset_id,existing["id"],{"kind":fault})
                    else:
                        iid = "inc-" + uuid.uuid4().hex[:12]
                        db.execute("INSERT INTO incidents(id,tenant,asset_id,kind,stage,status,created_at,policy) VALUES(?,?,?,?,?,?,?,?)", (iid,tenant,asset_id,fault,"isolate","open",now,POLICY_VERSION))
                        self.event(db,tenant,"incident.detected",asset_id,iid,{"kind":fault,"persistence_samples":3,"policy":POLICY_VERSION})
            self._workload_health(db,tenant)
        return self.snapshot(tenant)

    def _workload_health(self, db, tenant):
        for w in db.execute("SELECT * FROM workloads WHERE tenant=?", (tenant,)).fetchall():
            if w["status"] == "released":
                continue
            aa = [self.asset(db,tenant,aid) for aid in json.loads(w["assets"])]
            withheld = any(a["lifecycle"] in ("quarantined","repairing","validating","retired") for a in aa)
            degraded = any(a["fault"] for a in aa)
            unknown = any(a["health"] == "unknown" for a in aa)
            status = "paused_for_repair" if withheld else "unknown" if unknown else "degraded" if degraded else "running"
            step = 4200 if degraded else w["baseline_step_ms"]
            ttft = 920 if degraded else 180
            progress = w["progress"] + (1 if status == "running" else 0)
            db.execute("UPDATE workloads SET status=?,step_ms=?,ttft_ms=?,progress=? WHERE id=?", (status,step,ttft,progress,w["id"]))

    def _incident(self, db, tenant, incident_id):
        row = db.execute("SELECT * FROM incidents WHERE tenant=? AND id=?", (tenant,incident_id)).fetchone()
        if not row:
            raise LabError("Incident not found",404)
        return row

    def advance(self, tenant, incident_id, expected_version=None, fail_after_commit=False):
        """Exactly one durable transition. Retry reads the persisted next stage.

        fail_after_commit models a worker dying after effects commit, before ack.
        External ticket adapters must honor the same operation_id at their API.
        """
        with self.tx() as db:
            inc = self._incident(db,tenant,incident_id)
            if expected_version is not None and inc["version"] != expected_version:
                raise LabError("Stale workflow version")
            if inc["status"] in ("resolved","escalated","retired"):
                return dict(inc)
            a = self.asset(db,tenant,inc["asset_id"])
            if a["health"] == "unknown" or self.clock()-a["last_seen"] > FRESHNESS_SECONDS:
                raise LabError("Fresh evidence required before a repair action")
            stage = inc["stage"]
            if stage == "wait_repair":
                ticket = db.execute("SELECT * FROM tickets WHERE incident_id=?", (incident_id,)).fetchone()
                if not ticket or ticket["status"] != "completed":
                    raise LabError("Waiting for technician completion")
                if not ticket["success"]:
                    db.execute("UPDATE incidents SET status='escalated',last_error='technician reported unsuccessful repair' WHERE id=?",(incident_id,))
                    self.event(db,tenant,"incident.escalated",a["id"],incident_id,{"reason":"physical repair unsuccessful"})
                    return {"status":"escalated"}
                next_stage = "validate"
            elif stage == "isolate":
                db.execute("UPDATE assets SET lifecycle='quarantined',utilization=0 WHERE id=?", (a["id"],))
                db.execute("UPDATE incidents SET isolated_at=? WHERE id=?",(self.clock(),incident_id))
                next_stage = "diagnose"
                self.event(db,tenant,"capacity.isolated",a["id"],incident_id,{"workload_action":"simulated coordinated pause; no live job killed"})
            elif stage == "diagnose":
                sop = SOPS[inc["kind"]]
                self.event(db,tenant,"diagnosis.completed",a["id"],incident_id,{"observed_symptom":inc["kind"],"hypothesis":"injected lab fault","procedure":sop["id"],"evidence_source":"synthetic"})
                next_stage = "remediate"
            elif stage == "remediate":
                sop = SOPS[inc["kind"]]
                if sop["path"] == "physical":
                    tid = "ticket-" + hashlib.sha256(incident_id.encode()).hexdigest()[:10]
                    location = f"{a['site']} / {a['rack']} / slot {a['slot']} / {a['serial']}"
                    db.execute("INSERT OR IGNORE INTO tickets VALUES(?,?,?,?,?,?,?,?,?)", (tid,incident_id,tenant,location,sop["instruction"],"open",None,self.clock(),None))
                    self.event(db,tenant,"ticket.created",a["id"],incident_id,{"ticket_id":tid,"location":location,"procedure":sop["id"]})
                    next_stage = "wait_repair"
                else:
                    db.execute("UPDATE assets SET fault=NULL,temperature=45,streak=0,lifecycle='repairing' WHERE id=?", (a["id"],))
                    self.event(db,tenant,"remediation.applied",a["id"],incident_id,{"allowlisted_action":"simulated cooling profile","procedure":sop["id"]})
                    next_stage = "validate"
            elif stage == "validate":
                if inc["attempts"] >= 2:
                    db.execute("UPDATE incidents SET status='escalated',last_error='validation retry budget exhausted' WHERE id=?", (incident_id,))
                    self.event(db,tenant,"incident.escalated",a["id"],incident_id,{"reason":"validation retry budget exhausted"})
                    return {"status":"escalated"}
                db.execute("UPDATE incidents SET attempts=attempts+1 WHERE id=?",(incident_id,))
                ok = self._qualify(db,a,incident_id=incident_id)
                if not ok:
                    self.event(db,tenant,"revalidation.failed",a["id"],incident_id,{"remains_quarantined":True})
                    next_stage = "validate"
                else:
                    db.execute("UPDATE assets SET lifecycle='allocated',health='healthy',utilization=0.82 WHERE id=?", (a["id"],))
                    db.execute("UPDATE incidents SET status='resolved',resolved_at=?,last_error=NULL WHERE id=?", (self.clock(),incident_id))
                    self.event(db,tenant,"incident.resolved",a["id"],incident_id,{"verified":True,"history_retained":True,"workload_resumed":True})
                    next_stage = "done"
            else:
                raise LabError("Invalid workflow stage",500)
            op = f"{incident_id}:{stage}:{inc['attempts'] if stage=='validate' else 0}:v1"
            db.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?)", (op,tenant,incident_id,stage,json.dumps({"next_stage":next_stage}),self.clock()))
            db.execute("UPDATE incidents SET stage=?,version=version+1 WHERE id=?", (next_stage,incident_id))
            self._workload_health(db,tenant)
        if fail_after_commit:
            raise RuntimeError("Injected worker crash after committed transition, before acknowledgement")
        return self.snapshot(tenant)

    def complete_ticket(self, tenant, ticket_id, success=True, clear_fault=True):
        with self.tx() as db:
            ticket = db.execute("SELECT * FROM tickets WHERE tenant=? AND id=?",(tenant,ticket_id)).fetchone()
            if not ticket:
                raise LabError("Ticket not found",404)
            if ticket["status"] == "completed":
                if bool(ticket["success"]) != bool(success):
                    raise LabError("Completed repair outcome is immutable")
                return dict(ticket)
            inc = self._incident(db,tenant,ticket["incident_id"])
            db.execute("UPDATE tickets SET status='completed',success=?,completed_at=? WHERE id=?",(int(success),self.clock(),ticket_id))
            if success and clear_fault:
                db.execute("UPDATE assets SET fault=NULL,streak=0,temperature=45,storage_latency_ms=2,health='healthy',last_seen=?,lifecycle='repairing' WHERE id=?",(self.clock(),inc["asset_id"]))
            self.event(db,tenant,"ticket.completed",inc["asset_id"],inc["id"],{"success":bool(success),"clear_fault":bool(clear_fault),"not_yet_released":True})
        return self.snapshot(tenant)

    def release(self, tenant, workload_id):
        with self.tx() as db:
            w = db.execute("SELECT * FROM workloads WHERE tenant=? AND id=?", (tenant,workload_id)).fetchone()
            if not w:
                raise LabError("Workload not found",404)
            if w["status"] == "released":
                return dict(w)
            for aid in json.loads(w["assets"]):
                a = self.asset(db,tenant,aid)
                if a["lifecycle"] in ("quarantined","repairing","validating") or a["fault"]:
                    raise LabError("Cannot bypass an active repair by releasing a job")
                self._qualify(db,a)
                db.execute("UPDATE assets SET utilization=0,allocated_at=NULL WHERE id=?",(aid,))
            db.execute("UPDATE workloads SET status='released',ended_at=? WHERE id=?", (self.clock(),workload_id))
            self.event(db,tenant,"workload.released",data={"job_id":workload_id,"baseline_rechecked":True})
        return self.snapshot(tenant)

    def retire(self, tenant, asset_id):
        with self.tx() as db:
            a = self.asset(db,tenant,asset_id)
            if a["lifecycle"] == "allocated":
                raise LabError("Release workload before retirement")
            db.execute("UPDATE assets SET lifecycle='retired',health='unknown',utilization=0 WHERE id=?", (asset_id,))
            db.execute("UPDATE incidents SET status='retired',stage='done' WHERE tenant=? AND asset_id=? AND status!='resolved'", (tenant,asset_id))
            self.event(db,tenant,"asset.retired",asset_id,data={"credentials_cleanup":"simulated acknowledgement","hardware_data_erasure":"not performed by this lab"})
        return self.snapshot(tenant)

    def scenario(self, tenant, scenario):
        if scenario not in ("optic","thermal","storage","burnin","telemetry","worker-crash"):
            raise LabError("Unknown scenario",400)
        self.seed()
        snap = self.snapshot(tenant)
        target = next((a for a in snap["assets"] if a["lifecycle"] in ("discovered","available")),None)
        if not target:
            raise LabError("No idle node for another scenario; finish active repairs or use a fresh database")
        aid = target["id"]
        if scenario == "burnin":
            return self.commission(tenant,aid,fail_test="gpu_diagnostics")
        self.commission(tenant,aid)
        self.allocate(tenant,[aid])
        if scenario == "telemetry":
            return self.observe(tenant,aid,fresh=False)
        kind = "optic" if scenario == "worker-crash" else scenario
        self.inject(tenant,aid,kind)
        for _ in range(3):
            self.observe(tenant,aid)
        inc = next(i for i in self.snapshot(tenant)["incidents"] if i["asset_id"] == aid and i["status"] == "open")
        if scenario == "worker-crash":
            try:
                self.advance(tenant,inc["id"],fail_after_commit=True)
            except RuntimeError:
                pass
            with self.tx() as db:
                self.event(db,tenant,"worker.recovered",aid,inc["id"],{"restart_reads_durable_stage":True})
        return self.snapshot(tenant)

    def receive_alerts(self, tenant, payload):
        """Audit tenant-scoped notifications. Alert text never becomes an action."""
        alerts = payload.get('alerts', [])
        if not isinstance(alerts,list) or len(alerts)>100:
            raise LabError('alerts must be a list of at most 100 entries',400)
        accepted = duplicates = 0
        with self.tx() as db:
            for alert in alerts:
                if not isinstance(alert,dict): raise LabError('Invalid alert',400)
                labels = alert.get('labels',{})
                if not isinstance(labels,dict): raise LabError('Invalid labels',400)
                if labels.get('tenant') not in (None,tenant): continue
                aid = labels.get('asset_id')
                if aid: self.asset(db,tenant,aid)
                status = alert.get('status','firing')
                if status not in ('firing','resolved'): raise LabError('Invalid alert status',400)
                name = str(labels.get('alertname','unknown'))[:128]
                key = json.dumps([tenant,alert.get('fingerprint'),alert.get('startsAt'),status,aid,name],sort_keys=True)
                receipt = hashlib.sha256(key.encode()).hexdigest()
                inserted = db.execute('INSERT OR IGNORE INTO alert_receipts VALUES(?,?,?)',(receipt,tenant,self.clock())).rowcount
                if not inserted: duplicates += 1; continue
                self.event(db,tenant,'alert.received',aid,data={'alertname':name,'status':status,'action':'audit only; repair requires fresh policy evidence'})
                accepted += 1
        return {'accepted':accepted,'duplicates':duplicates,'repair_actions_executed':0}

    def snapshot(self, tenant):
        with self._connect() as db:
            assets = [dict(r) for r in db.execute("SELECT * FROM assets WHERE tenant=? ORDER BY id",(tenant,))]
            for asset in assets:
                if self.clock()-asset["last_seen"] > FRESHNESS_SECONDS:
                    asset["health"] = "unknown"
            incidents = [dict(r) for r in db.execute("SELECT * FROM incidents WHERE tenant=? ORDER BY created_at,id",(tenant,))]
            tickets = [dict(r) for r in db.execute("SELECT * FROM tickets WHERE tenant=? ORDER BY created_at,id",(tenant,))]
            events = [dict(r) for r in db.execute("SELECT * FROM events WHERE tenant=? ORDER BY sequence DESC LIMIT 250",(tenant,))][::-1]
            for e in events:
                e["data"] = json.loads(e["data"])
            qual = [dict(r) for r in db.execute("SELECT * FROM qualification WHERE tenant=? ORDER BY observed_at,id",(tenant,))]
            work = [dict(r) for r in db.execute("SELECT * FROM workloads WHERE tenant=? ORDER BY created_at,id",(tenant,))]
            for w in work:
                w["assets"] = json.loads(w["assets"])
            total = sum(a["gpu_count"] for a in assets)
            available = sum(a["gpu_count"] for a in assets if a["lifecycle"] == "available" and a["health"] == "healthy")
            blocked = sum(a["gpu_count"] for a in assets if a["lifecycle"] in ("quarantined","repairing","validating"))
            exposure = 0.0
            job_exposure = {}
            for w in work:
                # Intersect with the job lifetime, then union correlated incident intervals.
                end_job = w["ended_at"] or self.clock()
                intervals = sorted((max(i["created_at"],w["created_at"]),min(i["resolved_at"] or self.clock(),end_job)) for i in incidents if i["asset_id"] in w["assets"] and (i["resolved_at"] or self.clock()) > w["created_at"] and i["created_at"] < end_job)
                merged = []
                for start,end in intervals:
                    if merged and start <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1],end)
                    else:
                        merged.append([start,end])
                hours = sum(max(0,end-start) for start,end in merged)/3600*w["allocated_gpus"]
                job_exposure[w["id"]] = hours
                exposure += hours
            return {"schema_version":1,"mode":"synthetic-hardware-real-control-plane","tenant":tenant,"generated_at":self.clock(),"policy_version":POLICY_VERSION,
                    "assets":assets,"incidents":incidents,"tickets":tickets,"events":events,"qualification":qual,"workloads":work,
                    "summary":{"total_gpus":total,"available_gpus":available,"withheld_gpus":blocked,"open_incidents":sum(i["status"] not in ("resolved","retired") for i in incidents),"resolved_incidents":sum(i["status"]=="resolved" for i in incidents)},
                    "cost":{"basis":"illustrative capacity exposure, not cash savings or Nscale pricing","assumed_gpu_hour_usd":3,"exposed_gpu_hours":round(exposure,6),"capacity_exposure_usd":round(exposure*3,4),"job_exposure_gpu_hours":job_exposure}}

    def metrics(self):
        lines = ["# HELP fleet_asset_state Current lifecycle state of a synthetic asset.","# TYPE fleet_asset_state gauge"]
        for tenant in ("alpha","bravo"):
            s = self.snapshot(tenant)
            for a in s["assets"]:
                labels = f'tenant="{tenant}",site="{a["site"]}",asset_id="{a["id"]}",source="simulated"'
                for state in STATES:
                    lines.append(f'fleet_asset_state{{{labels},state="{state}"}} {int(a["lifecycle"]==state)}')
                lines.extend([f'fleet_telemetry_age_seconds{{{labels}}} {max(0,self.clock()-a["last_seen"]):.3f}',
                    f'lab_gpu_temperature_celsius{{{labels}}} {a["temperature"]}',
                    f'lab_fabric_errors_total{{{labels}}} {a["fabric_errors"]}',
                    f'lab_gpu_utilization_ratio{{{labels}}} {a["utilization"]}'])
            for status in ("open","resolved","escalated"):
                lines.append(f'fleet_incidents{{tenant="{tenant}",status="{status}"}} {sum(i["status"]==status for i in s["incidents"])}')
            lines.append(f'fleet_capacity_exposure_gpu_hours{{tenant="{tenant}"}} {s["cost"]["exposed_gpu_hours"]}')
        with self._connect() as db:
            pending = db.execute("SELECT count(*) FROM outbox WHERE logs_sent=0 OR traces_sent=0").fetchone()[0]
            lines.append(f"fleet_outbox_pending_events {pending}")
        return "\n".join(lines)+"\n"
