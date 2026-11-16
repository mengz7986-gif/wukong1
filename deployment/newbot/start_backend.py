"""Server-only backend entrypoint. No Telegram polling, webhook or workers."""
import os
from pathlib import Path
import sys
from dotenv import load_dotenv

load_dotenv(os.environ.get('NEWBOT_ENV_FILE','/etc/newbot/runtime.env'), override=True)
if os.environ.get('NEWBOT_ROLE') != 'backend-only':
    raise SystemExit('Backend-only deployment role required')
if os.environ.get('RAINBOW_DISABLE_BACKGROUND_WORKERS') != '1':
    raise SystemExit('Background business workers must remain disabled')
root=Path('/opt/newbot/current/legacy_app')
os.chdir(root)
sys.path.insert(0,str(root))
import uvicorn
uvicorn.run('api:app',host='127.0.0.1',port=8765,proxy_headers=False,access_log=False)
