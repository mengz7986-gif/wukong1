"""Isolated protection tests: metadata-only SQLite fixtures, no business orders."""
import importlib.util,json,sqlite3
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('newbot_protect',Path(__file__).with_name('protect.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

@pytest.mark.parametrize('used,free,inode,expected',[
    (79,30,10,'normal'),(80,30,10,'warning'),(90,20,10,'urgent'),
    (95,10,10,'critical'),(30,9,10,'warning'),(30,4,10,'urgent'),
    (30,1,10,'critical'),(30,20,90,'urgent'),(30,20,95,'critical')])
def test_capacity_and_inode(monkeypatch,used,free,inode,expected):
    total=100*p.GIB;available=free*p.GIB
    # Separate free-space tests from percentage tests.
    if used>=79:available=(100-used)*p.GIB
    else:total=int(available/(1-used/100))
    fake=type('Stat',(),dict(f_blocks=total,f_frsize=1,f_bavail=available,f_files=1000,f_favail=1000-inode*10))()
    monkeypatch.setattr(p.os,'statvfs',lambda _:fake)
    assert p.usage('/')['severity']==expected

def test_sqlite_online_snapshot_and_corruption(tmp_path):
    source=tmp_path/'metadata.sqlite3'
    with sqlite3.connect(source) as c:
        c.execute('create table metadata(value text)');c.execute("insert into metadata values ('fixture-not-an-order')")
    output=tmp_path/'orders.sqlite3';p.snapshot(source,output)
    manifest=dict(kind='newbot-online-backup-v1',files={'orders.sqlite3':dict(sha256=p.digest(output),sqlite=True)})
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    p.verify(tmp_path)
    output.write_bytes(b'corruption simulation')
    with pytest.raises(RuntimeError,match='HASH_MISMATCH'):p.verify(tmp_path)

def test_missing_db_does_not_create_backup_or_delete_existing(tmp_path,monkeypatch):
    backups=tmp_path/'backups';backups.mkdir()
    old=backups/'important-manual-backup';old.write_text('keep')
    monkeypatch.setattr(p,'DB',tmp_path/'missing.sqlite3');monkeypatch.setattr(p,'BACKUPS',backups)
    with pytest.raises(RuntimeError,match='BUSINESS_DATABASE_MISSING'):p.backup('routine')
    assert old.read_text()=='keep' and len(list(backups.iterdir()))==1

def test_capacity_guard_preserves_previous(tmp_path,monkeypatch):
    db=tmp_path/'source.sqlite3';db.write_bytes(b'existing')
    backups=tmp_path/'backups';backups.mkdir();old=backups/'protected';old.write_text('keep')
    monkeypatch.setattr(p,'DB',db);monkeypatch.setattr(p,'BACKUPS',backups)
    monkeypatch.setattr(p,'usage',lambda _:dict(pause_optional_archives=True,free_bytes=100*p.GIB))
    with pytest.raises(RuntimeError,match='BACKUP_CAPACITY_GUARD'):p.backup('routine')
    assert old.read_text()=='keep' and len(list(backups.iterdir()))==1

def test_manifest_cannot_include_parent_paths(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps(dict(kind='newbot-online-backup-v1',files={'../orders.sqlite3':{}})))
    with pytest.raises(RuntimeError,match='INVALID_MANIFEST'):p.verify(tmp_path)
