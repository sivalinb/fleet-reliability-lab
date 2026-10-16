"""Optional real Temporal adapter. All hardware observations remain simulated.

Workflow code only orchestrates activities and durable timers. Database/clock/IO
stay in activities. The transactional engine is the authority for effect dedupe.
"""
import argparse
import asyncio
import os
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker
with workflow.unsafe.imports_passed_through():
    from fleetlab.engine import Engine, LabError

QUEUE = 'fleet-repair-v1'

@activity.defn
async def repair_step(request):
    e = Engine(os.environ.get('LAB_DB','work/fleetlab.sqlite3'))
    tenant,iid,expected = request['tenant'],request['incident_id'],request.get('expected_version')
    def current():
        s=e.snapshot(tenant)
        inc=next(i for i in s['incidents'] if i['id']==iid)
        ticket=next((t for t in s['tickets'] if t['incident_id']==iid),None)
        return inc,ticket
    inc,ticket=current()
    if inc['status'] in ('resolved','escalated','retired'):
        return {'done':True,'status':inc['status'],'version':inc['version']}
    if expected is not None and inc['version'] != expected:
        # Retry after commit returns the durable version; never executes the next effect.
        return {'done':False,'version':inc['version'],'recovered_commit':True}
    if inc['stage']=='wait_repair' and (not ticket or ticket['status']!='completed'):
        return {'done':False,'version':inc['version'],'waiting_for_technician':True}
    # This adapter polls the synthetic source explicitly. A real adapter must read
    # independently authenticated hardware evidence rather than invent freshness.
    e.observe(tenant,inc['asset_id'])
    try: e.advance(tenant,iid,expected_version=inc['version'])
    except LabError as error:
        return {'done':False,'version':inc['version'],'blocked':str(error)}
    inc,_=current()
    return {'done':inc['status'] in ('resolved','escalated','retired'),'status':inc['status'],'version':inc['version']}

@workflow.defn
class RepairWorkflow:
    @workflow.run
    async def run(self,request):
        version=request.get('expected_version')
        for _ in range(1800):  # bounded one-hour lab workflow
            result=await workflow.execute_activity(repair_step,{**request,'expected_version':version},
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1),maximum_attempts=3))
            version=result['version']
            if result['done']: return result
            await workflow.sleep(timedelta(seconds=2))
        return {'done':False,'status':'lab_timeout_requires_operator','version':version}

async def run(args):
    client=await Client.connect(args.address)
    if args.command=='start':
        h=await client.start_workflow(RepairWorkflow.run,{'tenant':args.tenant,'incident_id':args.incident},id='repair-'+args.incident,task_queue=QUEUE)
        print(h.id,flush=True)
    else:
        async with Worker(client,task_queue=QUEUE,workflows=[RepairWorkflow],activities=[repair_step]):
            print('Temporal repair worker ready',flush=True)
            await asyncio.Event().wait()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['worker','start'])
    p.add_argument('--address',default='127.0.0.1:37233')
    p.add_argument('--tenant',default='alpha')
    p.add_argument('--incident')
    args=p.parse_args()
    if args.command=='start' and not args.incident:p.error('--incident required')
    asyncio.run(run(args))

if __name__=='__main__':main()
