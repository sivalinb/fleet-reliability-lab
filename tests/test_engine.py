import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from fleetlab.engine import Engine, LabError, GOLDEN

class Clock:
    def __init__(self): self.now = 1800000000.0
    def __call__(self): return self.now
    def add(self,seconds): self.now += seconds

class FleetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tmp.name)/'lab.sqlite3'
        self.e = Engine(self.path,self.clock)
        self.e.seed()
        self.a = 'alpha-gpu-01'

    def tearDown(self): self.tmp.cleanup()

    def ready(self,ids=None):
        ids=ids or [self.a]
        for aid in ids: self.e.commission('alpha',aid)
        return self.e.allocate('alpha',ids)

    def incident(self,kind='optic',ids=None):
        self.ready(ids)
        self.e.inject('alpha',self.a,kind)
        for _ in range(3): self.e.observe('alpha',self.a)
        return self.e.snapshot('alpha')['incidents'][0]

    def advance_to_ticket(self,inc):
        for _ in range(3): self.e.advance('alpha',inc['id'])
        return self.e.snapshot('alpha')['tickets'][0]

    def resolve(self,inc):
        t=self.advance_to_ticket(inc)
        self.e.complete_ticket('alpha',t['id'])
        self.e.advance('alpha',inc['id'])
        return self.e.advance('alpha',inc['id'])

    def test_seed_is_idempotent(self):
        self.e.seed(); self.assertEqual(len(self.e.snapshot('alpha')['assets']),4)
        self.assertEqual(len(self.e.snapshot('alpha')['events']),4)

    def test_initial_capacity_is_not_available(self):
        self.assertEqual(self.e.snapshot('alpha')['summary']['available_gpus'],0)

    def test_commission_opens_available_capacity(self):
        self.assertEqual(self.e.commission('alpha',self.a)['summary']['available_gpus'],8)

    def test_physical_gpu_test_is_skipped(self):
        s=self.e.commission('alpha',self.a)
        q=[q for q in s['qualification'] if q['test']=='physical_gpu_burnin']
        self.assertEqual(q[0]['status'],'skipped'); self.assertIsNone(q[0]['value'])

    def test_burnin_failure_withholds_capacity(self):
        s=self.e.commission('alpha',self.a,'gpu_diagnostics')
        self.assertEqual(s['assets'][0]['lifecycle'],'quarantined')
        self.assertEqual(s['summary']['available_gpus'],0)

    def test_failed_node_cannot_be_allocated(self):
        s=self.e.commission('alpha',self.a,'storage')
        failed=next(q for q in s['qualification'] if q['test']=='storage')
        self.assertEqual(failed['status'],'fail')
        self.assertGreater(failed['value'],5)  # operator evidence must violate its stated maximum
        with self.assertRaises(LabError): self.e.allocate('alpha',[self.a])

    def test_firmware_drift_blocks_qualification(self):
        with self.e.tx() as db: db.execute("UPDATE assets SET firmware='old' WHERE id=?",(self.a,))
        self.assertEqual(self.e.commission('alpha',self.a)['summary']['available_gpus'],0)

    def test_invalid_test_name_is_rejected(self):
        with self.assertRaises(LabError): self.e.commission('alpha',self.a,'anything')

    def test_allocated_node_cannot_be_recommissioned(self):
        self.ready()
        with self.assertRaises(LabError): self.e.commission('alpha',self.a)

    def test_two_samples_do_not_trigger_repair(self):
        self.ready();self.e.inject('alpha',self.a,'optic')
        for _ in range(2): self.e.observe('alpha',self.a)
        self.assertEqual(self.e.snapshot('alpha')['incidents'],[])

    def test_healthy_sample_resets_persistence(self):
        self.ready();self.e.inject('alpha',self.a,'optic')
        for _ in range(2): self.e.observe('alpha',self.a)
        self.e.observe('alpha',self.a,healthy=True)
        self.e.observe('alpha',self.a)
        self.assertEqual(self.e.snapshot('alpha')['incidents'],[])

    def test_three_samples_create_incident(self):
        self.incident();self.assertEqual(len(self.e.snapshot('alpha')['incidents']),1)

    def test_repeated_alerts_do_not_duplicate_incident(self):
        self.incident()
        for _ in range(8): self.e.observe('alpha',self.a)
        self.assertEqual(len(self.e.snapshot('alpha')['incidents']),1)

    def test_stale_telemetry_is_unknown_not_hardware_failure(self):
        self.ready();s=self.e.observe('alpha',self.a,fresh=False)
        self.assertEqual(s['assets'][0]['health'],'unknown');self.assertEqual(s['incidents'],[])

    def test_expired_available_capacity_not_counted(self):
        self.e.commission('alpha',self.a);self.clock.add(31)
        self.assertEqual(self.e.snapshot('alpha')['summary']['available_gpus'],0)

    def test_stale_capacity_cannot_be_allocated(self):
        self.e.commission('alpha',self.a);self.clock.add(31)
        with self.assertRaises(LabError): self.e.allocate('alpha',[self.a])

    def test_stale_evidence_blocks_repair(self):
        i=self.incident();self.clock.add(31)
        with self.assertRaises(LabError): self.e.advance('alpha',i['id'])

    def test_out_of_order_sample_ignored(self):
        self.ready();self.e.observe('alpha',self.a,observed_at=self.clock()-10)
        self.assertEqual(self.e.snapshot('alpha')['assets'][0]['last_seen'],self.clock())

    def test_future_timestamp_rejected(self):
        with self.assertRaises(LabError): self.e.observe('alpha',self.a,observed_at=self.clock()+10)

    def test_tenant_cannot_commission_another_asset(self):
        with self.assertRaises(LabError) as c:self.e.commission('bravo',self.a)
        self.assertEqual(c.exception.status,404)

    def test_snapshot_is_tenant_scoped(self):
        self.incident();s=self.e.snapshot('bravo')
        self.assertFalse(s['incidents']);self.assertTrue(all(a['tenant']=='bravo' for a in s['assets']))

    def test_duplicate_allocation_rejected(self):
        self.e.commission('alpha',self.a)
        with self.assertRaises(LabError):self.e.allocate('alpha',[self.a,self.a])

    def test_isolation_pauses_workload(self):
        i=self.incident();s=self.e.advance('alpha',i['id'])
        self.assertEqual(s['assets'][0]['lifecycle'],'quarantined')
        self.assertEqual(s['workloads'][0]['status'],'paused_for_repair')

    def test_diagnosis_names_versioned_procedure(self):
        i=self.incident();self.e.advance('alpha',i['id']);s=self.e.advance('alpha',i['id'])
        ev=next(x for x in s['events'] if x['kind']=='diagnosis.completed')
        self.assertEqual(ev['data']['procedure'],'optic-replace/v1')

    def test_ticket_includes_exact_location(self):
        i=self.incident();t=self.advance_to_ticket(i)
        self.assertIn('rack-01 / slot 2',t['location']);self.assertIn('SIM-ALPHA',t['location'])

    def test_wait_cannot_advance_without_technician(self):
        i=self.incident();self.advance_to_ticket(i)
        with self.assertRaises(LabError):self.e.advance('alpha',i['id'])

    def test_completion_does_not_immediately_release_node(self):
        i=self.incident();t=self.advance_to_ticket(i);s=self.e.complete_ticket('alpha',t['id'])
        self.assertEqual(s['assets'][0]['lifecycle'],'repairing')
        self.assertNotEqual(s['incidents'][0]['status'],'resolved')

    def test_repair_is_revalidated_before_resolve(self):
        s=self.resolve(self.incident())
        self.assertEqual(s['incidents'][0]['status'],'resolved')
        self.assertEqual(s['workloads'][0]['status'],'running')
        self.assertEqual(s['workloads'][0]['step_ms'],1000)

    def test_unsuccessful_repair_escalates(self):
        i=self.incident();t=self.advance_to_ticket(i);self.e.complete_ticket('alpha',t['id'],success=False)
        self.e.advance('alpha',i['id']);self.assertEqual(self.e.snapshot('alpha')['incidents'][0]['status'],'escalated')

    def test_validation_failure_keeps_quarantine(self):
        i=self.incident();t=self.advance_to_ticket(i);self.e.complete_ticket('alpha',t['id'],clear_fault=False)
        self.e.advance('alpha',i['id']);s=self.e.advance('alpha',i['id'])
        self.assertEqual(s['assets'][0]['lifecycle'],'quarantined');self.assertEqual(s['incidents'][0]['status'],'open')

    def test_retry_budget_bounds_failed_validation(self):
        i=self.incident();t=self.advance_to_ticket(i);self.e.complete_ticket('alpha',t['id'],clear_fault=False)
        for _ in range(4):self.e.advance('alpha',i['id'])
        self.assertEqual(self.e.snapshot('alpha')['incidents'][0]['status'],'escalated')

    def test_completed_ticket_outcome_immutable(self):
        i=self.incident();t=self.advance_to_ticket(i);self.e.complete_ticket('alpha',t['id'])
        with self.assertRaises(LabError):self.e.complete_ticket('alpha',t['id'],False)

    def test_ticket_completion_retry_is_idempotent(self):
        i=self.incident();t=self.advance_to_ticket(i);self.e.complete_ticket('alpha',t['id']);self.e.complete_ticket('alpha',t['id'])
        n=sum(e['kind']=='ticket.completed' for e in self.e.snapshot('alpha')['events']);self.assertEqual(n,1)

    def test_compare_and_swap_rejects_stale_version(self):
        i=self.incident();self.e.advance('alpha',i['id'],expected_version=0)
        with self.assertRaises(LabError):self.e.advance('alpha',i['id'],expected_version=0)

    def test_worker_crash_after_commit_survives_reopen(self):
        i=self.incident()
        with self.assertRaises(RuntimeError):self.e.advance('alpha',i['id'],fail_after_commit=True)
        e2=Engine(self.path,self.clock);self.assertEqual(e2.snapshot('alpha')['incidents'][0]['stage'],'diagnose')

    def test_crash_during_ticket_creation_does_not_duplicate_ticket(self):
        i=self.incident();self.e.advance('alpha',i['id']);self.e.advance('alpha',i['id'])
        with self.assertRaises(RuntimeError):self.e.advance('alpha',i['id'],fail_after_commit=True)
        e2=Engine(self.path,self.clock)
        with self.assertRaises(LabError):e2.advance('alpha',i['id'])
        self.assertEqual(len(e2.snapshot('alpha')['tickets']),1)

    def test_concurrent_workers_allow_one_expected_transition(self):
        i=self.incident()
        def run(_):
            try:self.e.advance('alpha',i['id'],expected_version=0);return 'ok'
            except LabError:return 'stale'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:r=list(pool.map(run,range(8)))
        self.assertEqual(r.count('ok'),1);self.assertEqual(r.count('stale'),7)

    def test_remote_thermal_path_needs_no_ticket(self):
        i=self.incident('thermal')
        for _ in range(4):self.e.advance('alpha',i['id'])
        s=self.e.snapshot('alpha');self.assertEqual(s['incidents'][0]['status'],'resolved');self.assertEqual(s['tickets'],[])

    def test_repair_history_survives_resolution(self):
        s=self.resolve(self.incident());self.assertTrue(any(e['kind']=='incident.resolved' for e in s['events']))
        self.assertGreater(len(s['qualification']),7)

    def test_release_rechecks_baseline(self):
        s=self.ready();wid=s['workloads'][0]['id'];s=self.e.release('alpha',wid)
        self.assertEqual(s['assets'][0]['lifecycle'],'available');self.assertEqual(s['workloads'][0]['status'],'released')

    def test_released_workload_does_not_restart_on_observation(self):
        s=self.ready();self.e.release('alpha',s['workloads'][0]['id']);s=self.e.observe('alpha',self.a)
        self.assertEqual(s['workloads'][0]['status'],'released')

    def test_active_repair_cannot_be_bypassed_by_job_release(self):
        i=self.incident();self.e.advance('alpha',i['id']);w=self.e.snapshot('alpha')['workloads'][0]
        with self.assertRaises(LabError):self.e.release('alpha',w['id'])

    def test_allocated_capacity_cannot_be_retired(self):
        self.ready()
        with self.assertRaises(LabError):self.e.retire('alpha',self.a)

    def test_retired_capacity_cannot_be_allocated(self):
        self.e.retire('alpha',self.a)
        with self.assertRaises(LabError):self.e.allocate('alpha',[self.a])

    def test_metrics_have_bounded_labels_and_explicit_simulation(self):
        self.ready();m=self.e.metrics();self.assertIn('source="simulated"',m)
        self.assertNotIn('trace_id=',m);self.assertNotIn('job_id=',m)

    def test_cost_uses_job_blast_radius(self):
        self.incident(ids=[self.a,'alpha-gpu-02']);self.clock.add(10)
        s=self.e.snapshot('alpha');self.assertAlmostEqual(s['cost']['exposed_gpu_hours'],16*10/3600,places=5)

    def test_overlapping_incidents_not_double_counted(self):
        self.incident(ids=[self.a,'alpha-gpu-02'])
        self.e.inject('alpha','alpha-gpu-02','thermal')
        for _ in range(3):self.e.observe('alpha','alpha-gpu-02')
        self.clock.add(10);self.assertAlmostEqual(self.e.snapshot('alpha')['cost']['exposed_gpu_hours'],16*10/3600,places=5)

    def test_old_incident_not_charged_to_new_job(self):
        i=self.incident();self.clock.add(10);s=self.resolve(i)
        self.e.release('alpha',s['workloads'][0]['id']);self.clock.add(2)
        s=self.e.allocate('alpha',[self.a]);new=s['workloads'][-1]
        self.assertEqual(s['cost']['job_exposure_gpu_hours'][new['id']],0)

    def test_unrecognized_fault_does_not_mutate(self):
        self.ready()
        with self.assertRaises(LabError):self.e.inject('alpha',self.a,'shell-command')
        self.assertIsNone(self.e.snapshot('alpha')['assets'][0]['fault'])

    def test_full_scenarios_are_available(self):
        for scenario in ['optic','thermal','storage','burnin','telemetry','worker-crash']:
            with self.subTest(scenario=scenario):
                ep=Engine(Path(self.tmp.name)/(scenario+'.db'),self.clock);ep.seed()
                self.assertEqual(ep.scenario('alpha',scenario)['mode'],'synthetic-hardware-real-control-plane')

if __name__=='__main__':unittest.main()
