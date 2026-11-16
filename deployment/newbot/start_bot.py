"""Explicitly authorized future ingestion only. Never sets activation time."""
import os,sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv('/etc/newbot/runtime.env',override=True)
if os.getenv('NEWBOT_INGESTION_POLICY_PATH')!='/etc/newbot/ingestion-policy.json':
    raise SystemExit('Mandatory ingestion policy missing')
root=Path('/opt/newbot/current/legacy_app');sys.path.insert(0,str(root));os.chdir(root)
from newbot_safety import require_ready
try:require_ready()
except ValueError as e:raise SystemExit(str(e)) from None
from bot import OrderBot
OrderBot().run()
