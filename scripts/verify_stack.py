"""Assert live backend delivery and a real Temporal worker restart.

Run after scripts/stack.py, using Python with requirements-temporal.txt installed.
Writes evidence/stack-verification.json; hardware inputs remain synthetic.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime,timezone
from pathlib import Path
from temporalio.client import Client
from fleetlab.engine import Engine
from fleetlab.probes import run as probes
from fleetlab.temporal_worker import RepairWorkflow,QUEUE
from fleetlab.telemetry import deliver

ROOT=Path(__file__).resolve().parents[1]

def http(url,body=None,token=None):
    headers={}
    if body is not None:headers['Content-Type']='application/json'
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers=headers)
    with urllib.request.urlopen(req,timeout=5) as response:
        raw=response.read()
        try:return json.loads(raw)
        except ValueError:return raw.decode()

def wait(predicate,description,timeout=90):
    until=time.monotonic()+timeout;last=None
    while time.monotonic()<until:
        try:
            result=predicate()
            if result:return result
        except Exception as error:last=str(error)
        time.sleep(1)
    raise AssertionError(f'{description} timed out: {last}')

def api(path,body=None):return http('http://127.0.0.1:8790/api/'+path,body,'alpha-operator-demo')

async def main(args):
    runtime=args.runtime.resolve()
    results={'recorded_at':datetime.now(timezone.utc).isoformat(),'platform':platform.system()+'/'+platform.machine(),'hardware_provenance':'synthetic GPU/fabric/storage/thermal signals; real software services and control-plane execution','checks':{}}
    for name,url in [('api','http://127.0.0.1:8790/api/health'),('mimir','http://127.0.0.1:39009/ready'),('loki','http://127.0.0.1:33100/ready'),('tempo','http://127.0.0.1:33200/ready'),('collector','http://127.0.0.1:23133'),('grafana','http://127.0.0.1:33300/api/health')]:
        wait(lambda:http(url),name+' readiness')
        results['checks'][name+'_ready']=True
    snap=api('scenarios',{'scenario':'optic'})
    inc=next(i for i in reversed(snap['incidents']) if i['status']=='open')
    iid=inc['id'];asset=inc['asset_id']
    client=await Client.connect('127.0.0.1:37233')
    handle=await client.start_workflow(RepairWorkflow.run,{'tenant':'alpha','incident_id':iid},id='repair-'+iid,task_queue=QUEUE)
    env={**os.environ,'LAB_DB':str(runtime/'fleet.sqlite3'),'PYTHONPATH':str(ROOT)}
    worker_log=(runtime/'logs/worker.log').open('a')
    def start_worker():return subprocess.Popen([sys.executable,'-m','fleetlab.temporal_worker','worker'],cwd=ROOT,env=env,stdout=worker_log,stderr=subprocess.STDOUT)
    worker=start_worker()
    try:
        def incident():return next(i for i in api('fleet')['incidents'] if i['id']==iid)
        wait(lambda:incident()['stage']=='wait_repair','Temporal advances to physical repair')
        # Leave enough time for real Prometheus -> Alertmanager -> authenticated audit.
        wait(lambda:any(e['kind']=='alert.received' and e['asset_id']==asset for e in api('fleet')['events']),'real alert route',45)
        worker.terminate();worker.wait(timeout=10)
        desc=await handle.describe()
        results['checks']['temporal_survives_worker_stop']=desc.status.name=='RUNNING'
        ticket=next(t for t in api('fleet')['tickets'] if t['incident_id']==iid)
        api('tickets/'+ticket['id']+'/complete',{'success':True})
        results['checks']['ticket_completion_does_not_resolve']=incident()['status']=='open'
        worker=start_worker()
        outcome=await asyncio.wait_for(handle.result(),timeout=60)
        snap=api('fleet')
        tickets=[t for t in snap['tickets'] if t['incident_id']==iid]
        assert outcome['status']=='resolved' and len(tickets)==1
        history=await handle.fetch_history()
        results['checks'].update({'temporal_worker_restart_verified':True,'one_ticket_after_restart':len(tickets)==1,'verified_recovery_after_ticket':incident()['status']=='resolved'})
        results['temporal']={'workflow_id':handle.id,'status':outcome['status'],'history_events':len(history.events),'ticket_rows':len(tickets),'workflow_engine':'real Temporal development server; actual worker terminated and restarted'}
    finally:
        if worker.poll() is None:worker.terminate();worker.wait(timeout=10)
        worker_log.close()
    engine=Engine(runtime/'fleet.sqlite3')
    # Exercise app outbox retry through a genuinely unreachable endpoint, then replay.
    with engine.tx() as db:engine.event(db,'alpha','verification.delivery_probe',asset,iid,{'provenance':'real collector retry test'})
    failed=deliver(engine,'http://127.0.0.1:1')
    recovered=deliver(engine,'http://127.0.0.1:24318')
    results['checks']['outbox_retains_unreachable_delivery']=failed['failed_channels']>0
    results['checks']['outbox_replays_to_real_collector']=recovered['delivered_channels']>0
    query=urllib.parse.urlencode({'query':'sum(fleet_asset_state{tenant="alpha"})'})
    metric=wait(lambda:(lambda d:d if d.get('data',{}).get('result') else None)(http('http://127.0.0.1:39009/prometheus/api/v1/query?'+query)),'Mimir remote write query')
    results['checks']['prometheus_remote_write_mimir_query']=True
    results['mimir']={'query':'sum(fleet_asset_state{tenant="alpha"})','result':metric['data']['result']}
    logquery=urllib.parse.urlencode({'query':'{service_name="fleet-reliability-lab"} |= "'+iid+'"','limit':'100'})
    # Incident IDs are OTLP attributes (structured metadata), not the log body.
    logquery=urllib.parse.urlencode({'query':'{service_name="fleet-reliability-lab"} | incident_id="'+iid+'"','limit':'100'})
    logs=wait(lambda:(lambda d:d if d.get('data',{}).get('result') else None)(http('http://127.0.0.1:33100/loki/api/v1/query_range?'+logquery)),'Loki incident query')
    results['checks']['collector_to_loki_query']=True
    results['loki']={'query_result_groups':len(logs['data']['result']),'records':sum(len(s['values']) for s in logs['data']['result']),'incident_id':iid}
    trace_id=hashlib.sha256(iid.encode()).hexdigest()[:32]
    trace=wait(lambda:http('http://127.0.0.1:33200/api/traces/'+trace_id),'Tempo trace lookup')
    span_count=sum(len(s.get('spans',[])) for b in trace.get('batches',trace.get('resourceSpans',[])) for s in b.get('scopeSpans',b.get('instrumentationLibrarySpans',[])))
    assert span_count>0
    results['checks']['collector_to_tempo_trace_lookup']=True
    results['tempo']={'trace_id':trace_id,'span_count':span_count,'span_provenance':'event-derived control-plane transitions, not GPU execution timings'}
    auth=base64.b64encode(b'labadmin:local-lab-only').decode()
    req=urllib.request.Request('http://127.0.0.1:33300/api/dashboards/uid/fleet-reliability',headers={'Authorization':'Basic '+auth})
    with urllib.request.urlopen(req,timeout=5) as r:dashboard=json.load(r)
    results['checks']['grafana_dashboard_provisioned']=len(dashboard['dashboard']['panels'])==9
    results['local_probes']=probes()
    assert all(results['checks'].values()),results
    target=ROOT/'evidence/stack-verification.json';target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,default=ROOT/'work/runtime')
    asyncio.run(main(p.parse_args()))
