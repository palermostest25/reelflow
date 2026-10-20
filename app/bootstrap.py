"""Generate installation credentials inside the persistent Docker data volume."""
import base64
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile


def credentials(data_dir, db_path):
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / '.runtime.json'
    with (data_dir / '.runtime.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            saved = json.loads(target.read_text()) if target.exists() else {}
            if not isinstance(saved, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise RuntimeError('Installation credentials could not be read. Restore /data/.runtime.json from backup; do not delete it to reset an existing installation.') from None
        requested = {key: os.getenv(key, '') for key in ('APP_SECRET', 'TOKEN_KEY', 'ADMIN_PASSWORD')}
        if saved.get('TOKEN_KEY') and requested['TOKEN_KEY'] and saved['TOKEN_KEY'] != requested['TOKEN_KEY']:
            raise RuntimeError('TOKEN_KEY differs from this installation. Restore the original key to keep Instagram and AI tokens readable.')
        if not saved.get('TOKEN_KEY') and not requested['TOKEN_KEY'] and db_path.exists():
            # Older installations kept their encryption key in external configuration.
            # Refuse to generate a replacement over existing encrypted credentials.
            try:
                with sqlite3.connect(str(db_path)) as db:
                    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='secrets'").fetchone()
                    has_tokens = has_table and db.execute("SELECT 1 FROM secrets WHERE value!='' LIMIT 1").fetchone()
            except sqlite3.Error:
                raise RuntimeError('Existing database could not be inspected. Check the data volume before starting.') from None
            if has_tokens:
                raise RuntimeError('Existing encrypted tokens need their original TOKEN_KEY. Put that key in compose.yaml once before migrating; see the deployment guide.')
        defaults = {
            'APP_SECRET': lambda: secrets.token_urlsafe(48),
            'TOKEN_KEY': lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            'ADMIN_PASSWORD': lambda: secrets.token_urlsafe(24),
        }
        result = {key: requested[key] or saved.get(key) or generate() for key, generate in defaults.items()}
        if len(result['APP_SECRET']) < 32 or len(result['ADMIN_PASSWORD']) < 12:
            raise RuntimeError('Use a 12+ character ADMIN_PASSWORD. Explicit APP_SECRET values must be at least 32 characters.')
        from cryptography.fernet import Fernet
        try:
            Fernet(result['TOKEN_KEY'].encode())
        except (ValueError, TypeError):
            raise RuntimeError('TOKEN_KEY is invalid. Restore the original installation key.') from None
        if result != saved:
            with tempfile.NamedTemporaryFile(mode='w', dir=str(data_dir), prefix='.runtime-', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump(result, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        os.chmod(target, 0o600)
        return result
