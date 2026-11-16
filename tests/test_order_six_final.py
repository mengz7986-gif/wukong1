"""Six requested order fixes, sanitized source examples and no production data."""
import asyncio,json,sqlite3,sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_regressions import isolated_rules

@pytest.fixture
def catalog(isolated_rules):
    _,base=isolated_rules
    specs=[(1,'茉莉龙井',10,'新版砖'),(2,'椰子水',20,'鸭嘴'),(3,'百香果西柚',30,'鸭嘴二代'),(4,'柠檬',20,'鸭嘴'),(5,'可乐',20,'鸭嘴'),(6,'柠檬',30,'鸭嘴二代')]
    products=[dict(id=i,name=n,parent_id=c,parent_name=cn,default_warehouse_id=1,is_active=True) for i,n,c,cn in specs]
    import os
    with sqlite3.connect(os.environ['DATABASE_URL'].removeprefix('sqlite:///')) as c:
        c.execute('DELETE FROM products')
        c.executemany('INSERT INTO products VALUES(?,?,?,?,0)',[(10,'新版砖',None,1),(20,'鸭嘴',None,1),(30,'鸭嘴二代',None,1)]+[(p['id'],p['name'],p['parent_id'],0) for p in products])
    return products

CONTACT='\n姓名：测试\n手机号：13800138000\n地址：北京市朝阳区测试路8号'

def test_empty_backup_remark_separated(catalog):
    from order_parser import OrderParser
    from order_structure import structure_order_text
    raw='品类：新版砖\n口味：茉莉龙井×1\n备选：\n共1支\nKfc记账'+CONTACT
    structure=structure_order_text(raw,catalog)
    assert 'Kfc记账' not in structure.text and 'Kfc记账' in structure.customer_warehouse_notes
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert len(parsed['items'])==1 and parsed['items'][0][1]==1,parsed
    assert 'Kfc' not in str(parsed['items'])
    assert not parsed['quantity_scope_errors'],parsed


def test_two_sections_global_chinese_total(catalog):
    from order_parser import OrderParser
    raw='鸭嘴：椰子水×1\n备选：奇异果、白雾红尘\n\n鸭嘴二代：百香果西柚×1\n备选：茉莉龙井、锡兰柠檬茶\n\n共两只'+CONTACT
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert len(parsed['items'])==2 and sum(q for _,q in parsed['items'])==2,parsed
    assert not parsed['quantity_scope_errors'],parsed
    assert parsed['declared_total']==2,parsed
    assert not parsed.get('section_subtotals'),parsed


@pytest.mark.parametrize('label',['共两只','共2支','总计2支','合计2件','一共二支'])
def test_totals_do_not_borrow_last_category(catalog,label):
    from catalog_quantity_scopes import analyze
    result=analyze('品类：鸭嘴\n椰子水×1\n品类：鸭嘴二代\n百香果西柚×1\n'+label,products=catalog)
    assert result.declared_total==2 and not result.section_subtotals and not result.errors,result


def test_reliable_two_choices_local_and_category_isolation(catalog):
    from typo_confirmation import local_result
    order={'items':[('品类(鸭嘴)口味(柠檬可乐)',1)]}
    result,choices=local_result(order,'鸭嘴：柠檬可乐×1'+CONTACT,catalog)
    assert choices[0]['choices']==['柠檬','可乐']
    assert choices[0]['product_ids']['柠檬']==[4]
    assert local_result(order,'未知分类：柠檬可乐×1'+CONTACT,catalog) is None


def test_fuzzy_cannot_record_without_customer_click(catalog):
    from bot import OrderBot
    bot=OrderBot.__new__(OrderBot)
    product,error=bot._resolve_product_for_order('品类(鸭嘴)口味(柠檬可乐)',catalog)
    assert product is None and error
    product,error=bot._resolve_product_for_order('品类(鸭嘴)口味(茉莉龙井)',catalog)
    assert product is None


def test_unconfirmed_ai_cannot_create_candidate(catalog):
    import flavor_aliases,rule_dictionary
    product=catalog[3]
    assert flavor_aliases.learn('柠檬可乐',product,.99,catalog,event='commit') is None
    assert flavor_aliases.learn('柠檬可乐',product,.99,catalog,origin='CUSTOMER_CONFIRMED_AI',event='commit') is None
    assert not rule_dictionary.read_store().get('brand_candidates')


def proof(product,raw='柠檬可乐'):
    return dict(category=product['parent_name'],raw_flavor=raw,standard=product['name'],product_ids=[product['id']],revision='abcd1234',callback_ref='a'*64,source_ref='b'*64)


def test_confirmed_mapping_scoped_rejected_and_idempotent(catalog):
    import flavor_aliases,rule_dictionary
    product=catalog[3];evidence=proof(product)
    row=flavor_aliases.learn('柠檬可乐',product,1,catalog,origin='CUSTOMER_CONFIRMED_AI',event='commit',confirmation=evidence)
    assert row['status']=='pending' and row['category_id']==20 and row['product_id']==4
    assert row['customer_confirmations'][0]['callback_ref']=='a'*64
    assert flavor_aliases.learn('柠檬可乐',product,1,catalog,origin='CUSTOMER_CONFIRMED_AI',event='commit',confirmation=evidence)['count']==1
    flavor_aliases.review({'id':row['id'],'action':'reject'},'test',catalog)
    assert flavor_aliases.learn('柠檬可乐',product,1,catalog,origin='CUSTOMER_CONFIRMED_AI',event='other',confirmation=evidence) is None
    from ai_flavor_decision import proposals
    result=dict(items=[dict(category='鸭嘴',raw_category='鸭嘴',flavor='柠檬可乐',raw_flavor='柠檬可乐',quantity=1)])
    choices=proposals(result,'鸭嘴 柠檬可乐×1',catalog)[0]['choices']
    assert choices==['可乐']


def test_failure_format_preserves_original_goods(catalog):
    from bot import OrderBot
    preview=dict(items=[dict(parent_name='鸭嘴',product_name='可乐',quantity=1)],recipient_name='测试',recipient_phone='13800138000',recipient_address='北京市朝阳区测试路8号',total_amount=0,grand_total=0)
    error="❌ 商品 '品类(鸭嘴)口味(柠檬)' TG机器人暂不出货\n该商品已到达预警库存，暂不出货（当前 2，预警 3）"
    text=OrderBot._agent_group_order_text(preview,error=error)
    assert '鸭嘴：可乐×1' in text
    assert '失败原因：商品 鸭嘴：柠檬 该口味已缺货' in text
    assert not any(w in text for w in ['TG机器人','预警','当前库存'])
    assert text.count('失败原因：')==1

from test_receipt_speed import chain

def prepare_ambiguous(chain,monkeypatch):
    from models import Product
    p=chain.pipeline
    with p.db.SessionLocal() as s:
        other=Product(name='苹果',parent_id=p.parent,regular_price=10,cost_price=2,default_warehouse_id=p.wid,reference_price_state='SET')
        s.add(other);s.commit();other_id=other.id
    assert p.db.inbound_stock(other_id,p.wid,50,unit_cost=2)['success']
    monkeypatch.setattr(chain.limiter.state,'reserve',lambda *a,**k:0)
    body='甲牌\n薄荷苹果×1\n姓名：测试\n手机：13800138000\n地址：北京市朝阳区测试路8号'
    chain.prepare(1,body=body)
    result=asyncio.run(chain.handle(1))
    from sqlalchemy import text
    with p.db.engine.connect() as c:
        row=c.execute(text('SELECT token,payload FROM telegram_order_drafts ORDER BY created_at DESC LIMIT 1')).mappings().one()
    payload=json.loads(row['payload']);pending=payload['pending_ai_flavor']
    assert payload['_inline_confirmation']['active'] is True
    from telegram import Message
    message=Message.de_json(dict(message_id=chain.sent[-1]['message_id'],date=1,chat={'id':-1000000000001,'type':'group'},text=chain.sent[-1]['text']),chain.telegram)
    async def edit_markup(**kw):
        return await chain.telegram.call('editMessageReplyMarkup',dict(chat_id=kw['chat_id'],message_id=kw['message_id']))
    monkeypatch.setattr(chain.telegram,'edit_message_reply_markup',edit_markup,raising=False)
    return row['token'],pending,message


def test_real_durable_button_confirm_and_repeat_one_business_effect(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline
    assert p.counts()['orders']==0
    assert set(pending['proposal']['choices'])=={'薄荷','苹果'}
    before=p.quantities();data='ai_flavor:0~'+pending['_button_revision']+':'+token
    asyncio.run(chain.callback(100,data,message))
    assert p.counts()['orders']==1 and p.quantities()==(before[0]-1,before[1]-1)
    counts=p.counts();stock=p.quantities()
    asyncio.run(chain.callback(101,data,message))
    assert p.counts()==counts and p.quantities()==stock
    import rule_dictionary
    candidates=[r for r in rule_dictionary.read_store()['brand_candidates'] if r.get('type')=='EXPLICIT_FLAVOR']
    assert len(candidates)==1 and candidates[0]['status']=='pending'
    assert candidates[0]['customer_confirmations'][0]['callback_ref']


def test_durable_cancel_only_selected_draft(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline;old=p.quantities()
    chain.prepare(2,body='甲牌\n薄荷苹果×1\n姓名：测试\n手机：13800138000\n地址：北京市朝阳区测试路9号')
    asyncio.run(chain.handle(2))
    asyncio.run(chain.callback(200,'ai_flavor:cancel~'+pending['_button_revision']+':'+token,message))
    assert p.counts()['orders']==0 and p.quantities()==old
    from sqlalchemy import text
    with p.db.engine.connect() as c:
        rows=list(c.execute(text('SELECT token,status,payload FROM telegram_order_drafts ORDER BY created_at')).mappings())
    assert rows[0]['status']!='pending' and 'pending_ai_flavor' in json.loads(rows[1]['payload'])
    import rule_dictionary
    assert not rule_dictionary.read_store()['brand_candidates']


def test_scoped_approval_only_changes_selected_category(catalog):
    import flavor_aliases
    from deepseek_resolver import resolve_item
    product=catalog[3];row=flavor_aliases.learn('柠檬可乐',product,1,catalog,origin='CUSTOMER_CONFIRMED_AI',event='commit',confirmation=proof(product))
    raw=dict(category='鸭嘴',raw_category='鸭嘴',flavor='柠檬可乐',raw_flavor='柠檬可乐')
    assert resolve_item(raw,catalog)['resolved_flavor'] is None
    flavor_aliases.review({'id':row['id'],'action':'approve'},'test',catalog)
    assert resolve_item(raw,catalog)['resolved_flavor']=='柠檬'
    assert resolve_item(dict(raw,category='鸭嘴二代',raw_category='鸭嘴二代'),catalog)['resolved_flavor'] is None


def test_ai_flags_cannot_forge_customer_learning_proof(catalog):
    from brand_aliases import learn_validated
    item=dict(category='鸭嘴',raw_category='鸭嘴',flavor='柠檬',raw_flavor='柠檬可乐',_customer_confirmed=True,_customer_confirmation=proof(catalog[3]))
    preview={}
    learn_validated({'items':[item]},preview,catalog)
    assert '_customer_confirmation' not in preview['_learning_evidence']['result']['items'][0]


def test_exact_kfc_catalog_item_is_not_a_banned_word(catalog):
    from order_parser import OrderParser
    product=dict(catalog[0],name='Kfc记账')
    parsed=OrderParser.extract_order_info('新版砖\nKfc记账×1\n共1支'+CONTACT,[product])
    assert len(parsed['items'])==1 and parsed['items'][0][1]==1
    assert 'Kfc记账' in str(parsed['items'])


def test_source_with_explicit_category_subtotal_keeps_subtotal(catalog):
    from order_parser import OrderParser
    raw='鸭嘴1支：椰子水×1\n鸭嘴二代1支：百香果西柚×1\n共2支'+CONTACT
    parsed=OrderParser.extract_order_info(raw,catalog)
    assert not parsed['quantity_scope_errors'],parsed
    assert len(parsed['section_subtotals'])==2


def test_committed_two_category_order_with_chinese_global_total(chain,monkeypatch):
    from models import Product
    p=chain.pipeline
    with p.db.SessionLocal() as s:
        parent=Product(name='乙牌',is_parent=True,regular_price=10,default_warehouse_id=p.wid,reference_price_state='SET');s.add(parent);s.flush()
        product=Product(name='苹果',parent_id=parent.id,regular_price=10,cost_price=2,default_warehouse_id=p.wid,reference_price_state='SET');s.add(product);s.commit();pid=product.id
    assert p.db.inbound_stock(pid,p.wid,50,unit_cost=2)['success']
    monkeypatch.setattr(chain.limiter.state,'reserve',lambda *a,**k:0)
    before=p.quantities();chain.prepare(1,body='甲牌：薄荷×1\n乙牌：苹果×1\n共两只'+CONTACT)
    result=asyncio.run(chain.handle(1))
    assert result['state']=='COMPLETED' and p.counts()['orders']==1
    assert p.quantities()==(before[0]-2,before[1]-2)
    assert chain.receipt_result(1)['ack_at']


def test_pending_confirmation_stock_change_rechecked(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline
    from models import WarehouseStock,InventoryBatch
    selected_id=pending['proposal']['product_ids'][pending['proposal']['choices'][0]][0]
    with p.db.SessionLocal() as s:
        for stock in s.query(WarehouseStock).filter_by(product_id=selected_id):stock.quantity=0
        for lot in s.query(InventoryBatch).filter_by(product_id=selected_id):lot.remain_qty=0
        s.commit()
    before=p.quantities();asyncio.run(chain.callback(400,'ai_flavor:0~'+pending['_button_revision']+':'+token,message))
    assert p.counts()['orders']==0 and p.quantities()==before
    import rule_dictionary
    assert not rule_dictionary.read_store()['brand_candidates']


def test_concurrent_clicks_use_existing_serial_claim_and_single_commit(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline;before=p.quantities()
    from telegram import Update
    from telegram_queue_runtime import Runtime
    from types import SimpleNamespace
    data='ai_flavor:0~'+pending['_button_revision']+':'+token
    for n in [300,301]:
        payload={'update_id':n,'callback_query':{'id':str(n),'from':{'id':123456789,'is_bot':False,'first_name':'测试'},'chat_instance':'offline','data':data,'message':message.to_dict()}}
        assert p.queue.receive(-1000000000001,-n,123456789,payload)
    first=p.queue.claim(lane='callback');assert first
    assert p.queue.claim(lane='callback') is None
    runtime=Runtime.__new__(Runtime);runtime.queue=p.queue;runtime.owner=chain.bot;runtime.executions={}
    async def process(job):
        context=SimpleNamespace(user_data={},chat_data={},bot_data={},bot=chain.telegram,_rainbow_queue_resume=True)
        try:
            await runtime.guarded_handler(job,Update.de_json(job['decoded_payload'],chain.telegram),context,True)
            assert p.queue.finish(job,'COMPLETED')
        finally:p.release(job)
    asyncio.run(process(first));second=p.queue.claim(lane='callback');assert second;asyncio.run(process(second))
    assert p.counts()['orders']==1 and p.quantities()==(before[0]-1,before[1]-1)


def test_pending_click_uses_current_price(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline
    from models import Product,Order,OrderItem
    pid=pending['proposal']['product_ids'][pending['proposal']['choices'][0]][0]
    with p.db.SessionLocal() as s:s.get(Product,pid).regular_price=17;s.commit()
    asyncio.run(chain.callback(500,'ai_flavor:0~'+pending['_button_revision']+':'+token,message))
    with p.db.SessionLocal() as s:
        assert float(s.query(Order).one().total_amount)==17
        assert float(s.query(OrderItem).one().unit_price)==17


def test_removed_catalog_choice_cannot_commit(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline
    from models import Product
    pid=pending['proposal']['product_ids'][pending['proposal']['choices'][0]][0]
    with p.db.SessionLocal() as s:s.get(Product,pid).is_active=False;s.commit()
    before=p.quantities();asyncio.run(chain.callback(510,'ai_flavor:0~'+pending['_button_revision']+':'+token,message))
    assert p.counts()['orders']==0 and p.quantities()==before


def test_wrong_revision_cannot_consume_order(chain,monkeypatch):
    token,pending,message=prepare_ambiguous(chain,monkeypatch);p=chain.pipeline
    before=p.quantities();asyncio.run(chain.callback(520,'ai_flavor:0~deadbeef:'+token,message))
    assert p.counts()['orders']==0 and p.quantities()==before
    asyncio.run(chain.callback(521,'ai_flavor:0~'+pending['_button_revision']+':'+token,message))
    assert p.counts()['orders']==1


def test_unconfirmed_recognition_diagnostics_never_become_alias_candidates(catalog):
    import failure_analysis
    assert failure_analysis.local_candidates('鸭嘴 柠檬可乐1',catalog)==[]
    with pytest.raises(ValueError,match='客户按钮'):
        failure_analysis._proposal(dict(kind='FLAVOR_ALIAS',alias='柠檬可乐',product_id=4),{'original':'鸭嘴 柠檬可乐1'},catalog)
    assert failure_analysis.save_confirmation_suggestions('unused',[],catalog) is None


def test_flavor_confirmation_does_not_disappear_with_backup_notes(catalog):
    from typo_confirmation import local_result
    order={'items':[('品类(鸭嘴)口味(柠檬可乐)',1)]}
    result,choices=local_result(order,'鸭嘴：柠檬可乐×1\n备选：无\n共1支'+CONTACT,catalog)
    assert choices[0]['choices']==['柠檬','可乐']
    assert result['alternates']==[] and len(result['items'])==1


def test_unresolved_backup_reuses_same_scoped_confirmation(catalog):
    from typo_confirmation import local_result
    result,choices=local_result({'items':[('品类(鸭嘴)口味(椰子水)',1)]},'鸭嘴：椰子水×1\n备选：柠檬可乐\n共1支'+CONTACT,catalog)
    assert choices[0]['kind']=='alternate' and choices[0]['choices']==['柠檬','可乐']
    assert len(result['items'])==len(result['alternates'])==1


def test_primary_choice_preserves_backup_link(catalog):
    from typo_confirmation import local_result
    from ai_flavor_decision import authorize
    result,choices=local_result({'items':[('品类(鸭嘴)口味(柠檬可乐)',1)]},'鸭嘴：柠檬可乐×1\n备选：椰子水'+CONTACT,catalog)
    selected=authorize(result,choices[0],'可乐')
    assert selected['alternates'][0]['for']=='可乐' and selected['alternates'][0]['flavor']=='椰子水'


def test_backup_button_commits_only_selected_in_stock_product(chain,monkeypatch):
    from models import Product,WarehouseStock,InventoryBatch,OrderItem
    from sqlalchemy import text
    from telegram import Message
    p=chain.pipeline
    with p.db.SessionLocal() as s:
        for name in ['苹果','青提']:
            product=Product(name=name,parent_id=p.parent,regular_price=10,cost_price=2,default_warehouse_id=p.wid,reference_price_state='SET');s.add(product);s.flush()
        s.commit();ids=[r.id for r in s.query(Product).filter(Product.name.in_(['苹果','青提'])).all()]
        for stock in s.query(WarehouseStock).filter_by(product_id=p.pid):stock.quantity=0
        for lot in s.query(InventoryBatch).filter_by(product_id=p.pid):lot.remain_qty=0
        s.commit()
    for pid in ids:assert p.db.inbound_stock(pid,p.wid,50,unit_cost=2)['success']
    monkeypatch.setattr(chain.limiter.state,'reserve',lambda *a,**k:0)
    chain.prepare(1,body='甲牌：薄荷×1\n备选：苹果青提\n共1支'+CONTACT)
    asyncio.run(chain.handle(1));assert p.counts()['orders']==0
    with p.db.engine.connect() as c:row=c.execute(text('SELECT token,payload FROM telegram_order_drafts ORDER BY created_at DESC LIMIT 1')).mappings().one()
    pending=json.loads(row['payload'])['pending_ai_flavor'];assert pending['proposal']['kind']=='alternate'
    assert chain.sent[-1]['text'].startswith('请选择正确口味。')
    assert json.loads(row['payload'])['_inline_confirmation']['active'] is True
    message=Message.de_json(dict(message_id=chain.sent[-1]['message_id'],date=1,chat={'id':-1000000000001,'type':'group'},text=chain.sent[-1]['text']),chain.telegram)
    async def edit_markup(**kw):return await chain.telegram.call('editMessageReplyMarkup',dict(chat_id=kw['chat_id'],message_id=kw['message_id']))
    monkeypatch.setattr(chain.telegram,'edit_message_reply_markup',edit_markup,raising=False)
    before=p.quantities();asyncio.run(chain.callback(530,'ai_flavor:0~'+pending['_button_revision']+':'+row['token'],message))
    assert p.counts()['orders']==1 and p.quantities()==(before[0]-1,before[1]-1)
    with p.db.SessionLocal() as s:assert s.query(OrderItem).one().quantity==1
    import rule_dictionary
    candidates=rule_dictionary.read_store()['brand_candidates']
    assert len(candidates)==1 and candidates[0]['alias']=='苹果青提' and candidates[0]['status']=='pending'
    assert candidates[0]['customer_confirmations'][0]['category_id']==p.parent


@pytest.mark.parametrize('note',['备注：椰子水×2','仓库：Kfc','发货备注：多放2包纸巾','记账说明：本次金额123元'])
def test_labelled_notes_never_add_products_or_quantities(catalog,note):
    from order_parser import OrderParser
    parsed=OrderParser.extract_order_info('鸭嘴：椰子水×1\n共1支\n'+note+CONTACT,catalog)
    assert len(parsed['items'])==1 and sum(q for _,q in parsed['items'])==1,parsed
    assert not parsed['quantity_scope_errors'],parsed
    import unicodedata
    assert unicodedata.normalize('NFKC',note) in [unicodedata.normalize('NFKC',n) for n in parsed['customer_warehouse_notes']]
