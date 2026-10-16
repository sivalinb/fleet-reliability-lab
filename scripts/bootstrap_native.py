"""Download pinned official releases; validate SHA-256 before extracting binaries.

No root install. Everything stays under ignored work/bin/.
"""
import concurrent.futures,hashlib,json,os,stat,tarfile,urllib.request,zipfile,platform
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'work/bin'
SYSTEM=platform.system().lower()
ARCH={'arm64':'arm64','aarch64':'arm64','x86_64':'amd64','amd64':'amd64'}.get(platform.machine().lower())
if SYSTEM not in ('darwin','linux') or not ARCH:raise SystemExit('Native profile supports macOS/Linux arm64/amd64')
ROOT.mkdir(parents=True,exist_ok=True)
ITEMS=[('mimir','grafana/mimir','mimir-3.2.2','mimir-darwin-arm64'),('loki','grafana/loki','v3.7.8','loki-darwin-arm64.zip'),('tempo','grafana/tempo','v2.7.2','tempo_2.7.2_darwin_arm64.tar.gz'),('prometheus','prometheus/prometheus','v3.15.0','prometheus-3.15.0.darwin-arm64.tar.gz'),('alertmanager','prometheus/alertmanager','v0.34.1','alertmanager-0.34.1.darwin-arm64.tar.gz'),('otelcol-contrib','open-telemetry/opentelemetry-collector-releases','v0.162.0','otelcol-contrib_0.162.0_darwin_arm64.tar.gz'),('temporal','temporalio/cli','v1.9.1','temporal_cli_1.9.1_darwin_arm64.tar.gz')]
def fetch(item):
 name,repo,tag,asset=item
 asset=asset.replace('darwin-arm64',SYSTEM+'-'+ARCH).replace('darwin_arm64',SYSTEM+'_'+ARCH)
 out=ROOT/name;out.mkdir(exist_ok=True)
 binary=out/name
 if binary.exists():return {'name':name,'path':str(binary.relative_to(ROOT)),'cached':True}
 meta=json.load(urllib.request.urlopen(f'https://api.github.com/repos/{repo}/releases/tags/{tag}',timeout=30))
 entry=next(a for a in meta['assets'] if a['name']==asset)
 target=out/asset
 if not target.exists():
  target.write_bytes(urllib.request.urlopen(entry['browser_download_url'],timeout=180).read())
 digest=hashlib.sha256(target.read_bytes()).hexdigest()
 checksum_assets=[a for a in meta['assets'] if a['name'] in ('SHA256SUMS','sha256sums.txt','checksums.txt',asset+'-sha-256',asset+'.sha256') or ('checksums' in a['name'].lower() and a['name'].endswith('.txt'))]
 verified=False
 for cs in checksum_assets:
  try:
   text=urllib.request.urlopen(cs['browser_download_url'],timeout=30).read().decode()
   if digest in text:verified=True;break
  except Exception:pass
 if not verified:
  raise RuntimeError(f'Checksum verification unavailable for {name}; candidates={[a["name"] for a in checksum_assets]}')
 if asset.endswith('.zip'):
  with zipfile.ZipFile(target) as z:
   member=next(n for n in z.namelist() if n.rstrip('/').split('/')[-1] in (name,'loki-'+SYSTEM+'-'+ARCH))
   binary.write_bytes(z.read(member))
 elif asset.endswith('.tar.gz'):
  with tarfile.open(target) as t:
   member=next(m for m in t.getmembers() if m.isfile() and m.name.split('/')[-1]==name)
   binary.write_bytes(t.extractfile(member).read())
 else:
  if target!=binary:binary.write_bytes(target.read_bytes())
 binary.chmod(binary.stat().st_mode|stat.S_IXUSR)
 return {'name':name,'path':str(binary.relative_to(ROOT)),'tag':tag,'sha256':digest,'checksum_verified':True}
def main():
 results=[];failures=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  jobs={pool.submit(fetch,item):item[0] for item in ITEMS}
  for f in concurrent.futures.as_completed(jobs):
   try:r=f.result();results.append(r);print(json.dumps(r),flush=True)
   except Exception as e:failures.append(jobs[f]);print(json.dumps({'name':jobs[f],'error':str(e)}),flush=True)
 (ROOT/'download-manifest.json').write_text(json.dumps(results,indent=2))
 if failures:raise SystemExit('Downloads failed: '+', '.join(failures))
if __name__=='__main__':main()
