"""Consistent SQLite snapshot. Run inside the reelflow container; back up /data/media separately."""
import datetime
import os
from pathlib import Path
import sqlite3

folder = Path(os.getenv('DATA_DIR','./data'))
target = folder/'backups'
target.mkdir(exist_ok=True)
name = target/('reelflow-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite3')
with sqlite3.connect(folder/'reelflow.sqlite3') as source, sqlite3.connect(name) as dest:
    source.backup(dest)
print(name)
