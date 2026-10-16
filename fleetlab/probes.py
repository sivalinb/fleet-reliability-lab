"""Real CPU and scratch-file probes, never presented as GPU benchmarks."""
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

def run(samples=20):
    payload=b'fleet-lab-scratch-probe\n'*32768
    expected=hashlib.sha256(payload).hexdigest()
    cpu=[];io=[]
    with tempfile.TemporaryDirectory(prefix='fleetlab-probe-') as tmp:
        path=Path(tmp)/'scratch.bin'
        for _ in range(samples):
            start=time.perf_counter()
            result=hashlib.pbkdf2_hmac('sha256',b'public synthetic workload',b'fleetlab',10000)
            cpu.append((time.perf_counter()-start)*1000)
            if len(result)!=32:raise RuntimeError('CPU result failed validation')
            start=time.perf_counter();path.write_bytes(payload);read=path.read_bytes()
            io.append((time.perf_counter()-start)*1000)
            if hashlib.sha256(read).hexdigest()!=expected:raise RuntimeError('Scratch data verification failed')
    return {'origin':'measured local CPU and temporary filesystem; no GPU or RDMA benchmark','samples':samples,
            'cpu_digest_step_ms':{'median':statistics.median(cpu),'max':max(cpu)},
            'scratch_roundtrip_ms':{'median':statistics.median(io),'max':max(io)},
            'scratch_bytes':len(payload),'checksum_verified':True,'physical_gpu_test':'skipped'}

if __name__=='__main__':print(json.dumps(run(),indent=2))
