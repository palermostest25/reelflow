"""CI smoke test: readiness, both processes, authenticated login and stable credentials."""
import hashlib
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config as c

c.validate_runtime()
base='http://127.0.0.1:8080'
for attempt in range(90):
    try:
        with urllib.request.urlopen(base+'/healthz',timeout=2) as response:
            ready=json.load(response)
        if ready.get('status')=='ok' and ready.get('worker_alive'):
            break
    except (urllib.error.URLError,ConnectionError,TimeoutError):
        pass
    time.sleep(.5)
else:
    raise SystemExit('The web UI and posting worker did not become ready.')
assert ready['version']==c.VERSION and ready['revision']==c.REVISION
with urllib.request.urlopen(base+'/',timeout=5) as response:
    page=response.read().decode()
    assert response.headers['Cache-Control']=='no-store'
assert f'Reelflow {c.VERSION} · {c.REVISION[:7]}' in page
assets=re.findall(r'/static/build/([a-f0-9]{12})/(app\.js|style\.css)',page)
assert {name for _,name in assets}=={'app.js','style.css'}
for digest,name in assets:
    with urllib.request.urlopen(f'{base}/static/build/{digest}/{name}',timeout=5) as response:
        assert hashlib.sha256(response.read()).hexdigest().startswith(digest)
state=json.loads((c.DATA/'.runtime.json').read_text())
fingerprint=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
request=urllib.request.Request(base+'/api/login',data=json.dumps({'password':c.PASSWORD}).encode(),headers={'Content-Type':'application/json'})
with client.open(request,timeout=10) as response:
    assert response.status==200
with client.open(base+'/api/state',timeout=10) as response:
    assert response.status==200 and json.load(response)['csrf']
print(fingerprint)
