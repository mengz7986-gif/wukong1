"""Fixed JPEG geometry, exact catalog ownership, scoped cache and existing senders."""
import ast,asyncio,contextlib,copy,hashlib,io,json,os,sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))
import inventory_preview as v

@pytest.fixture(autouse=True)
def font(monkeypatch):
    extra=os.environ.get('INVENTORY_TEST_FONT')
    if extra:monkeypatch.setattr(v,'FONT_CANDIDATES',(extra,)+v.FONT_CANDIDATES)
    assert Path(v._font_path()).exists()


def snapshot(wid='1',counts=(8,11,5),long=False):
    cats=[]
    for c,n in enumerate(counts):
        items=tuple(v.InventoryItem(f'{c}-{i}',('测试长口味茉莉龙井柠檬可乐配方'+str(i)) if long else ('100号' if i==0 else '清新薄荷'+str(i)),(0,25,100)[i%3],2 if i%7==0 else 0) for i in range(n))
        cats.append(v.InventoryCategory(str(c),'测试品类'+str(c),items))
    return v.InventorySnapshot(tuple(cats),wid,{'1':'一仓','2':'二仓','3':'三仓'}.get(wid,'测试仓'))


def identities(s):return Counter((c.id,i.id) for c in s.categories for i in c.items)
def plan_identities(p):return Counter((c.id,i.id) for page in p['pages'] for c,_,_,_ in page for i in c.items)

@pytest.mark.parametrize('wid',['1','2','3'])
def test_actual_scale_shape_uniform_fonts_and_no_overlap(wid):
    counts=(33,25,25,22,21,15,12,12,11,7) if wid=='1' else (34,22,21,20,20,16,15,13,12,10) if wid=='2' else (33,24,22,19,13,10,5)
    s=snapshot(wid,counts);before=copy.deepcopy(s);p=v.inventory_page_plan(s,{})
    assert (p['width'],p['height'])==(3200,3200) and s==before
    assert v.LAYOUT_STYLE['item_font']==36 and v.LAYOUT_STYLE['category_font']==58 and v.LAYOUT_STYLE['card_background_mix']==0.14
    assert plan_identities(p)==identities(s)
    for page in p['pages']:
        boxes=[]
        for c,col,y,h in page:
            x=48+col*(p['card_width']+24);box=(x,y,x+p['card_width'],y+h)
            assert x>=48 and box[2]<=3200-48 and y>=300 and y+h<=p['body_end']
            for other in boxes:assert box[2]<=other[0] or other[2]<=box[0] or box[3]<=other[1] or other[3]<=box[1]
            boxes.append(box)


def test_overflow_splits_whole_items_and_keeps_category_scope():
    s=snapshot('1',(300,180,120),long=True);p=v.inventory_page_plan(s,{})
    assert len(p['pages'])>1 and plan_identities(p)==identities(s)
    assert sum(len(c.items) for page in p['pages'] for c,_,_,_ in page)==600
    assert all(c.name.startswith('测试品类'+c.id) for page in p['pages'] for c,_,_,_ in page)


def test_empty_and_empty_category_are_valid_fixed_pages(tmp_path):
    for n,s in enumerate([snapshot('1',()),snapshot('2',(0,))]):
        p=v.inventory_page_plan(s,{});path=tmp_path/f'empty{n}.jpg';v._render(s,{},path,plan=p)
        with Image.open(path) as im:assert im.format=='JPEG' and im.size==(3200,3200)


def test_real_jpeg_text_bounds_hidden_stock_and_legal_numeric_flavor(tmp_path,monkeypatch):
    s=v.InventorySnapshot((v.InventoryCategory('1','高维',(
        v.InventoryItem('1','100号',246813),v.InventoryItem('2','超长口味中文与VeryLongFlavorName组合'*3,25,2))),),'2','二仓')
    calls=[];original=ImageDraw.ImageDraw.text
    def record(draw,xy,value,*a,**kw):
        bbox=draw.textbbox(xy,value,font=kw.get('font'),stroke_width=kw.get('stroke_width',0));calls.append((value,bbox));return original(draw,xy,value,*a,**kw)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',record)
    settings=dict(inventory_image_show_quantity='1');p=v.inventory_page_plan(s,settings)
    for i in range(len(p['pages'])):
        path=tmp_path/f'page{i}.jpg';v._render(s,settings,path,page_index=i,plan=p)
        with Image.open(path) as im:assert im.size==(3200,3200) and im.format=='JPEG'
    assert any(t=='100号' for t,_ in calls) and all('246813' not in t and ' · 25' not in t for t,_ in calls)
    assert all(0<=b[0]<=b[2]<=3200 and 0<=b[1]<=b[3]<=3200 for _,b in calls)


def test_long_notes_continue_without_clipping_or_shrinking(tmp_path):
    text='请核对商品并保留开箱视频。'*45
    settings=dict(inventory_image_description=text,inventory_image_footer_note=text,inventory_image_menu_note='备注'*100)
    p=v.inventory_page_plan(snapshot(counts=(1,)),settings)
    assert len(p['pages'])>1
    assert ''.join(p['module_lines'][0])==text
    assert sum(len(p['module_lines'][0][i*4:(i+1)*4]) for i in range(len(p['pages'])))==len(p['module_lines'][0])
    assert sum(len(p['module_lines'][1][i*6:(i+1)*6]) for i in range(len(p['pages'])))==len(p['module_lines'][1])


def test_status_thresholds_and_background_assignment_unchanged():
    settings=dict(inventory_image_red_max='0',inventory_image_green_min='50')
    assert [v.stock_marker_color(n,settings) for n in [0,25,50]]==['#dc2626','#eab308','#16a34a']
    assert v._background_name('1')=='inventory-long-march-snow-v1.png'
    assert v._background_name('2')=='inventory-long-march-grassland-v1.png'
    assert v._background_name('3')=='inventory-founding-ceremony-v1.png'

@pytest.fixture
def light_render(monkeypatch):
    calls=[]
    def render(s,settings,path,*,page_index=0,plan=None):
        calls.append((s.warehouse_id,page_index));color=(int(s.categories[0].items[0].stock)%256,0,0) if s.categories and s.categories[0].items else (0,0,0)
        Image.new('RGB',(16,16),color).save(path,'JPEG')
    monkeypatch.setattr(v,'_render',render)
    return calls


def test_cache_hits_and_stock_layout_opacity_changes_invalidate(tmp_path,light_render,monkeypatch):
    s=snapshot(counts=(2,));first,hit=v.render_inventory_image_pages_bytes(s,{},tmp_path);assert not hit
    assert v.render_inventory_image_pages_bytes(s,{},tmp_path)==(first,True)
    changed=v.InventorySnapshot((v.InventoryCategory('0','测试品类0',(v.InventoryItem('0-0','100号',99),v.InventoryItem('0-1','清新薄荷1',25))),),'1','一仓')
    assert not v.render_inventory_image_pages_bytes(changed,{},tmp_path)[1]
    monkeypatch.setattr(v,'LAYOUT_STYLE',dict(v.LAYOUT_STYLE,card_background_mix=0.13))
    assert not v.render_inventory_image_pages_bytes(changed,{},tmp_path)[1]
    monkeypatch.setattr(v,'LAYOUT_STYLE',dict(v.LAYOUT_STYLE,gap=26))
    assert not v.render_inventory_image_pages_bytes(changed,{},tmp_path)[1]
    assert len(light_render)==4


def test_background_bytes_replacement_invalidates_cache(tmp_path,light_render,monkeypatch):
    # Isolated fixture asset; never edits the project background.
    import types
    image_path=tmp_path/'asset.png';Image.new('RGB',(8,8),'red').save(image_path)
    key_fn=v._cache_key
    monkeypatch.setattr(v,'_background_name',lambda wid:str(image_path))
    s=snapshot(counts=(1,));key1=key_fn(s,{})
    v.render_inventory_image_pages_bytes(s,{},tmp_path/'cache')
    Image.new('RGB',(8,8),'blue').save(image_path)
    assert key1!=key_fn(s,{}) and not v.render_inventory_image_pages_bytes(s,{},tmp_path/'cache')[1]


def test_failed_new_generation_preserves_previous_complete_cache(tmp_path,light_render,monkeypatch):
    s=snapshot(counts=(1,));old,hit=v.render_inventory_image_pages_bytes(s,{},tmp_path);before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.suffix in ['.json','.jpg']}
    original=v._render
    def fail(*a,**k):raise OSError('isolated injected write failure')
    monkeypatch.setattr(v,'_render',fail)
    with pytest.raises(OSError):v.render_inventory_image_pages_bytes(s,{'inventory_image_title':'新标题'},tmp_path)
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.suffix in ['.json','.jpg']}
    monkeypatch.setattr(v,'_render',original)
    assert v.render_inventory_image_pages_bytes(s,{},tmp_path)==(old,True)


def test_concurrent_readers_share_one_complete_generation(tmp_path,light_render):
    s=snapshot(counts=(2,))
    with ThreadPoolExecutor(max_workers=4) as ex:rows=list(ex.map(lambda n:v.render_inventory_image_pages_bytes(s,{},tmp_path),range(8)))
    assert len(light_render)==1 and sum(not hit for _,hit in rows)==1
    assert all(pages==rows[0][0] for pages,_ in rows)


def test_invalidation_is_warehouse_scoped_and_removes_all_pages(tmp_path,light_render):
    s=snapshot('1',(120,),long=True);v.render_inventory_image_pages_bytes(s,{},tmp_path)
    v.render_inventory_image_pages_bytes(snapshot('3',(2,)),{},tmp_path)
    suffix=hashlib.sha256(b'3').hexdigest()[:12];other={p.name:p.read_bytes() for p in tmp_path.glob('inventory-preview-'+suffix+'*')}
    unrelated=tmp_path/'do-not-remove.txt';unrelated.write_text('KEEP')
    assert v.invalidate_warehouse_cache('1',tmp_path)>=2
    assert other=={p.name:p.read_bytes() for p in tmp_path.glob('inventory-preview-'+suffix+'*')} and unrelated.read_text()=='KEEP'
    assert v.clear_inventory_cache(tmp_path)>=2 and unrelated.exists()


@pytest.mark.parametrize('invalid',[0,-1,True,'1',2])
def test_page_selection_rejects_invalid_values(tmp_path,light_render,invalid):
    with pytest.raises(ValueError):v.render_inventory_image_page_bytes(snapshot(counts=(1,)),{},tmp_path,invalid)


def bot_harness(monkeypatch,pages):
    source=Path(__file__).resolve().parents[1]/'legacy_app/bot.py';tree=ast.parse(source.read_text())
    cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='OrderBot')
    names={'_send_inventory_gallery','_send_inventory_preview_images'};methods=[n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),ast.ClassDef(name='Harness',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[]);ast.fix_missing_locations(module)
    transactions=[]
    @contextlib.contextmanager
    def transaction(key):transactions.append(key);yield
    import logging
    ns=dict(asyncio=asyncio,os=os,__file__=str(source),logger=logging.getLogger('test'),inventory_image_settings=lambda **k:{'inventory_image_enabled':'1'},snapshots_from_stock_rows=v.snapshots_from_stock_rows,inventory_document_transaction=transaction,clean_telegram_text=str)
    exec(compile(module,'inventory_bot_methods','exec'),ns)
    monkeypatch.setattr(v,'render_inventory_image_pages_bytes',lambda s,settings,cache:(tuple(b'JPEG'+str(i).encode() for i in range(pages.get(s.warehouse_id,1))),False))
    class Message:
        def __init__(self):self.groups=[];self.documents=[];self.fail=False
        async def reply_media_group(self,**kw):self.groups.append(kw['media']);
        async def reply_document(self,**kw):self.documents.append(kw)
    message=Message();bot=ns['Harness']();bot._safe_reply=lambda *a:None
    return bot,SimpleNamespace(update_id=123,effective_message=message,message=message),transactions


def test_existing_gallery_batches_all_pages_at_ten_without_new_sender(monkeypatch):
    bot,update,transactions=bot_harness(monkeypatch,{'1':11,'3':2})
    assert asyncio.run(bot._send_inventory_gallery(update,[],[dict(id=1,name='一仓'),dict(id=3,name='三仓')]))
    assert [len(x) for x in update.message.groups]==[10,3] and len(transactions)==2


def test_legacy_document_loop_uses_distinct_page_transaction_ids(monkeypatch):
    bot,update,transactions=bot_harness(monkeypatch,{'1':3})
    rows=[dict(warehouse_id=1,warehouse_name='一仓',parent_id=1,parent_name='分类',product_id=1,product_name='口味',quantity=1)]
    assert asyncio.run(bot._send_inventory_preview_images(update,rows))
    assert len(update.message.documents)==3 and len(set(transactions))==3
    assert [x['filename'] for x in update.message.documents]==['一仓-实时库存-第1页.jpg','一仓-实时库存-第2页.jpg','一仓-实时库存-第3页.jpg']


def test_gallery_timeout_not_retried_or_fanned_out(monkeypatch):
    bot,update,transactions=bot_harness(monkeypatch,{'1':3});calls=[]
    async def fail(**kw):calls.append(1);raise TimeoutError('uncertain')
    update.message.reply_media_group=fail
    assert asyncio.run(bot._send_inventory_gallery(update,[],[dict(id=1,name='一仓')])) is False
    assert calls==[1] and len(transactions)==1


def test_admin_jpeg_page_headers_and_auth_are_preserved(tmp_path,monkeypatch):
    import inventory_admin as admin
    from fastapi import FastAPI,Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    monkeypatch.setattr(admin,'ROOT',tmp_path);seen=[]
    jpeg=io.BytesIO();Image.new('RGB',(16,16)).save(jpeg,'JPEG')
    def render(s,settings,cache,page=1):
        seen.append(page)
        if type(page) is not int or not 1<=page<=3:raise ValueError('bad page')
        return jpeg.getvalue(),False,3
    monkeypatch.setattr(v,'render_inventory_image_page_bytes',render)
    async def stock(w):return [dict(warehouse_id=w,warehouse_name='一仓',parent_id=1,parent_name='分类',product_id=1,product_name='口味',quantity=1)]
    app=FastAPI();app.add_middleware(SessionMiddleware,secret_key='isolated-test-key')
    @app.get('/test-login')
    def login(req:Request):req.session.update(admin_logged_in=True,admin_username='admin',inventory_csrf='csrf');return {}
    app.include_router(admin.make_router(None,stock,lambda:True,admin_username=lambda:'admin',warehouse_provider=lambda:[dict(id=1,name='一仓')]))
    with TestClient(app) as client:
        path='/api/warehouses/1/inventory-image/preview';assert client.post(path,json={}).status_code==401
        client.get('/test-login');assert client.post(path,json={}).status_code==403
        result=client.post(path,json={'page':2,'settings':{}},headers={'X-Inventory-CSRF':'csrf'})
        assert result.status_code==200 and result.headers['content-type']=='image/jpeg' and result.headers['X-Inventory-Page']=='2' and result.headers['X-Inventory-Pages']=='3'
        assert client.post(path,json={'page':9},headers={'X-Inventory-CSRF':'csrf'}).status_code==400
    assert seen==[2,9]


def test_legacy_single_jpeg_cache_is_not_reused(tmp_path,light_render):
    suffix=hashlib.sha256(b'1').hexdigest()[:12]
    old=tmp_path/f'inventory-preview-{suffix}.jpg';old.write_bytes(b'OLD BACKGROUND')
    (tmp_path/f'inventory-preview-{suffix}.sha256').write_text('legacy-key')
    pages,hit=v.render_inventory_image_pages_bytes(snapshot(counts=(2,)),{},tmp_path)
    assert not hit and pages[0]!=b'OLD BACKGROUND' and not old.exists() and len(light_render)==1


def test_corrupt_manifest_cannot_point_to_another_generation(tmp_path,light_render):
    s=snapshot(counts=(2,));v.render_inventory_image_pages_bytes(s,{},tmp_path)
    path=next(tmp_path.glob('*.pages.json'));manifest=json.loads(path.read_text());manifest['pages']=[manifest['pages'][0].replace('-p1.jpg','-p99.jpg')];path.write_text(json.dumps(manifest))
    assert not v.render_inventory_image_pages_bytes(s,{},tmp_path)[1] and len(light_render)==2


@pytest.mark.parametrize('failure',['render_second_page','publish_second_page'])
def test_failed_multipage_replacement_never_exposes_mixed_generation(tmp_path,light_render,monkeypatch,failure):
    s=snapshot(counts=(300,),long=True);old,_=v.render_inventory_image_pages_bytes(s,{},tmp_path);assert len(old)>1
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.suffix in ['.json','.jpg']}
    if failure=='render_second_page':
        original=v._render
        def fail(*a,**kw):
            if kw['page_index']==1:raise OSError('injected')
            return original(*a,**kw)
        monkeypatch.setattr(v,'_render',fail)
    else:
        original=v.os.replace
        def fail(src,dest):
            if str(dest).endswith('-p2.jpg'):raise OSError('injected')
            return original(src,dest)
        monkeypatch.setattr(v.os,'replace',fail)
    with pytest.raises(OSError):v.render_inventory_image_pages_bytes(s,{'inventory_image_title':'新版标题'},tmp_path)
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.suffix in ['.json','.jpg']}
    assert not list(tmp_path.glob('.inventory-page-*')) and not list(tmp_path.glob('.inventory-manifest-*'))
