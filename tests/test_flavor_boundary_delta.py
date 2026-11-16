"""Isolated lexical boundary and private stage evidence, no production data."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))
from test_four_specials_regressions import isolated_rules


@pytest.mark.parametrize('name',['荔枝','海盐荔枝','雪玉荔枝','桂枝','薄荷2号','七喜','一颗荔枝','首选荔枝'])
def test_exact_catalog_lexemes_keep_priority(isolated_rules,name):
    import flavor_aliases
    _,catalog=isolated_rules
    product=dict(catalog[0],name=name)
    assert flavor_aliases.safe_learning_token(name,product,[product],{'aliases':[]})==name


def test_lychee_suffix_is_not_packaging_and_reviewable_alias_survives(isolated_rules):
    import flavor_aliases
    _,catalog=isolated_rules
    product=dict(catalog[0],name='雪玉荔枝')
    assert not flavor_aliases._syntax_contaminated('荔枝')
    assert not flavor_aliases._syntax_contaminated('雪王荔枝')
    assert flavor_aliases.safe_learning_token('玉荔枝',product,[product],{'aliases':[]})=='玉荔枝'
    # Removing a unit false positive must not relax the existing similarity gate.
    assert flavor_aliases.safe_learning_token('雪王荔枝',product,[product],{'aliases':[]}) is None
    assert not flavor_aliases.plan(dict(warehouse_id=1,category_id=10,product_id=1,aliases='雪王荔枝'),[product],{'aliases':[]})['conflicts']


@pytest.mark.parametrize('alias',['一个荔枝','一盒荔枝','颗 荔枝','1荔枝','二荔枝','１荔枝','荔枝一支','荔枝2','荔枝（一盒）','荔枝 盒','盒:荔枝','荔枝×2','备选荔枝','（备选荔枝）','荔枝 备注','甲牌荔枝','乙牌荔枝'])
def test_raw_syntax_never_enters_learning_or_manual_approval(isolated_rules,alias):
    import flavor_aliases
    _,catalog=isolated_rules
    product=dict(catalog[0],name='荔枝')
    other=dict(catalog[1],name='海盐荔枝')
    all_products=[product,other]
    assert flavor_aliases.safe_learning_token(alias,product,all_products,{'aliases':[]}) is None
    result=flavor_aliases.plan(dict(warehouse_id=1,category_id=10,product_id=1,aliases=alias),all_products,{'aliases':[]})
    assert result['conflicts'] and not result['new']


def test_leading_numeric_words_are_not_assumed_to_be_quantities(isolated_rules):
    import flavor_aliases
    _,catalog=isolated_rules
    for alias in ('7up','2代薄荷'):
        result=flavor_aliases.plan(dict(warehouse_id=1,category_id=10,product_id=1,aliases=alias),catalog,{'aliases':[]})
        assert result['new']==[alias] and not result['conflicts']


def test_same_brand_canonical_flavor_cannot_be_learned_for_other_target(isolated_rules):
    import flavor_aliases
    _,catalog=isolated_rules
    lychee=dict(catalog[0],name='海盐荔枝')
    lemon=dict(catalog[0],id=4,name='海盐柠檬')
    assert flavor_aliases.safe_learning_token('海盐荔枝',lemon,[lychee,lemon],{'aliases':[]}) is None
    apple=dict(catalog[0],name='苹果');green=dict(apple,id=4,name='青苹果')
    assert flavor_aliases.safe_learning_token('苹果',green,[apple,green],{'aliases':[]}) is None


def test_rejection_stays_scoped_and_survives_new_lexical_boundary(isolated_rules):
    import flavor_aliases,rule_dictionary
    path,catalog=isolated_rules
    first=dict(catalog[0],name='雪玉荔枝');second=dict(catalog[2],name='雪玉荔枝')
    import sqlite3,os
    with sqlite3.connect(os.environ['DATABASE_URL'].removeprefix('sqlite:///')) as connection:
        connection.execute("UPDATE products SET name='雪玉荔枝' WHERE id IN (1,3)")
    all_products=[first,second]
    from test_order_six_final import proof
    candidate=flavor_aliases.learn('玉荔枝',first,.95,all_products,origin='CUSTOMER_CONFIRMED_AI',event='first',confirmation=proof(first,'玉荔枝'))
    flavor_aliases.review({'id':candidate['id'],'action':'reject'},'test',all_products)
    flavor_aliases.review({'id':candidate['id'],'action':'delete'},'test',all_products)
    assert flavor_aliases.learn('玉荔枝',first,.99,all_products) is None
    assert flavor_aliases.learn('玉荔枝',second,.99,all_products,origin='CUSTOMER_CONFIRMED_AI',event='other',confirmation=proof(second,'玉荔枝')) is not None
    store=rule_dictionary.read_store()
    assert len(store['rejected_flavor_mappings'])==1


def test_new_stage_timings_await_and_mark_failures_without_changing_result():
    import order_trace
    import time
    row={'started':time.perf_counter(),'stages':[]};token=order_trace.CURRENT.set(row)
    @order_trace.timed_async('ORDER_PREVIEW')
    async def call(fail=False):
        await asyncio.sleep(.001)
        if fail:raise ValueError('synthetic')
        return 42
    try:
        assert asyncio.run(call())==42
        with pytest.raises(ValueError):asyncio.run(call(True))
        assert len(row['stages'])==2 and all(x[1]>=1 for x in row['stages'])
        assert 'ORDER_PREVIEW' in row['errors']
    finally:order_trace.CURRENT.reset(token)


def test_stage_records_allow_only_private_opaque_correlations(tmp_path,monkeypatch):
    import performance_metrics,order_trace
    target=tmp_path/'performance.json';monkeypatch.setenv('RAINBOW_PERFORMANCE_PATH',str(target))
    order_trace.persist({'trace_id':'a'*32,'source':'b'*16,'stages':[('SOURCE_REGION_EVIDENCE',1),('SOURCE_QUANTITY_EVIDENCE',2),('ORDER_WRITE_LOCK_WAIT',3)]},10,'API_TOTAL')
    rows=json.loads(target.read_text())['samples']
    assert len(rows)==4 and all(x['trace_id']=='a'*32 and x['source']=='b'*16 for x in rows)
    performance_metrics.record_samples([dict(mode='application',provider='Local',stage='ORDER_PREVIEW',elapsed_ms=1,trace_id='customer text',source='123456789',raw_text='private')])
    row=json.loads(target.read_text())['samples'][-1]
    assert not {'trace_id','source','raw_text'}&row.keys()


def test_async_wrapper_has_no_trace_retention_between_orders():
    import order_trace
    @order_trace.timed_async('VISIBLE_CATALOG')
    async def call():return 'ok'
    assert asyncio.run(call())=='ok' and order_trace.CURRENT.get() is None
