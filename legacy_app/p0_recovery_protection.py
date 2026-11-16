"""Exact incident hold, no writes/replay or extra worker/queue.

Existing nine remain frozen in place. Unrelated complete future updates retain
original scheduling/source validation. Related replies/drafts stay unclaimed.
"""
import hashlib,json,logging,os,stat
from pathlib import Path
from sqlalchemy import text
PINNED_POLICY_SHA256='dc6ddc2b34bf6b977c9cac51a4369a93be2b7452e5fdeaa23f56f10da3dec8cd'
DEFAULT_PATH='/etc/newbot/p0-recovery-protection-20261028.json'
BOT_ID='8729718308';INCIDENT='oom-20261028-164416'
FIELDS=('namespace','message_id','state','received_at','source_date','started_at','finished_at','lease_until','attempts','fingerprint')
class ProtectionError(RuntimeError):pass

def identity(row):return hashlib.sha256(('%s:%s:%s'%(row['namespace'],row['chat_id'],row['message_id'])).encode()).hexdigest()
def hashed(value):return hashlib.sha256(str(value).encode()).hexdigest() if value is not None else None

def job_pin(row):
 out={k:row[k] for k in FIELDS};out.update(identity_sha256=identity(row),chat_sha256=hashed(row['chat_id']),sender_sha256=hashed(row['sender_id']),lease_token_sha256=hashed(row['lease_token']));return out

def signature(st):return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns

class Protection:
 def __init__(self,path):
  self.path=Path(path)
  if self.path.is_symlink():raise ProtectionError('P0_POLICY_SYMLINK')
  st=self.path.stat()
  if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0,os.geteuid()) or st.st_mode&0o022 or st.st_size>65536:raise ProtectionError('P0_POLICY_PRIVATE_TRUST_REQUIRED')
  raw=self.path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=PINNED_POLICY_SHA256:raise ProtectionError('P0_EXACT_POLICY_SHA_REQUIRED')
  self.signature=signature(st);self.policy=json.loads(raw);p=self.policy
  if set(p)!={'version','incident','bot_id','cutoff_received_at','entries','frozen_execution_receipts_sha256'} or p['version']!=1 or p['incident']!=INCIDENT or p['bot_id']!=BOT_ID or type(p['cutoff_received_at']) not in (int,float) or not isinstance(p['entries'],list) or len(p['entries'])!=9:raise ProtectionError('P0_EXACT_INCIDENT_SCOPE_REQUIRED')
  self.entries={r['identity_sha256']:r for r in p['entries']}
  if len(self.entries)!=9 or sum(r['state']=='PROCESSING' for r in self.entries.values())!=7 or sum(r['state']=='RECEIVED' for r in self.entries.values())!=2:raise ProtectionError('P0_EXACT_NINE_STATE_SET')
  if any(len(k)!=64 or any(ch not in '0123456789abcdef' for ch in k) for k in self.entries):raise ProtectionError('P0_FULL_IDENTITY_REQUIRED')
  self.chat_refs={r['chat_sha256'] for r in self.entries.values()};self.drafts={d for r in self.entries.values() for d in r['draft_token_sha256']};self.verified=False
 def assert_unchanged(self):
  if self.path.is_symlink() or signature(self.path.stat())!=self.signature:raise ProtectionError('P0_POLICY_DRIFT_NO_AUTO_REFRESH')
 def verify_before_workers(self,database):
  self.assert_unchanged()
  with database.engine.connect() as c:
   rows=c.execute(text("SELECT namespace,chat_id,message_id,sender_id,state,received_at,source_date,started_at,finished_at,lease_until,lease_token,attempts,fingerprint,draft_token FROM telegram_order_jobs WHERE namespace IN ('shared',:observer) AND received_at<=:cutoff AND state IN ('RECEIVED','PROCESSING','FAILED_RETRYABLE','REVIEW_REQUIRED')"),dict(observer='bot:'+BOT_ID,cutoff=self.policy['cutoff_received_at'])).mappings().all()
   if {identity(r) for r in rows}!=set(self.entries):raise ProtectionError('P0_OLD_ACTIVE_SET_CHANGED')
   for row in rows:
    expected=self.entries[identity(row)];pin=job_pin(row)
    if any(pin.get(k)!=expected.get(k) for k in pin):raise ProtectionError('P0_FROZEN_JOB_CHANGED')
    args=dict(ns=row['namespace'],chat=row['chat_id'],msg=row['message_id'])
    ids=sorted(c.execute(text('SELECT order_id FROM telegram_execution_commits WHERE namespace=:ns AND chat_id=:chat AND message_id=:msg'),args).scalars())
    sourceids=sorted(c.execute(text('SELECT id FROM orders WHERE source_chat_id=:chat AND source_message_id=:msg'),args).scalars())
    tokens=set(c.execute(text('SELECT token FROM telegram_order_drafts WHERE namespace=:ns AND chat_id=:chat AND message_id=:msg'),args).scalars())
    if row['draft_token']:tokens.add(row['draft_token'])
    if ids!=expected['commit_order_ids'] or sourceids!=expected['source_order_ids'] or sorted(hashed(t) for t in tokens if t)!=expected['draft_token_sha256']:raise ProtectionError('P0_COMMIT_OR_DRAFT_FACT_CHANGED')
   unknown_rows=[dict(r) for r in c.execute(text("SELECT event_key,chat_id,source_message_id,edit_message_id,status,attempts,created_at,delivered_message_id,error_code FROM telegram_execution_receipts WHERE status IN ('sending','unknown') ORDER BY event_key")).mappings()]
   from recovery_timeout_hold import partition_verified
   try:unknown_rows,_=partition_verified(unknown_rows,c)
   except ValueError as error:raise ProtectionError(str(error)) from None
   if hashlib.sha256(json.dumps(unknown_rows,sort_keys=True).encode()).hexdigest()!=self.policy['frozen_execution_receipts_sha256']:raise ProtectionError('P0_FROZEN_RECEIPT_SET_CHANGED')
   current_active=c.execute(text("SELECT namespace,chat_id,message_id FROM telegram_order_jobs WHERE namespace IN ('shared',:observer) AND state IN ('PROCESSING','FAILED_RETRYABLE','REVIEW_REQUIRED')"),dict(observer='bot:'+BOT_ID)).mappings()
   if any(identity(r) not in self.entries for r in current_active):raise ProtectionError('P0_NEW_UNREVIEWED_ACTIVE_TASK')
   if c.execute(text("SELECT COUNT(*) FROM telegram_execution_receipts WHERE status='pending'")).scalar():raise ProtectionError('P0_OLD_PENDING_SEND_REQUIRES_REVIEW')
   if c.execute(text("SELECT COUNT(*) FROM telegram_execution_receipts WHERE status='sending'")).scalar():raise ProtectionError('P0_STARTUP_SENDING_REQUIRES_REVIEW')
  self.verified=True
  logging.getLogger(__name__).warning('P0_RECOVERY_HOLD_ACTIVE incident=%s exact_jobs=9 original_states_unchanged=true',INCIDENT)
 def blocks(self,row,cipher=None):
  self.assert_unchanged()
  if not self.verified:raise ProtectionError('P0_PROTECTION_NOT_STARTUP_VERIFIED')
  ref=identity(row)
  if ref in self.entries:
   pin=job_pin(row)
   if any(pin.get(k)!=self.entries[ref].get(k) for k in pin):raise ProtectionError('P0_FROZEN_JOB_DRIFT')
   return True
  # Related new callbacks/quotes are held too, never converted to success/failure.
  if row.get('draft_token') and hashed(row['draft_token']) in self.drafts:return True
  if row['message_id']>0 and hashed(row['chat_id']) in self.chat_refs and cipher is not None:
   try:
    body=json.loads(cipher.decrypt(row['payload'].encode()));reply=(body.get('message') or {}).get('reply_to_message') or {};reply_id=reply.get('message_id')
    if reply_id is not None and identity(dict(row,message_id=reply_id)) in self.entries:return True
   except Exception as error:raise ProtectionError('P0_RELATED_SOURCE_UNPROVEN') from None
  return False

def attach(queue):
 # Existing new-server launcher independently requires this ingestion policy.
 # No bypass flag or caller-provided approved=True can disable incident holds.
 if os.getenv('NEWBOT_INGESTION_POLICY_PATH')!='/etc/newbot/ingestion-policy.json':return None
 policy=Protection(DEFAULT_PATH);policy.verify_before_workers(queue.database);queue.p0_protection=policy;return policy
