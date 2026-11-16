"""Quota identities may be reused; admission facts and leases never are."""
import sys,time
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timedelta
import pytest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_pipeline import pipeline


def test_normalization_reused_but_region_replacement_invalidates_both_levels(monkeypatch):
    import address_daily_orders as daily,package_rules,region_index
    index=[object()];calls=[]
    monkeypatch.setattr(region_index,'get_region_index',lambda:index[0])
    def norm(raw):calls.append(raw);return str(id(index[0]))+raw
    monkeypatch.setattr(package_rules,'address_key',norm)
    first=SimpleNamespace(info={});second=SimpleNamespace(info={})
    old=daily._address_key(first,'synthetic-address')
    assert daily._address_key(second,'synthetic-address')==old and len(calls)==1
    index[0]=object()
    assert daily._address_key(first,'synthetic-address')!=old and len(calls)==2
    assert daily._address_key(second,'synthetic-address')==daily._address_key(first,'synthetic-address')


def test_shared_normalization_is_bounded_and_never_caches_unavailable_index(monkeypatch):
    import address_daily_orders as daily,package_rules,region_index
    calls=[];index=object()
    monkeypatch.setattr(region_index,'get_region_index',lambda:index)
    monkeypatch.setattr(daily,'_NORMALIZATION_LIMIT',4)
    def norm(raw):calls.append(raw);return raw+'-key'
    monkeypatch.setattr(package_rules,'address_key',norm)
    for i in range(8):daily._address_key(SimpleNamespace(info={}),str(i))
    assert len(daily._normalization_cache)<=4
    assert daily._address_key(SimpleNamespace(info={}),'0')=='0-key' and len(calls)==9
    monkeypatch.setattr(region_index,'get_region_index',lambda:None)
    daily._address_key(SimpleNamespace(info={}),'0');daily._address_key(SimpleNamespace(info={}),'0')
    assert len(calls)==11


def test_quota_counts_status_changes_and_address_edits_are_always_fresh(pipeline):
    from models import Order
    from address_daily_orders import used,record
    p=pipeline;now=datetime.utcnow();a='北京市朝阳区建国路100号';b='北京市朝阳区建国路200号'
    with p.db.SessionLocal() as s:
        assert used(s,a,now)==used(s,b,now)==0
        o=Order(user_id=p.user.id,order_number='SYNTHETIC-QUOTA',recipient_name='隔离',recipient_phone='13800000000',recipient_address=a,status='pending',created_at=now,total_amount=0,grand_total=0)
        s.add(o);s.flush();record(s,o,now)
        assert used(s,a,now)==1
        o.recipient_address=b;s.flush()
        assert used(s,b,now)==1  # current address, even after a normalization cache hit
        o.status='cancelled';s.flush()
        assert used(s,a,now)==used(s,b,now)==0
        s.rollback()


def test_order_address_normalization_is_warmed_without_business_writer(pipeline,monkeypatch):
    import package_rules,address_daily_orders
    from models import Order
    p=pipeline;original=package_rules.address_key;observed=[]
    monkeypatch.setenv('BOT_ACCOUNT_SWITCH_AT','2026-01-01T00:00:00+00:00')
    monkeypatch.setenv('BOT_ACCOUNT_SWITCH_ID','123456789')
    # More addresses than the shared cache can hold must still use this
    # request's versioned warmup, not normalize again under the writer.
    monkeypatch.setattr(address_daily_orders,'_NORMALIZATION_LIMIT',2)
    with p.db.SessionLocal() as s:
        for i in range(5):
            s.add(Order(user_id=p.user.id,order_number=f'WARM-{i}',recipient_name='隔离',
                recipient_phone='13800000000',recipient_address=f'北京市朝阳区建国路{10000+i}号',
                total_amount=0,grand_total=0,status='pending',created_at=datetime.utcnow()-timedelta(days=2)))
        s.commit()
    def norm(raw):
        with p.db.engine.connect() as other:
            other.exec_driver_sql('BEGIN IMMEDIATE');other.rollback()
        observed.append(raw);return original(raw)
    monkeypatch.setattr(package_rules,'address_key',norm)
    order=p.create(1,address='北京市朝阳区建国路888号')
    assert order and observed


def test_failed_optional_warmup_does_not_skip_quota_checks(pipeline,monkeypatch):
    import address_daily_orders
    p=pipeline
    monkeypatch.setattr(address_daily_orders,'prewarm',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic-warmup-failure')))
    a=p.create(1,address='北京市朝阳区建国路999号');b=p.create(2,address='北京市朝阳区建国路999号')
    assert a and b and a.status=='pending' and b.status=='split_pending'


def test_claim_lease_starts_after_probe_work_not_from_stale_clock(pipeline,monkeypatch):
    from sqlalchemy import text
    import telegram_order_queue as module
    p=pipeline;p.receive(1);clock=[time.time()+1];original=p.queue._candidate_rows;calls=[]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    def probe(*a,**k):
        row=original(*a,**k);calls.append(1)
        if len(calls)==1:clock[0]+=10
        return row
    monkeypatch.setattr(p.queue,'_candidate_rows',probe)
    job=p.queue.claim(lease_seconds=120)
    try:
        with p.db.engine.connect() as c:
            row=c.execute(text('SELECT started_at,lease_until FROM telegram_order_jobs')).one()
        assert row.started_at==clock[0] and row.lease_until==clock[0]+120
        assert job['started_at']==row.started_at and job['lease_until']==row.lease_until
    finally:p.release(job)


def test_explicit_queue_clock_remains_deterministic(pipeline):
    p=pipeline;p.receive(1);now=time.time()+1;job=p.queue.claim(now=now,lease_seconds=120)
    try:assert job['started_at']==now and job['lease_until']==now+120
    finally:p.release(job)
