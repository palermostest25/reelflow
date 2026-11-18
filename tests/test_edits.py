"""Funny edits follow the marked person and land in the library as a new clip."""
import shutil
import subprocess
import time
import pytest
from fastapi.testclient import TestClient
from app import config as c, edits, media
from app.db import connect
from app.main import app

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')

def moving_clip(path, seconds=3):
    # A textured square drifts across the frame: x = 100+100t, y = 300+80t.
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c=0x334455:s=360x640:d={seconds}:r=30',
                    '-f','lavfi','-i',f'testsrc=s=80x80:d={seconds}:r=30','-f','lavfi','-i',f'sine=f=440:d={seconds}',
                    '-filter_complex',"[0][1]overlay=x='60+t*60':y='200+t*50'[v]",'-map','[v]','-map','2:a',
                    '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True)

def test_tracker_follows_the_marked_box():
    path = c.DATA/'moving.mp4'
    moving_clip(path)
    spec = edits.validate({'start':.5,'box':{'x':90/360,'y':225/640,'w':80/360,'h':80/640},'effects':['circle'],'length':2},3)
    boxes = edits.track(path,spec,30,(360,640))
    for index in (30,60,74):
        (x,y,w,h),lost = boxes[index]
        t = index/30
        assert abs(x-(60+60*t))<12 and abs(y-(200+50*t))<12 and lost==0

@pytest.mark.parametrize('spec',[
    {'start':5,'box':{'x':.1,'y':.1,'w':.1,'h':.1},'effects':['arrow']},
    {'start':1,'box':{'x':.95,'y':.1,'w':.2,'h':.1},'effects':['arrow']},
    {'start':1,'box':{'x':.1,'y':.1,'w':.1,'h':.1},'effects':[]},
])
def test_invalid_marks_are_rejected(spec):
    with pytest.raises(edits.EditError):
        edits.validate(spec,3)

def test_edit_becomes_a_new_clip_and_replaces_the_original():
    clip_id = 'a'*64
    source = c.FOOTAGE/'night.mp4'
    moving_clip(source)
    shutil.copyfile(source,c.DATA/'media'/(clip_id+'.mp4'))
    with connect(True) as db:
        db.execute("INSERT INTO intake_batches(id,name,mode,created_at) VALUES('upload:n','Night','mix',0)")
        db.execute("""INSERT INTO clips(id,name,source,created_at,status,caption,duration,media_version,batch,song)
            VALUES(?,?,?,?,'ready','Caption.',3,?,'upload:n','Big Tune')""",(clip_id,'night.mp4',str(source),time.time(),media.MEDIA_VERSION))
    with TestClient(app) as client:
        assert client.post('/api/login',json={'password':c.PASSWORD}).status_code==200
        h = {'x-csrf-token':client.get('/api/state').json()['csrf']}
        assert client.get(f'/api/clips/{clip_id}/faces?t=1').json()=={'faces':[]}
        r = client.post(f'/api/clips/{clip_id}/edits',headers=h,json={'start':.5,'box':{'x':.25,'y':.35,'w':.22,'h':.125},
            'effects':['arrow','circle','spotlight','zoom'],'label':'this guy','length':2})
        assert r.status_code==200
        for _ in range(300):
            status = client.get('/api/edits/'+r.json()['id']).json()
            if status['status']!='running':break
            time.sleep(.1)
        assert status['status']=='done', status
        assert client.post(f'/api/clips/{"b"*64}/edits',headers=h,json={'start':0,'box':{'x':0,'y':0,'w':.1,'h':.1},'effects':['arrow']}).status_code==404
    with connect() as db:
        new = dict(db.execute('SELECT * FROM clips WHERE id=?',(status['result_clip'],)).fetchone())
        original = db.execute('SELECT disabled FROM clips WHERE id=?',(clip_id,)).fetchone()[0]
        match = db.execute('SELECT score FROM song_matches').fetchone()[0]
    assert new['status']=='processing' and new['edit_of']==clip_id and new['batch']=='upload:n' and new['song']=='Big Tune'
    assert new['name']=='night · edit.mp4' and original==1 and match==100 and new['caption']=='Caption.'
    assert media.digest(media.Path(new['source']))==new['id']
    duration,width,height = media.video_info(new['source'])
    assert (width,height)==(360,640) and abs(duration-3)<.2
    probe = subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=codec_type','-of','csv=p=0',new['source']],capture_output=True,text=True)
    assert probe.stdout.strip()=='audio'

def test_interrupted_renders_are_reported_after_restart():
    with connect(True) as db:
        db.execute("INSERT INTO edits(id,clip_id,created_at,status) VALUES('x','y',0,'running')")
    edits.recover()
    with connect() as db:
        assert db.execute("SELECT status FROM edits WHERE id='x'").fetchone()[0]=='error'
