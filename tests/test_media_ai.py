import shutil
import subprocess
import time
import pytest
from app import config as c,media,ai
from app.db import connect
from conftest import seed

@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')
def test_actual_video_intake_conversion_and_duplicate_hashing():
    path=c.FOOTAGE/'sample.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=180x320:d=3.1','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True)
    now=time.time()
    assert media.scan(now)==0
    assert media.scan(now+31)==1
    with connect() as db:r=dict(db.execute('SELECT * FROM clips').fetchone())
    assert r['status']=='captioning' and r['duration']>=3
    assert (c.DATA/'media'/(r['id']+'.mp4')).exists()
    assert (c.DATA/'thumbs'/(r['id']+'.jpg')).exists()
    assert media.probe(c.DATA/'media'/(r['id']+'.mp4'))[1:]==(1080,1920)
    shutil.copyfile(path,c.FOOTAGE/'duplicate.mov')
    media.scan(now+40)
    assert media.scan(now+80)==0
    with connect() as db:assert db.execute('SELECT COUNT(*) FROM clips').fetchone()[0]==1

def test_missing_ai_key_never_creates_placeholder():
    cid=seed(1,status='captioning')[0]
    ai.caption_pending()
    with connect() as db:r=db.execute('SELECT * FROM clips WHERE id=?',(cid,)).fetchone()
    assert r['status']=='needs_caption'
    assert r['error'].startswith('Connect an AI provider')
