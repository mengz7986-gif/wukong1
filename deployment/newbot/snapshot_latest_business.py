"""Dedicated local business rollback copy, no pause, deletion or cleanup."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib,json,os,shutil,sqlite3,uuid
from pathlib import Path
from data_evidence import evidence

root=Path(__file__).resolve().parents[2]
source=root/'work/legacy-data/imported-catalog-20261021.db'
os.umask(0o077)
dest=root/'work/backups'/('latest-business-migration-'+uuid.uuid4().hex)
dest.mkdir(mode=0o700)
target=dest/'latest-business.sqlite3'
with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as c,closing(sqlite3.connect(target)) as d:c.backup(d)
fact=evidence(target)
digest=hashlib.sha256(target.read_bytes()).hexdigest()
restored=dest/'restore-proof.sqlite3'
shutil.copyfile(target,restored)
assert hashlib.sha256(restored.read_bytes()).hexdigest()==digest
assert evidence(restored)==fact
current=evidence(source)
report=dict(source=str(source),backup=str(target),sha256=digest,at=datetime.now(timezone.utc).isoformat(),
    restored=True,current_matches_snapshot=current==fact,
    write_freeze=(root/'work/server-deployment/local-writes-frozen.json').exists(),evidence=fact)
(dest/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(dict(backup_directory=str(dest),sha256=digest,current_matches_snapshot=current==fact,stock=fact['stock_quantity'],users=fact['user_count'],accounts=fact['accounts']),ensure_ascii=False))
