import json
import time
import httpx
import pytest
from app import ai, config as c
from app.db import connect, settings, runtime
from conftest import seed, set_settings

MODEL = {
    'id': 'meta/muse-spark-1.3-contributor',
    'supported_parameters': ['max_tokens','reasoning','response_format','structured_outputs'],
    'reasoning': {'mandatory': True, 'supported_efforts': ['high','medium','low','minimal']},
    'top_provider': {'max_completion_tokens': 943718},
}


@pytest.fixture(autouse=True)
def no_cached_catalog():
    ai._openrouter_catalog.cache_clear()
    yield
    ai._openrouter_catalog.cache_clear()


def clip_and_settings(monkeypatch):
    monkeypatch.setattr(c,'AI_KEY','test-only-key')
    set_settings(ai_model=MODEL['id'])
    cid=seed(1,status='captioning')[0]
    with connect(True) as db:
        db.execute("UPDATE clips SET caption='',category='' WHERE id=?",(cid,))
    with connect() as db: clip=dict(db.execute('SELECT * FROM clips WHERE id=?',(cid,)).fetchone())
    return clip,settings()


def install_transport(monkeypatch,handler):
    original=httpx.Client
    monkeypatch.setattr(ai.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


def completion(caption='Keep the next one rolling.',category='DJ performance',**kwargs):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'caption':caption,'category':category})},**kwargs}]}


def test_reasoning_model_completes_caption_instead_of_using_up_400_token_limit(monkeypatch):
    clip,s=clip_and_settings(monkeypatch)
    seen=[]
    def provider(request):
        if request.method=='GET':return httpx.Response(200,json={'data':[MODEL]})
        body=json.loads(request.content);seen.append(body)
        if body['max_tokens']<=400:
            return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':None}}], 'usage':{'completion_tokens':422,'completion_tokens_details':{'reasoning_tokens':422}}})
        assert body['reasoning']=={'effort':'minimal','exclude':True}
        assert body['response_format']['json_schema']['strict'] is True
        assert set(body['response_format']['json_schema']['schema']['required'])=={'caption','category'}
        assert body['provider']['require_parameters'] is True
        return httpx.Response(200,json=completion())
    install_transport(monkeypatch,provider)
    ai.caption_pending()
    with connect() as db:
        saved=db.execute('SELECT * FROM clips WHERE id=?',(clip['id'],)).fetchone()
        assert saved['status']=='ready' and saved['caption']=='Keep the next one rolling.'
        assert not saved['error']
    assert len(seen)==1 and seen[0]['max_tokens']==4096


def test_vision_frames_are_retained_when_requesting_structured_output(monkeypatch):
    clip,s=clip_and_settings(monkeypatch)
    for i in range(3):(c.DATA/'frames'/(clip['id']+'-'+str(i)+'.jpg')).write_bytes(b'test-only-frame')
    monkeypatch.setattr(ai,'model_catalog',lambda:[MODEL])
    def provider(request):
        content=json.loads(request.content)['messages'][1]['content']
        assert [p['type'] for p in content]==['text','image_url','image_url','image_url']
        assert all(p['image_url']['url'].startswith('data:image/jpeg;base64,') for p in content[1:])
        return httpx.Response(200,json=completion())
    install_transport(monkeypatch,provider)
    assert ai.generate(clip,s)[0]=='Keep the next one rolling.'


@pytest.mark.parametrize('content',[None,'',json.dumps({'caption':'Complete looking text.','category':'DJ performance'})])
def test_length_completion_is_held_even_if_partial_content_looks_valid(content):
    with pytest.raises(ai.CaptionError,match='output limit'):
        ai.parse_caption({'choices':[{'finish_reason':'length','message':{'content':content}}]})


def test_reasoning_text_is_never_used_as_a_public_caption():
    with pytest.raises(ai.CaptionError,match='no caption text') as caught:
        ai.parse_caption({'choices':[{'finish_reason':'stop','message':{'content':None,'reasoning':'private-reasoning-sentinel'}}]})
    assert 'private-reasoning-sentinel' not in str(caught.value)


@pytest.mark.parametrize('status,fragment',[(401,'API key'),(402,'credit'),(403,'blocked'),(429,'rate limiting'),(503,'available')])
def test_provider_failures_are_actionable_and_never_expose_raw_responses(monkeypatch,status,fragment):
    clip,s=clip_and_settings(monkeypatch)
    monkeypatch.setattr(ai,'model_catalog',lambda:[MODEL])
    install_transport(monkeypatch,lambda request:httpx.Response(status,json={'error':{'message':'private-provider-response test-only-key'}}))
    with pytest.raises(ai.CaptionError,match=fragment) as caught:ai.generate(clip,s)
    assert 'private-provider-response' not in str(caught.value) and 'test-only-key' not in str(caught.value)


def test_http_200_provider_error_is_not_mistaken_for_a_completion():
    with pytest.raises(ai.CaptionError,match='rate limiting'):
        ai.parse_caption({'error':{'code':429,'message':'sensitive response'}})


def test_text_parts_and_fenced_json_are_supported():
    data=completion()
    data['choices'][0]['message']['content']=[{'type':'text','text':'```json\n'+json.dumps({'caption':'A real caption.','category':'DJ performance'})+'\n```'},{'type':'reasoning','text':'never publish this'}]
    assert ai.parse_caption(data)==('A real caption.','DJ performance')


@pytest.mark.parametrize('payload',[{'caption':123,'category':'DJ performance'},{'caption':'a'*401,'category':'DJ performance'},{'caption':'','category':'DJ performance'},{'caption':'A real caption.','category':''}])
def test_invalid_caption_fields_never_release_a_clip(payload):
    with pytest.raises(ai.CaptionError):
        ai.parse_caption({'choices':[{'message':{'content':json.dumps(payload)}}]})


def test_custom_provider_receives_no_openrouter_only_parameters(monkeypatch):
    clip,s=clip_and_settings(monkeypatch)
    monkeypatch.setattr(c,'AI_BASE','https://provider.test/v1')
    monkeypatch.setattr(ai,'model_catalog',lambda:pytest.fail('Custom providers must not fetch OpenRouter model metadata'))
    def provider(request):
        body=json.loads(request.content)
        assert 'provider' not in body and 'reasoning' not in body and 'response_format' not in body
        return httpx.Response(200,json=completion())
    install_transport(monkeypatch,provider)
    assert ai.generate(clip,s)[0]=='Keep the next one rolling.'


def test_timeout_holds_clip_with_short_retry_and_no_placeholder(monkeypatch):
    clip,s=clip_and_settings(monkeypatch)
    monkeypatch.setattr(ai,'model_catalog',lambda:[MODEL])
    def provider(request):raise httpx.ReadTimeout('private-transport-sentinel',request=request)
    install_transport(monkeypatch,provider)
    before=time.time();ai.caption_pending()
    with connect() as db:
        saved=db.execute('SELECT * FROM clips WHERE id=?',(clip['id'],)).fetchone()
        assert saved['status']=='needs_caption' and not saved['caption']
        assert 'timed out' in saved['error'] and 'private-transport-sentinel' not in saved['error']
    assert before+59 <= float(runtime('caption_retry_at')) <= time.time()+61


def test_catalog_failure_preserves_compatible_completion(monkeypatch):
    clip,s=clip_and_settings(monkeypatch)
    def offline():raise httpx.ConnectError('Catalog unavailable')
    monkeypatch.setattr(ai,'model_catalog',offline)
    install_transport(monkeypatch,lambda request:httpx.Response(200,json=completion()))
    assert ai.generate(clip,s)[0]=='Keep the next one rolling.'
