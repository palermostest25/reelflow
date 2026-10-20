import time
from app import notifications as n
from app.db import connect
from conftest import set_settings

def test_outbox_retries_without_dropping_notice(monkeypatch):
    set_settings(ntfy_topic='test-only-topic')
    n.enqueue('one','Test message')
    n.enqueue('one','Duplicate message')
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):raise TimeoutError()
    monkeypatch.setattr(n.httpx,'Client',Client)
    n.flush()
    with connect() as db:
        row=db.execute('SELECT * FROM notices').fetchone()
        assert row['status']=='pending' and row['attempts']==1
        assert db.execute('SELECT COUNT(*) FROM notices').fetchone()[0]==1
    class OK:
        def raise_for_status(self):pass
    monkeypatch.setattr(Client,'post',lambda *a,**kw:OK())
    with connect(True) as db:db.execute('UPDATE notices SET last_attempt=0')
    n.flush()
    with connect() as db:assert db.execute('SELECT status FROM notices').fetchone()[0]=='sent'
