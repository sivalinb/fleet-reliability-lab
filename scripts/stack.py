"""Run the loopback-only native integration profile, and stop its own children."""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--binaries',type=Path,default=ROOT/'work/bin')
    p.add_argument('--runtime',type=Path,default=ROOT/'work/runtime')
    p.add_argument('--grafana-home',type=Path)
    p.add_argument('--python',default=sys.executable,help='Python with optional Temporal SDK installed')
    args=p.parse_args()
    ports=[8790,39009,39095,37946,33100,39096,33200,39097,34317,34318,24318,23133,8888,39093,39090,37233,38233]
    if args.grafana_home:ports.append(33300)
    for port in ports:
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise SystemExit(f'Port {port} already has a listener. Stop the previous lab cleanly or choose a different profile before starting.')
    runtime=args.runtime.resolve();runtime.mkdir(parents=True,exist_ok=True)
    for name in ('mimir','loki','tempo','otel-queue','prometheus','alertmanager','grafana','logs'):(runtime/name).mkdir(exist_ok=True)
    env={**os.environ,'LAB_RUNTIME':str(runtime),'LAB_DB':str(runtime/'fleet.sqlite3'),'PYTHONPATH':str(ROOT),'OTEL_EXPORTER_OTLP_ENDPOINT':'http://127.0.0.1:24318'}
    def binary(name):
        path=args.binaries.resolve()/name/name
        if not path.is_file():raise SystemExit(f'Missing {path}. Run scripts/bootstrap_native.py first.')
        return str(path)
    commands=[
        ('mimir',[binary('mimir'),'-config.file='+str(ROOT/'ops/mimir.yaml'),'-config.expand-env=true','-query-frontend.instance-addr=127.0.0.1']),
        ('loki',[binary('loki'),'-config.file='+str(ROOT/'ops/loki.yaml'),'-config.expand-env=true']),
        ('tempo',[binary('tempo'),'-config.file='+str(ROOT/'ops/tempo.yaml'),'-config.expand-env=true']),
        ('collector',[binary('otelcol-contrib'),'--config='+str(ROOT/'ops/collector.yaml')]),
        ('alertmanager',[binary('alertmanager'),'--config.file='+str(ROOT/'ops/alertmanager.yaml'),'--web.listen-address=127.0.0.1:39093','--cluster.listen-address=','--storage.path='+str(runtime/'alertmanager')]),
        ('prometheus',[binary('prometheus'),'--config.file='+str(ROOT/'ops/prometheus.yaml'),'--web.listen-address=127.0.0.1:39090','--storage.tsdb.path='+str(runtime/'prometheus'),'--storage.tsdb.retention.time=2d']),
        ('temporal',[binary('temporal'),'server','start-dev','--ip','127.0.0.1','--port','37233','--ui-ip','127.0.0.1','--ui-port','38233','--db-filename',str(runtime/'temporal.sqlite3')]),
        ('api',[args.python,'-m','fleetlab.server','--db',str(runtime/'fleet.sqlite3')]),
    ]
    if args.grafana_home:
        home=args.grafana_home.resolve()
        env.update({'GF_PATHS_DATA':str(runtime/'grafana/data'),'GF_PATHS_LOGS':str(runtime/'grafana/logs'),'GF_PATHS_PLUGINS':str(runtime/'grafana/plugins'),'GF_PATHS_PROVISIONING':str(ROOT/'ops/grafana'),'LAB_DASHBOARD_PATH':str(ROOT/'ops/dashboards'),'GF_SERVER_HTTP_ADDR':'127.0.0.1','GF_SERVER_HTTP_PORT':'33300','GF_SECURITY_ADMIN_USER':'labadmin','GF_SECURITY_ADMIN_PASSWORD':'local-lab-only','GF_AUTH_ANONYMOUS_ENABLED':'false','GF_ANALYTICS_REPORTING_ENABLED':'false','GF_ANALYTICS_CHECK_FOR_UPDATES':'false','GF_USERS_ALLOW_SIGN_UP':'false'})
        commands.append(('grafana',[str(home/'bin/grafana'),'server','--homepath',str(home)]))
    children=[];logs=[]
    def stop(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        for name,cmd in commands:
            log=(runtime/'logs'/f'{name}.log').open('a');logs.append(log)
            child=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            children.append((name,child))
            print(json.dumps({'service':name,'pid':child.pid}),flush=True)
        (runtime/'processes.json').write_text(json.dumps({name:child.pid for name,child in children},indent=2))
        print('Story http://127.0.0.1:8790 | Grafana :33300 | Temporal :38233',flush=True)
        while True:
            for name,child in children:
                if child.poll() is not None: raise RuntimeError(f'{name} exited {child.returncode}; inspect {runtime}/logs/{name}.log')
            time.sleep(1)
    except KeyboardInterrupt: pass
    finally:
        for _,child in reversed(children):
            if child.poll() is None:child.terminate()
        for _,child in reversed(children):
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for log in logs:log.close()

if __name__=='__main__':main()
