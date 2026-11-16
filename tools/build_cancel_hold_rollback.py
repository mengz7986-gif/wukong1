"""Build code-only safety rollback overlay; never operates on production."""
from pathlib import Path
import subprocess,tarfile,json,hashlib,io,argparse
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--evidence',required=True);a=p.parse_args()
repo=Path(__file__).resolve().parents[1];dest=Path(a.output);dest.mkdir(parents=True,exist_ok=False);evidence=Path(a.evidence);evidence.mkdir(parents=True,exist_ok=True)
commit='fb8611a07f0569de0068d9627b07928209178ab0'
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args])
with tarfile.open(fileobj=io.BytesIO(git('archive','990203e'))) as t:t.extractall(dest,filter='data')
names=[n for n in git('diff','--name-only','b3631cb',commit).decode().splitlines() if n.startswith('legacy_app/')]
for name in names:
 current=git('show',commit+':'+name)
 if name=='legacy_app/bot.py':
  file=dest/name;s=file.read_text();marker="        else:\n            try:\n                from learning_pipeline import record_committed\n                await asyncio.to_thread(record_committed, preview, order)"
  replacement="        else:\n            # Exact historical order-success hold, also on legacy group entry.\n            from six_unknown_receipt_hold import guard_direct\n            from success_receipt_audit import identity\n            guard_direct(identity(order),order_id=order.get('id'))\n            try:\n                from learning_pipeline import record_committed\n                await asyncio.to_thread(record_committed, preview, order)"
  assert s.count(marker)==1;s=s.replace(marker,replacement)
  startup="    async def _start_durable_queue(self, application):\n        from models import Database"
  gate="    async def _start_durable_queue(self, application):\n        import os\n        if os.getenv('NEWBOT_INGESTION_POLICY_PATH')=='/etc/newbot/ingestion-policy.json':\n            from success_receipt_audit import assert_quiet\n            await asyncio.to_thread(assert_quiet)\n        from models import Database"
  assert s.count(startup)==1;file.write_text(s.replace(startup,gate))
 else:
  file=dest/name
  if file.exists():assert git('show','990203e:'+name)==git('show','b3631cb:'+name)
  file.write_bytes(current)
cancel_names=['legacy_app/cancel_notice_hold.py','legacy_app/account_notifications.py','legacy_app/cancel_notice_worker.py','legacy_app/telegram_delivery_sync.py']
source_head=git('rev-parse','HEAD').decode().strip()
for name in cancel_names:
 (dest/name).write_bytes(git('show',source_head+':'+name))
names+=cancel_names
for name in ['tests/test_cancel_notice_hold.py','tools/verify_cancel_15920_gate.py']:
 (dest/name).parent.mkdir(parents=True,exist_ok=True);(dest/name).write_bytes(git('show',source_head+':'+name))
(dest/'tests/test_six_unknown_hold.py').write_bytes(git('show',commit+':tests/test_six_unknown_hold.py'))
manifest={'version_label':'990203e+six-and-cancel15920-holds','base_commit':'990203e2c7df060b710de10aae9bebfda5bdcb90','six_guard_source_commit':commit,'cancellation_guard_source_commit':source_head,'purpose':'SAFETY_PRESERVING_CODE_ROLLBACK_NOT_PURE_990203E','production_installed':False,'guard_overlay':{n:hashlib.sha256((dest/n).read_bytes()).hexdigest() for n in names}}
manifest_path=evidence/'guarded-rollback-manifest.json';manifest_path.write_text(json.dumps(manifest,indent=2))
with tarfile.open(evidence/'guarded-rollback-code-only.tar.gz','w:gz') as t:
 for name in names:t.add(dest/name,arcname=name)
 t.add(manifest_path,arcname='guarded-rollback-manifest.json')
print(json.dumps(manifest,indent=2))
