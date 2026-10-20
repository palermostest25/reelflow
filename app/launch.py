"""One container runs the web UI and scheduler, with shared lifecycle handling."""
import os
import signal
import subprocess
import sys
import time
from . import config as c


def terminate_group(process, sig):
    try:
        # A crashed worker can leave FFmpeg alive in its original process group.
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def supervise(commands, grace=25):
    """Stop both children on shutdown; exit failed if either unexpectedly dies."""
    stopping = False
    children = []
    previous = {}

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, request_stop)
    try:
        for name, command in commands:
            children.append((name, subprocess.Popen(command, start_new_session=True)))
        while not stopping:
            for name, process in children:
                code = process.poll()
                if code is not None:
                    print('%s exited unexpectedly (%s); restarting the container.' % (name, code), flush=True)
                    return 1
            time.sleep(.25)
        return 0
    finally:
        # Includes FFmpeg descendants, so a stopped worker cannot leave a conversion running.
        for name, process in children:
            terminate_group(process, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while any(process.poll() is None for _, process in children) and time.monotonic() < deadline:
            time.sleep(.1)
        for name, process in children:
            terminate_group(process, signal.SIGKILL)
            process.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    # Finish database initialization before either child starts.
    from .db import init
    init()
    if not os.getenv('ADMIN_PASSWORD'):
        print('Reelflow generated admin password: '+c.PASSWORD, flush=True)
        print('This password persists in the data volume. Keep Docker logs private.', flush=True)
    return supervise([
        ('Web UI', [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8080', '--no-access-log', '--no-proxy-headers']),
        ('Posting worker', [sys.executable, '-m', 'app.worker']),
    ])


if __name__ == '__main__':
    raise SystemExit(main())
