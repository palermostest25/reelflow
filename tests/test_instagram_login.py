import time
from urllib.parse import parse_qs
import httpx
import pytest
from app import config as c, instagram as ig
from app.db import connect, secret


@pytest.mark.parametrize('wrapped', [False, True])
def test_login_exchanges_flat_and_documented_wrapped_tokens(monkeypatch, wrapped):
    seen=[]
    def handler(request):
        seen.append(request)
        if request.url.host=='api.instagram.com':
            form=parse_qs(request.content.decode())
            assert request.method=='POST'
            assert form['client_id']==['instagram-app-id']
            assert form['client_secret']==['instagram-app-secret']
            assert form['redirect_uri']==[c.PUBLIC_URL+'/auth/instagram/callback']
            assert form['code']==['one-use-code']
            record={'access_token':'short-test-token','user_id':'123456','permissions':'instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights'}
            return httpx.Response(200,json={'data':[record]} if wrapped else record)
        if request.url.path=='/access_token':
            assert request.url.params['access_token']=='short-test-token'
            assert request.url.params['grant_type']=='ig_exchange_token'
            return httpx.Response(200,json={'access_token':'long-test-token','expires_in':5184000})
        assert request.url.path=='/'+c.API_VERSION+'/me'
        assert request.headers['Authorization']=='Bearer long-test-token'
        # The scoped account ID remains available even when the profile omits it.
        return httpx.Response(200,json={'username':'trades'})
    original=httpx.Client
    monkeypatch.setattr(ig.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    monkeypatch.setattr(c,'META_ID','instagram-app-id')
    monkeypatch.setattr(c,'META_SECRET','instagram-app-secret')
    before=time.time()
    ig.connect_account('one-use-code')
    assert len(seen)==3
    assert secret('instagram_token')=='long-test-token'
    with connect() as db:
        account=db.execute('SELECT * FROM account WHERE id=1').fetchone()
        assert account['ig_id']=='123456' and account['username']=='trades'
        assert account['expires_at']>=before+5184000


@pytest.mark.parametrize('payload', [
    {'data':[]}, {'data':[{},{}]}, {'data':{}},
    {'data':[{'access_token':'','user_id':'123456'}]},
    {'data':[{'access_token':'short-test-token'}]},
])
def test_invalid_login_response_never_saves_or_exchanges_credentials(monkeypatch,payload):
    def handler(request):
        assert request.url.host=='api.instagram.com'
        return httpx.Response(200,json=payload)
    original=httpx.Client
    monkeypatch.setattr(ig.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(ig.MetaError,match='invalid token response'):
        ig.connect_account('one-use-code')
    assert not secret('instagram_token')
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM account').fetchone()[0]==0
