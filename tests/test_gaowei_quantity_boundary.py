# coding: utf-8
"""Post-release category/quantity boundary regression, synthetic data only."""
import json,sqlite3,sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_regressions import isolated_rules
CONTACT='\n姓名：测试\n手机号：13800138000\n地址：北京市朝阳区测试路8号'
@pytest.fixture
def catalog(isolated_rules):
    path,_=isolated_rules
    products=[dict(id=i,name=n,parent_id=parent,parent_name=category,default_warehouse_id=1,is_active=True) for i,n,parent,category in [(1,'茉莉龙井',10,'高维'),(2,'青提',10,'高维'),(3,'柠檬',10,'高维'),(4,'可乐',10,'高维'),(5,'茉莉龙井',20,'其他分类')]]
    import os
    with sqlite3.connect(os.environ['DATABASE_URL'].removeprefix('sqlite:///')) as c:
        c.execute('DELETE FROM products');c.executemany('INSERT INTO products VALUES(?,?,?,?,0)',[(10,'高维',None,1),(20,'其他分类',None,1)]+[(p['id'],p['name'],p['parent_id'],0) for p in products])
    path.write_text(json.dumps({'aliases':[dict(type='MODEL_ALIASES',alias='正品高维',standard='高维',category_id=10,status='active',scope='GLOBAL')],'audit':[],'brand_candidates':[]}))
    return products
@pytest.mark.parametrize('space',['',' '])
@pytest.mark.parametrize('symbol',['×','x','X','*'])
def test_category_count_before_flavor(catalog,symbol,space):
    from order_parser import OrderParser
    raw='正品高维'+symbol+'1'+space+'茉莉龙井\n备选口味：青提'+CONTACT
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert parsed['items']==[('品类(高维)口味(茉莉龙井)',1)],parsed

@pytest.mark.parametrize('symbol',['×','x','X','*'])
def test_suffix_count_unchanged(catalog,symbol):
    from order_parser import OrderParser
    parsed=OrderParser.extract_order_info('正品高维 茉莉龙井'+symbol+'2\n备选口味：青提'+CONTACT,catalog)
    assert parsed['items']==[('品类(高维)口味(茉莉龙井)',2)],parsed

@pytest.mark.parametrize('symbol',['×','x','X','*'])
def test_compact_uncertain_flavor_reuses_scoped_choices(catalog,symbol):
    from order_parser import OrderParser
    from typo_confirmation import local_result
    raw='正品高维'+symbol+'1柠檬可乐\n备选口味：青提'+CONTACT
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert parsed['items']==[('品类(高维)口味(柠檬可乐)',1)],parsed
    assert not parsed.get('quantity_scope_errors'),parsed
    result=local_result(parsed,raw,catalog)
    assert result is not None,parsed
    result,choices=result
    assert set(choices[0]['choices'])=={'柠檬','可乐'}
    assert result['alternates'][0]['flavor']=='青提' and len(result['items'])==1
    assert choices[0]['category']=='高维'


def test_unknown_flavor_no_arbitrary_choices_or_learning(catalog):
    from order_parser import OrderParser
    from typo_confirmation import local_result
    import rule_dictionary
    raw='正品高维x1宇宙不明\n备选口味：青提'+CONTACT
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert parsed['items']==[('品类(高维)口味(宇宙不明)',1)],parsed
    assert local_result(parsed,raw,catalog) is None
    assert not rule_dictionary.read_store()['brand_candidates']

from test_receipt_speed import chain
import asyncio
@pytest.fixture
def gaowei_chain(chain,monkeypatch):
    from models import Product
    import rule_dictionary
    p=chain.pipeline
    with p.db.SessionLocal() as s:
        s.get(Product,p.parent).name='高维';s.get(Product,p.pid).name='茉莉龙井'
        for name in ['青提','柠檬','可乐']:
            s.add(Product(name=name,parent_id=p.parent,regular_price=10,cost_price=2,default_warehouse_id=p.wid,reference_price_state='SET'))
        s.commit()
        ids=[r.id for r in s.query(Product).filter(Product.name.in_(['青提','柠檬','可乐'])).all()]
    for pid in ids:assert p.db.inbound_stock(pid,p.wid,50,unit_cost=2)['success']
    path=rule_dictionary.path();store=json.loads(path.read_text());store['aliases'].append(dict(type='MODEL_ALIASES',alias='正品高维',standard='高维',category_id=p.parent,status='active',scope='GLOBAL'));path.write_text(json.dumps(store))
    monkeypatch.setattr(chain.limiter.state,'reserve',lambda *a,**k:0)
    return chain


def test_exact_standard_full_chain_commits_one_item_no_confirmation(gaowei_chain):
    from models import OrderItem
    import rule_dictionary
    c=gaowei_chain;p=c.pipeline;before=p.quantities()
    c.prepare(1,body='正品高维x1茉莉龙井\n备选口味：青提'+CONTACT)
    result=asyncio.run(c.handle(1))
    assert result['state']=='COMPLETED' and p.counts()['orders']==1
    assert p.quantities()==(before[0]-1,before[1]-1)
    with p.db.SessionLocal() as s:
        item=s.query(OrderItem).one();assert item.product_id==p.pid and item.quantity==1
    assert c.receipt_result(1)['ack_parts']==1
    assert not rule_dictionary.read_store()['brand_candidates']

@pytest.mark.parametrize('action',['confirm','cancel'])
def test_compact_unknown_full_chain_buttons_and_repeated_click(gaowei_chain,monkeypatch,action):
    from sqlalchemy import text
    from telegram import Message
    import rule_dictionary
    c=gaowei_chain;p=c.pipeline;before=p.quantities()
    keyboards=[];original_call=c.telegram.call
    async def capture(endpoint,data,policy=None):
        if data.get('reply_markup'):keyboards.append(data['reply_markup'])
        return await original_call(endpoint,data,policy)
    monkeypatch.setattr(c.telegram,'call',capture)
    c.prepare(1,body='正品高维x1柠檬可乐\n备选口味：青提'+CONTACT)
    assert asyncio.run(c.handle(1))['state']=='NEED_CONFIRMATION'
    assert p.counts()['orders']==0 and p.quantities()==before
    assert not rule_dictionary.read_store()['brand_candidates']
    with p.db.engine.connect() as db:row=db.execute(text('SELECT token,payload FROM telegram_order_drafts ORDER BY created_at DESC LIMIT 1')).mappings().one()
    payload=json.loads(row['payload']);pending=payload['pending_ai_flavor']
    assert set(pending['proposal']['choices'])=={'柠檬','可乐'}
    assert payload['_inline_confirmation']['active']
    assert '需要确认' in c.sent[-1]['text']
    labels=[button.text for row in keyboards[-1].inline_keyboard for button in row]
    assert any('柠檬' in label for label in labels) and any('可乐' in label for label in labels)
    assert any('取消' in label for label in labels)
    message=Message.de_json(dict(message_id=c.sent[-1]['message_id'],date=1,chat={'id':-1000000000001,'type':'group'},text=c.sent[-1]['text']),c.telegram)
    async def edit_markup(**kw):return await c.telegram.call('editMessageReplyMarkup',dict(chat_id=kw['chat_id'],message_id=kw['message_id']))
    monkeypatch.setattr(c.telegram,'edit_message_reply_markup',edit_markup,raising=False)
    selected='0' if action=='confirm' else 'cancel'
    data='ai_flavor:'+selected+'~'+pending['_button_revision']+':'+row['token']
    asyncio.run(c.callback(100,data,message));counts=p.counts();stock=p.quantities()
    asyncio.run(c.callback(101,data,message))
    assert p.counts()==counts and p.quantities()==stock
    assert p.counts()['orders']==(1 if action=='confirm' else 0)
    candidates=rule_dictionary.read_store()['brand_candidates']
    if action=='confirm':
        assert len(candidates)==1 and candidates[0]['alias']=='柠檬可乐' and candidates[0]['status']=='pending'
        assert candidates[0]['customer_confirmations'][0]['category_id']==p.parent
    else:assert not candidates and stock==before


def test_numeric_catalog_flavor_with_spaced_prefix_keeps_identity(catalog):
    from category_evidence import prepare
    import rule_dictionary
    products=catalog+[dict(catalog[0],id=6,name='100号')]
    prepared=prepare('正品高维x1 100号',products,rule_dictionary.read_store()['aliases'])
    assert '口味(100号)×1' in prepared and '品类：高维' in prepared


def test_backup_label_does_not_strip_real_flavor_prefix():
    from order_structure import fulfillment_parts
    assert fulfillment_parts('备选口味：青提',('青提',))[1]=='青提'
    assert fulfillment_parts('备选：口味青提',('口味青提',))[1]=='口味青提'
