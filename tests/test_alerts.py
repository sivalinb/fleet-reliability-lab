import tempfile
import unittest
from pathlib import Path
from fleetlab.engine import Engine,LabError

class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'lab.sqlite3');self.e.seed()
        self.alert={'status':'firing','fingerprint':'abc','startsAt':'2026-10-15T00:00:00Z','labels':{'tenant':'alpha','asset_id':'alpha-gpu-01','alertname':'FleetTelemetryStale'}}
    def test_retry_is_deduplicated(self):
        self.assertEqual(self.e.receive_alerts('alpha',{'alerts':[self.alert]})['accepted'],1)
        self.assertEqual(self.e.receive_alerts('alpha',{'alerts':[self.alert]})['duplicates'],1)
    def test_alert_cannot_execute_or_open_repair(self):
        result=self.e.receive_alerts('alpha',{'alerts':[self.alert]})
        self.assertEqual(result['repair_actions_executed'],0)
        self.assertEqual(self.e.snapshot('alpha')['incidents'],[])
    def test_cross_tenant_notification_is_ignored(self):
        self.assertEqual(self.e.receive_alerts('bravo',{'alerts':[self.alert]})['accepted'],0)
    def test_spoofed_asset_does_not_cross_tenant(self):
        self.alert['labels']['asset_id']='bravo-gpu-01'
        with self.assertRaises(LabError):self.e.receive_alerts('alpha',{'alerts':[self.alert]})
    def test_resolution_is_separate_audit_event(self):
        self.e.receive_alerts('alpha',{'alerts':[self.alert]})
        self.alert['status']='resolved'
        self.assertEqual(self.e.receive_alerts('alpha',{'alerts':[self.alert]})['accepted'],1)
