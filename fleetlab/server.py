"""Local synthetic lab API and static story, using Python's standard library."""
import argparse
import hmac
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import Engine, LabError

DEFAULT_TOKENS = {"alpha-operator-demo":("alpha","operator"),"alpha-viewer-demo":("alpha","viewer"),"bravo-operator-demo":("bravo","operator"),"metrics-admin-demo":("*","metrics")}

def token_map():
    value = os.getenv("LAB_TOKENS")
    if not value:
        return DEFAULT_TOKENS
    result = {}
    for entry in value.split(','):
        token,tenant,role = entry.split(':')
        if role not in ("operator","viewer","metrics") or not token or tenant not in ("alpha","bravo","*"):
            raise ValueError("LAB_TOKENS must contain token:tenant:role entries")
        if tenant == "*" and role != "metrics":
            raise ValueError("Wildcard tenancy is only for the internal metrics scraper")
        result[token] = (tenant,role)
    return result

def make_handler(engine, web_root, tokens=None):
    tokens = token_map() if tokens is None else tokens
    allowed = set(os.getenv("LAB_ALLOWED_ORIGINS","http://127.0.0.1:8790,http://localhost:8790,https://sivalinb.github.io").split(','))
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,directory=str(web_root),**kwargs)

        def log_message(self,format,*args):
            # Never log bearer tokens or request bodies.
            print(json.dumps({"service":"fleetlab-http","message":format%args,"time":time.time()}),flush=True)

        def end_headers(self):
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Cache-Control","no-store" if self.path.startswith('/api') else "no-cache")
            origin = self.headers.get("Origin")
            if origin in allowed:
                self.send_header("Access-Control-Allow-Origin",origin)
                self.send_header("Vary","Origin")
            super().end_headers()

        def reply(self,payload,status=200,content_type="application/json"):
            raw = payload.encode() if isinstance(payload,str) else json.dumps(payload,allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def principal(self,write=False,metrics=False):
            auth = self.headers.get("Authorization","")
            if not auth.startswith("Bearer "):
                raise LabError("Bearer token required",401)
            supplied = auth[7:]
            identity = next((v for k,v in tokens.items() if hmac.compare_digest(k,supplied)),None)
            if identity is None:
                raise LabError("Invalid bearer token",401)
            tenant,role = identity
            if metrics and role != "metrics":
                raise LabError("Internal metrics scope required",403)
            if not metrics and role == "metrics":
                raise LabError("Scraper token cannot access tenant APIs",403)
            if write and role != "operator":
                raise LabError("Operator role required",403)
            return tenant

        def do_OPTIONS(self):
            if self.headers.get("Origin") not in allowed:
                return self.reply({"error":"Origin not allowed"},403)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers","Authorization, Content-Type")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/health":
                    return self.reply({"status":"ok","mode":"synthetic-hardware-real-control-plane","version":"1.0.0"})
                if path == "/metrics":
                    self.principal(metrics=True)
                    return self.reply(engine.metrics(),content_type="text/plain; version=0.0.4")
                if path == "/api/fleet":
                    return self.reply(engine.snapshot(self.principal()))
                if path.startswith('/api/'):
                    return self.reply({"error":"Not found"},404)
                # This serves web/ only; the database, resume and private guide are outside it.
                if path.endswith('/'):
                    candidate = Path(self.translate_path(self.path))
                    if not (candidate/'index.html').is_file():
                        return self.reply({"error":"Directory listing disabled"},404)
                return super().do_GET()
            except LabError as error:
                self.reply({"error":str(error)},error.status)

        def do_POST(self):
            try:
                tenant = self.principal(write=True)
                origin = self.headers.get("Origin")
                if origin and origin not in allowed:
                    raise LabError("Origin not allowed",403)
                length = int(self.headers.get("Content-Length",0))
                if length <= 0 or length > 65536:
                    raise LabError("JSON body must be 1..65536 bytes",413)
                body = json.loads(self.rfile.read(length),parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
                if not isinstance(body,dict):
                    raise LabError("JSON object required",400)
                path = urlparse(self.path).path.strip('/').split('/')
                if path == ["api","scenarios"]:
                    result = engine.scenario(tenant,body.get("scenario"))
                elif path == ["api","alerts"]:
                    return self.reply(engine.receive_alerts(tenant,body))
                elif len(path)==4 and path[:2]==["api","assets"]:
                    action = path[3]
                    if action == "commission":
                        result = engine.commission(tenant,path[2],body.get("fail_test"))
                    elif action == "faults":
                        result = engine.inject(tenant,path[2],body.get("kind"))
                    elif action == "observe":
                        result = engine.observe(tenant,path[2],fresh=body.get("fresh",True),healthy=body.get("healthy",False))
                    elif action == "retire":
                        result = engine.retire(tenant,path[2])
                    else:
                        raise LabError("Not found",404)
                elif path == ["api","allocate"]:
                    result = engine.allocate(tenant,body.get("assets"))
                elif len(path)==4 and path[:2]==["api","incidents"] and path[3]=="advance":
                    version = body.get("expected_version")
                    if type(version) is not int or version < 0:
                        raise LabError("expected_version integer is required for compare-and-swap",400)
                    result = engine.advance(tenant,path[2],expected_version=version)
                elif len(path)==4 and path[:2]==["api","tickets"] and path[3]=="complete":
                    if type(body.get("success",True)) is not bool:
                        raise LabError("success must be boolean",400)
                    result = engine.complete_ticket(tenant,path[2],body.get("success",True))
                elif len(path)==4 and path[:2]==["api","workloads"] and path[3]=="release":
                    result = engine.release(tenant,path[2])
                else:
                    raise LabError("Not found",404)
                if "assets" not in result:
                    result = engine.snapshot(tenant)
                self.reply(result)
            except LabError as error:
                self.reply({"error":str(error)},error.status)
            except (ValueError,TypeError,KeyError):
                self.reply({"error":"Invalid request body"},400)
            except Exception as error:
                self.log_message("Request failed: %s",type(error).__name__)
                self.reply({"error":"Internal error; see server logs"},500)
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8790)
    parser.add_argument('--db',default='work/fleetlab.sqlite3')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1','localhost','::1') and os.getenv('LAB_ALLOW_NETWORK_BIND') != '1':
        raise SystemExit('Network bind requires LAB_ALLOW_NETWORK_BIND=1; this is a synthetic lab, not a production service.')
    engine = Engine(args.db)
    engine.seed()
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
    if endpoint:
        from .telemetry import deliver
        def export_loop():
            while True:
                deliver(engine,endpoint)
                time.sleep(2)
        threading.Thread(target=export_loop,daemon=True).start()
    root = Path(__file__).resolve().parent.parent/'web'
    server = ThreadingHTTPServer((args.host,args.port),make_handler(engine,root))
    print(f'Fleet Reliability Lab: http://{args.host}:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
