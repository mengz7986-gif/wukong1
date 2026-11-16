"""Candidate validation only: login, GET endpoints, no order creation or replay."""
import json,os
from pathlib import Path
import requests
from dotenv import dotenv_values
from data_evidence import evidence

env=dotenv_values('/etc/newbot/runtime.env')
db=Path(env['DATABASE_URL'].removeprefix('sqlite:///'))
assert str(db).startswith(('/var/lib/newbot/candidates/','/var/lib/newbot/live/'))
baseline=os.getenv('NEWBOT_VALIDATION_BASELINE')
if baseline:
    expected=json.loads(Path(baseline).read_text())
elif '/live/' in str(db):
    # Live business data must never be compared to the historical migration
    # seed or restored over merely because an administrator changed it.
    expected=evidence(db)
else:expected=json.loads((Path('/opt/newbot/current')/'data-evidence.json').read_text())
assert evidence(db)==expected,'Candidate changed after startup'
base='http://127.0.0.1:8765'
headers={'Host':'admin.chdf888.com','X-Forwarded-Proto':'https'}
s=requests.Session()
r=s.post(base+'/login',headers=headers,data={'username':env['ADMIN_USERNAME'],'password':env['ADMIN_PASSWORD']},allow_redirects=False,timeout=20)
assert r.status_code==302
assert 'secure' in r.headers['Set-Cookie'].lower() and 'httponly' in r.headers['Set-Cookie'].lower()
# Secure session cookies must be used through the HTTPS public origin. For this
# loopback-only validation explicitly supply its cookie without relaxing flags.
cookie=r.headers['Set-Cookie'].split(';',1)[0]
authed={**headers,'Cookie':cookie}
assert s.get(base+'/admin',headers=authed,timeout=20).status_code==200
assert requests.get(base+'/admin',headers=headers,allow_redirects=False,timeout=20).status_code==302
stock=requests.get(base+'/api/stock',headers={**headers,'X-API-Key':env['API_SECRET_KEY']},timeout=20)
assert stock.status_code==200, 'Authenticated stock read failed'
assert requests.get(base+'/api/stock',headers=headers,allow_redirects=False,timeout=20).status_code in (401,403)
assert requests.get(base+'/login',headers={'Host':'unapproved.example'},timeout=20).status_code==400
assert evidence(db)==expected,'Validation unexpectedly mutated business data'
token=env['BOT_TOKEN']
identity=requests.post('https://api.telegram.org/bot'+token+'/getMe',timeout=15).json()
assert identity.get('ok')
webhook=requests.post('https://api.telegram.org/bot'+token+'/getWebhookInfo',timeout=15).json()
assert webhook.get('ok')
result=dict(admin_login=True,secure_session=True,unauthenticated_access_blocked=True,
    readonly_api=True,host_boundary=True,all_rows_unchanged=True,
    bot_identity={'id':identity['result']['id'],'username':identity['result']['username']},
    webhook_configured=bool(webhook['result'].get('url')),bot_polling_started=False,
    schema_tables=expected['table_count'],stock=expected['stock_quantity'],fifo=expected['fifo_quantity'],users=expected['user_count'],accounts=expected['accounts'])
(db.parent/'backend-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
