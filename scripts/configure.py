"""First-run .env generator. Uses only the Python standard library."""
import base64
import getpass
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent.parent
if (root/'.env').exists():
    raise SystemExit('.env already exists. Edit it in place; do not rotate encryption keys without migrating stored tokens.')
print('Reelflow first-run setup. Passwords and keys will be saved in a local .env file.')
url = input('Public HTTPS URL (leave blank for localhost dry run): ').strip() or 'http://localhost:8080'
footage = input('Footage folder [./footage]: ').strip() or './footage'
password = getpass.getpass('Admin password (12+ characters, or blank to generate): ')
if not password:
    password = secrets.token_urlsafe(20)
    print('Generated admin password:',password)
if len(password)<12:
    raise SystemExit('Use at least 12 characters.')
if any(x in password for x in "\n\r'$") or any(x in footage+url for x in "\n\r'$"):
    raise SystemExit('Avoid newlines, single quotes and dollar signs in these setup values.')
secret = secrets.token_urlsafe(48)
token_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
uid = os.getuid() if hasattr(os,'getuid') else 1000
gid = os.getgid() if hasattr(os,'getgid') else 1000
values = {'APP_SECRET':secret,'TOKEN_KEY':token_key,'ADMIN_PASSWORD':password,'PUBLIC_URL':url,
          'META_APP_ID':'','META_APP_SECRET':'','META_API_VERSION':'v25.0',
          'AI_BASE_URL':'https://openrouter.ai/api/v1','AI_API_KEY':'','AI_MODEL':'',
          'FOOTAGE_PATH':footage,'DATA_PATH':'./data','PUID':str(uid),'PGID':str(gid),
          'LISTEN_HOST':'127.0.0.1','PORT':'8080','DEMO_MODE':'false'}
fd = os.open(root/'.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream:
    stream.write('\n'.join(k+"='"+v+"'" for k,v in values.items())+'\n')
(root/'data').mkdir(exist_ok=True)
Path(footage if Path(footage).is_absolute() else root/footage).mkdir(parents=True,exist_ok=True)
print('Saved .env. Add your Meta app credentials, then run: docker compose up -d --build')
