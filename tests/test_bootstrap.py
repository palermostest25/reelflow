import base64
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import pytest
from cryptography.fernet import Fernet
from app.bootstrap import credentials


def clear_environment(monkeypatch):
    for key in ('APP_SECRET','TOKEN_KEY','ADMIN_PASSWORD'):
        monkeypatch.delenv(key,raising=False)


def test_generated_credentials_persist_with_private_permissions(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    folder=tmp_path/'fresh'
    first=credentials(folder,folder/'app.sqlite3')
    assert len(first['ADMIN_PASSWORD'])>=24 and len(first['APP_SECRET'])>=32
    cipher=Fernet(first['TOKEN_KEY'].encode())
    encrypted=cipher.encrypt(b'Instagram access token')
    second=credentials(folder,folder/'app.sqlite3')
    assert second==first
    assert Fernet(second['TOKEN_KEY'].encode()).decrypt(encrypted)==b'Instagram access token'
    assert stat.S_IMODE((folder/'.runtime.json').stat().st_mode)==0o600


def test_concurrent_containers_get_the_same_installation(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    script='''from pathlib import Path
from app.bootstrap import credentials
import hashlib,json,sys
folder=Path(sys.argv[1])
print(hashlib.sha256(json.dumps(credentials(folder,folder/'db'),sort_keys=True).encode()).hexdigest())
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(tmp_path/'shared')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
    results=[]
    for process in processes:
        out,err=process.communicate(timeout=30)
        assert process.returncode==0,err
        results.append(out.strip())
    assert len(set(results))==1


def test_existing_encrypted_tokens_never_get_a_replacement_key(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE secrets(name TEXT,value TEXT)')
        db.execute("INSERT INTO secrets VALUES('instagram_token','existing-encrypted-value')")
    with pytest.raises(RuntimeError,match='original TOKEN_KEY'):
        credentials(tmp_path,path)
    assert not (tmp_path/'.runtime.json').exists()


def test_legacy_explicit_key_is_migrated_and_reused(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    key=Fernet.generate_key().decode()
    token=Fernet(key.encode()).encrypt(b'old-token').decode()
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE secrets(name TEXT,value TEXT)')
        db.execute('INSERT INTO secrets VALUES(?,?)',('instagram_token',token))
    monkeypatch.setenv('TOKEN_KEY',key)
    first=credentials(tmp_path,path)
    monkeypatch.delenv('TOKEN_KEY')
    assert credentials(tmp_path,path)==first
    assert Fernet(first['TOKEN_KEY'].encode()).decrypt(token.encode())==b'old-token'


def test_replacing_installation_key_is_rejected(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    credentials(tmp_path,tmp_path/'db')
    monkeypatch.setenv('TOKEN_KEY',Fernet.generate_key().decode())
    with pytest.raises(RuntimeError,match='differs from this installation'):
        credentials(tmp_path,tmp_path/'db')


def test_corrupt_state_is_not_overwritten(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    target=tmp_path/'.runtime.json'
    target.write_text('corrupted')
    with pytest.raises(RuntimeError,match='Restore'):
        credentials(tmp_path,tmp_path/'db')
    assert target.read_text()=='corrupted'


def test_no_environment_files_required_for_real_application_login(tmp_path,monkeypatch):
    clear_environment(monkeypatch)
    env=dict(os.environ,DATA_DIR=str(tmp_path/'docker-data'),FOOTAGE_DIR=str(tmp_path/'footage'),PUBLIC_URL='http://testserver',DEMO_MODE='false')
    script='''from app.main import app
from app import config as c
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/api/state').status_code==401
    assert client.post('/api/login',json={'password':c.PASSWORD}).status_code==200
    assert client.get('/api/state').status_code==200
    assert client.get('/healthz').json()['status']=='ok'
'''
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'docker-data'/'.runtime.json').is_file()
