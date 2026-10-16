import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from fleetlab.engine import Engine
from fleetlab.telemetry import deliver

class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.e=Engine(Path(self.tmp.name)/'lab.db');self.e.seed()
        self.received=[];self.fail=False;self.partial=False
        outer=self
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.received.append((self.path,data))
                self.send_response(503 if outer.fail else 200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'partialSuccess':{'rejectedLogRecords':1,'rejectedSpans':1}} if outer.partial else {}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Receiver);self.t=threading.Thread(target=self.server.serve_forever,daemon=True);self.t.start()
        self.endpoint=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.t.join();self.tmp.cleanup()
    def pending(self):
        with self.e._connect() as db:return db.execute('SELECT count(*) FROM outbox WHERE logs_sent=0 OR traces_sent=0').fetchone()[0]
    def test_exports_both_signal_types(self):
        r=deliver(self.e,self.endpoint);self.assertEqual(r['delivered_channels'],16);self.assertEqual(self.pending(),0)
        self.assertEqual({p for p,_ in self.received},{'/v1/logs','/v1/traces'})
    def test_successful_records_not_exported_again(self):
        deliver(self.e,self.endpoint);self.received.clear();deliver(self.e,self.endpoint);self.assertFalse(self.received)
    def test_failed_backend_retains_durable_outbox(self):
        self.fail=True;deliver(self.e,self.endpoint);self.assertEqual(self.pending(),8)
    def test_delivery_recovers_after_reopening_database(self):
        self.fail=True;deliver(self.e,self.endpoint);self.fail=False
        other=Engine(Path(self.tmp.name)/'lab.db');deliver(other,self.endpoint);self.assertEqual(self.pending(),0)
    def test_partial_rejection_is_not_acknowledged(self):
        self.partial=True;deliver(self.e,self.endpoint);self.assertEqual(self.pending(),8)
    def test_ids_are_stable_between_retries(self):
        self.fail=True;deliver(self.e,self.endpoint,limit=1)
        span1=next(d for p,d in self.received if p=='/v1/traces')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        self.received.clear();self.fail=False;deliver(self.e,self.endpoint,limit=1)
        span2=next(d for p,d in self.received if p=='/v1/traces')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        self.assertEqual(span1['spanId'],span2['spanId']);self.assertEqual(span1['traceId'],span2['traceId'])
    def test_trace_and_log_correlation(self):
        deliver(self.e,self.endpoint,limit=1)
        log=next(d for p,d in self.received if p=='/v1/logs')['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        span=next(d for p,d in self.received if p=='/v1/traces')['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        self.assertEqual(log['traceId'],span['traceId']);self.assertEqual(log['spanId'],span['spanId'])
    def test_synthetic_origin_is_explicit(self):
        deliver(self.e,self.endpoint,limit=1)
        resource=self.received[0][1]['resourceLogs'][0]['resource']
        self.assertTrue(any(a['value']['stringValue']=='synthetic-hardware-real-control-plane' for a in resource['attributes']))
    def test_export_limit_is_bounded(self):
        deliver(self.e,self.endpoint,limit=2);self.assertEqual(self.pending(),6)

if __name__=='__main__':unittest.main()
