import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from fleetlab.engine import Engine
from fleetlab.server import make_handler

class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.e=Engine(root/'lab.db');self.e.seed()
        web=root/'web';web.mkdir();(web/'index.html').write_text('<h1>Fleet lab</h1>')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.e,web))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def request(self,path,token=None,data=None,method=None,origin=None):
        h={}
        if token:h['Authorization']='Bearer '+token
        if origin:h['Origin']=origin
        if data is not None:h['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,data=None if data is None else json.dumps(data).encode(),headers=h,method=method)
        try:
            with urllib.request.urlopen(req) as r:return r.status,r.read(),r.headers
        except urllib.error.HTTPError as r:return r.code,r.read(),r.headers

    def test_health_is_public(self):self.assertEqual(self.request('/api/health')[0],200)
    def test_static_story_is_public(self):self.assertIn(b'Fleet lab',self.request('/')[1])
    def test_tenant_api_requires_token(self):self.assertEqual(self.request('/api/fleet')[0],401)
    def test_bad_token_rejected(self):self.assertEqual(self.request('/api/fleet','invalid')[0],401)
    def test_authenticated_snapshot(self):self.assertEqual(json.loads(self.request('/api/fleet','alpha-operator-demo')[1])['tenant'],'alpha')
    def test_viewer_can_read(self):self.assertEqual(self.request('/api/fleet','alpha-viewer-demo')[0],200)
    def test_viewer_cannot_mutate(self):self.assertEqual(self.request('/api/scenarios','alpha-viewer-demo',{'scenario':'optic'})[0],403)
    def test_cannot_select_another_tenant_in_body(self):
        s=json.loads(self.request('/api/scenarios','alpha-operator-demo',{'scenario':'optic','tenant':'bravo'})[1])
        self.assertEqual(s['tenant'],'alpha');self.assertFalse(self.e.snapshot('bravo')['incidents'])
    def test_cross_tenant_asset_returns_not_found(self):self.assertEqual(self.request('/api/assets/bravo-gpu-01/commission','alpha-operator-demo',{})[0],404)
    def test_cross_tenant_incident_returns_not_found(self):
        i=self.e.scenario('bravo','optic')['incidents'][0]
        self.assertEqual(self.request('/api/incidents/'+i['id']+'/advance','alpha-operator-demo',{'expected_version':0})[0],404)
    def test_metrics_requires_scraper_role(self):self.assertEqual(self.request('/metrics','alpha-operator-demo')[0],403)
    def test_metrics_scraper_cannot_query_tenant_api(self):self.assertEqual(self.request('/api/fleet','metrics-admin-demo')[0],403)
    def test_metrics_has_prometheus_content_type(self):
        status,body,h=self.request('/metrics','metrics-admin-demo');self.assertEqual(status,200)
        self.assertIn('text/plain',h['Content-Type']);self.assertIn(b'fleet_asset_state',body)
    def test_post_requires_expected_version(self):
        i=self.e.scenario('alpha','optic')['incidents'][0]
        self.assertEqual(self.request('/api/incidents/'+i['id']+'/advance','alpha-operator-demo',{})[0],400)
    def test_request_cas_protects_duplicate_action(self):
        i=self.e.scenario('alpha','optic')['incidents'][0];p='/api/incidents/'+i['id']+'/advance'
        self.assertEqual(self.request(p,'alpha-operator-demo',{'expected_version':0})[0],200)
        self.assertEqual(self.request(p,'alpha-operator-demo',{'expected_version':0})[0],409)
    def test_invalid_scenario_rejected(self):self.assertEqual(self.request('/api/scenarios','alpha-operator-demo',{'scenario':'reboot-real-host'})[0],400)
    def test_non_object_json_rejected(self):self.assertEqual(self.request('/api/scenarios','alpha-operator-demo',[])[0],400)
    def test_unexpected_origin_rejected(self):self.assertEqual(self.request('/api/scenarios','alpha-operator-demo',{'scenario':'optic'},origin='https://untrusted.example')[0],403)
    def test_approved_origin_echoed(self):self.assertEqual(self.request('/api/fleet','alpha-operator-demo',origin='https://sivalinb.github.io')[2]['Access-Control-Allow-Origin'],'https://sivalinb.github.io')
    def test_unknown_endpoint_is_404(self):self.assertEqual(self.request('/api/missing','alpha-operator-demo')[0],404)
    def test_database_not_exposed_as_static_file(self):self.assertEqual(self.request('/lab.db')[0],404)

if __name__=='__main__':unittest.main()
