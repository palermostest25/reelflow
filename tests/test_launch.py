import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from app import launch


def test_child_exit_stops_sibling_and_fails_container(monkeypatch):
    original=launch.subprocess.Popen
    children=[]
    def spawn(*args,**kwargs):
        child=original(*args,**kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(launch.subprocess,'Popen',spawn)
    result=launch.supervise([
        ('failing-worker',[sys.executable,'-c','raise SystemExit(7)']),
        ('web',[sys.executable,'-c','import time; time.sleep(60)']),
    ],grace=2)
    assert result==1
    assert len(children)==2 and all(child.poll() is not None for child in children)


def test_container_sigterm_stops_both_children(tmp_path):
    script='''import sys
from app.launch import supervise
script="import os,time; from pathlib import Path; Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(60)"
raise SystemExit(supervise([('web',[sys.executable,'-c',script,sys.argv[1]]),('worker',[sys.executable,'-c',script,sys.argv[2]])],grace=2))
'''
    first=tmp_path/'web.pid'
    second=tmp_path/'worker.pid'
    process=subprocess.Popen([sys.executable,'-c',script,str(first),str(second)])
    try:
        for attempt in range(100):
            if first.exists() and second.exists():
                break
            time.sleep(.05)
        assert first.exists() and second.exists()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10)==0
        for path in (first,second):
            pid=int(path.read_text())
            try:
                os.kill(pid,0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError('A child survived container shutdown')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
