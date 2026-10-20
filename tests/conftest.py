import os
import base64
import hashlib
import json
import time
os.environ.setdefault('APP_SECRET','test-only-session-secret-with-more-than-32-characters')
os.environ.setdefault('TOKEN_KEY',base64.urlsafe_b64encode(b'0'*32).decode())
os.environ.setdefault('ADMIN_PASSWORD','test-password-12345')
from pathlib import Path
import pytest
from app import config as c
from app.db import init,connect,settings

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(c,'DATA',tmp_path/'data')
    monkeypatch.setattr(c,'FOOTAGE',tmp_path/'footage')
    monkeypatch.setattr(c,'DB',tmp_path/'data'/'reelflow.sqlite3')
    monkeypatch.setattr(c,'DEMO',False)
    monkeypatch.setattr(c,'PUBLIC_URL','https://reelflow.test')
    monkeypatch.setattr(c,'AI_KEY','')
    init()

def seed(count=12,status='ready',prefix='clip'):
    ids=[]
    with connect(True) as db:
        for i in range(count):
            cid=hashlib.sha256((prefix+str(i)).encode()).hexdigest()
            db.execute('INSERT INTO clips(id,name,source,created_at,status,caption,category) VALUES(?,?,?,?,?,?,?)',(cid,f'{prefix}-{i}.mp4','/test/source',time.time(),status,'A caption for this real footage.',str(i%3)))
            ids.append(cid)
    return ids

def set_settings(**kwargs):
    with connect(True) as db:
        s=settings(db)
        s.update(kwargs)
        db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(s),))

def connected():
    from app.db import secret
    with connect(True) as db:
        db.execute("INSERT INTO account(id,ig_id,username,expires_at) VALUES(1,'123456','trades',?)",(time.time()+30*86400,))
    secret('instagram_token','test-only-token')
