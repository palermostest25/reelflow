import os
from pathlib import Path

DATA = Path(os.getenv('DATA_DIR', './data')).resolve()
FOOTAGE = Path(os.getenv('FOOTAGE_DIR', './footage')).resolve()
DB = DATA / 'reelflow.sqlite3'
SECRET = os.getenv('APP_SECRET', '')
TOKEN_KEY = os.getenv('TOKEN_KEY', '')
PASSWORD = os.getenv('ADMIN_PASSWORD', '')
PUBLIC_URL = os.getenv('PUBLIC_URL', 'http://localhost:8080').rstrip('/')
DEMO = os.getenv('DEMO_MODE', 'false').lower() == 'true'
META_ID = os.getenv('META_APP_ID', '')
META_SECRET = os.getenv('META_APP_SECRET', '')
API_VERSION = os.getenv('META_API_VERSION', 'v25.0')
AI_BASE = os.getenv('AI_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')
AI_KEY = os.getenv('AI_API_KEY', '')
AI_MODEL = os.getenv('AI_MODEL', '')

DEFAULTS = {
    'brand_name': 'TRADES', "business_context": "Infer this account’s subject and voice from its previous posts. Use footage and operator notes for clip-specific facts.",
    'voice_notes': 'Short, natural, one or two sentences. No invented locations, bookings, track names or event dates.',
    'caption_instructions': '',
    'timezone': 'Australia/Perth', 'paused': True, 'dry_run': True,
    'trials_enabled': True, 'trials_per_day': 5, 'main_per_day': 1, 'preview_days': 7, 'low_watermark': 7,
    'posting_start_hour': 8, 'posting_end_hour': 22, 'learn_timing': True,
    'conserve_every_days': 2, 'conserve_main_per_day': 1, 'conserve_trials_per_day': 1,
    'drought_until': '', 'conserve': True, 'ntfy_url': 'https://ntfy.sh',
    'ntfy_topic': '', 'ai_model': AI_MODEL, 'ai_vision': True,
    'require_approval': False, 'min_gap_days': 1,
    'schedule_hours': [18, 18, 18, 18, 18, 18, 18],
}

def bootstrap_runtime():
    global SECRET, TOKEN_KEY, PASSWORD
    from .bootstrap import credentials
    values = credentials(DATA, DB)
    SECRET = values['APP_SECRET']
    TOKEN_KEY = values['TOKEN_KEY']
    PASSWORD = values['ADMIN_PASSWORD']


def validate_runtime():
    bootstrap_runtime()
    for folder in (DATA, DATA / 'media', DATA / 'thumbs', DATA / 'frames', DATA / 'uploads', FOOTAGE):
        folder.mkdir(parents=True, exist_ok=True)
