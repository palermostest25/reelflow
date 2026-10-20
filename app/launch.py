"""Container entrypoint: first-run setup requires no host tooling."""
import os
from . import config as c


def main():
    c.validate_runtime()
    if not os.getenv('ADMIN_PASSWORD'):
        print('Reelflow generated admin password: '+c.PASSWORD, flush=True)
        print('This password persists in the data volume. Keep Docker logs private.', flush=True)
    import uvicorn
    uvicorn.run('app.main:app', host='0.0.0.0', port=8080, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
