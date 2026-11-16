"""Synthetic, isolated use of the existing database, queue, parser and worker APIs."""
from pathlib import Path
import os,json,time

class Pipeline:
 def __init__(self,path):
  import config
  from models import Database,Warehouse,Product
  self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
  self.secret='four-specials-offline-secret-'+'x'*40
  os.environ.update(SESSION_SECRET_KEY=self.secret,RAINBOW_DEPLOY_DRAIN_PATH=str(self.path/'drain.json'),RAINBOW_CLOUD_ADDRESS_TRUST_PATH=str(self.path/'address-trust.db'),RAINBOW_DICTIONARY_PATH=str(self.path/'dictionary.json'),DATABASE_URL='sqlite:///'+str(self.path/'orders.sqlite3'))
  (self.path/'dictionary.json').write_text(json.dumps({'aliases':[],'audit':[],'brand_candidates':[]}))
  config.BOT_TOKEN='123456789:offline';config.TELEGRAM_BOT_TOKEN=config.BOT_TOKEN;config.SESSION_SECRET_KEY=self.secret;config.ADDRESS_DAILY_MAX_ORDERS=1;config.ADDRESS_DAILY_MAX_UNITS=0;config.UNITS_PER_PACKAGE=6;config.ORDER_AUTO_SPLIT_UNITS=6;config.CATEGORY_SPLIT_LIMITS={}
  self.db=Database(os.environ['DATABASE_URL'])
  from telegram_order_queue import migrate,Queue
  import telegram_accounts
  with self.db.engine.begin() as c:migrate(c);telegram_accounts.migrate(c)
  with self.db.SessionLocal() as s:
   w=Warehouse(name='隔离测试仓',address='测试地址',shipping_fee=0);s.add(w);s.flush()
   parent=Product(name='甲牌',regular_price=10,is_parent=True,default_warehouse_id=w.id,reference_price_state='SET');s.add(parent);s.flush()
   p=Product(name='薄荷',parent_id=parent.id,regular_price=10,cost_price=2,default_warehouse_id=w.id,reference_price_state='SET');s.add(p);s.commit();self.wid=w.id;self.pid=p.id;self.parent=parent.id
  assert self.db.inbound_stock(self.pid,self.wid,10000,unit_cost=2)['success']
  self.user=self.db.create_user('123456789',username='synthetic_user')
  self.queue=Queue(self.db,self.secret)
  self.catalog=[dict(id=self.pid,name='薄荷',parent_id=self.parent,parent_name='甲牌',default_warehouse_id=self.wid,price=10,stock=10000)]
 def payload(self,message=1,qty=1,address='北京市朝阳区建国路88号'):
  return {'update_id':message,'message':{'message_id':message,'date':int(time.time()),'chat':{'id':-1000000000001,'type':'supergroup'},'from':{'id':123456789},'text':f'甲牌 薄荷{qty}\n张三 13800000000\n{address}'}}
 def receive(self,message=1,qty=1,address='北京市朝阳区建国路88号'):
  return self.queue.receive(-1000000000001,message,123456789,self.payload(message,qty,address))
 def create(self,message=1,qty=1,job=None,address='北京市朝阳区建国路88号'):
  execution={k:job[k] for k in ('namespace','chat_id','message_id','lease_token')} if job else None
  return self.db.create_order(self.user.id,[dict(product_id=self.pid,warehouse_id=self.wid,quantity=qty)],'张三','13800000000',address,source_chat_id=-1000000000001,source_message_id=message,source_chat_type='supergroup',execution_info=execution)
 def release(self,job):
  from deployment_drain import release_job
  release_job(job)
 def counts(self):
  from sqlalchemy import text
  with self.db.engine.connect() as c:
   return {t:c.execute(text('SELECT count(*) FROM '+t)).scalar() for t in ['orders','order_items','transactions','stock_movements','telegram_execution_commits']}
 def quantities(self):
  from sqlalchemy import text
  with self.db.engine.connect() as c:
   return tuple(c.execute(text('SELECT (SELECT sum(quantity) FROM warehouse_stock),(SELECT sum(remain_qty) FROM inventory_batches)')).one())
 def close(self):self.db.engine.dispose()
 def add_representative_reference(self):
  """536 synthetic catalog rows and a 7MB history; no production data."""
  from models import Product
  products=[]
  with self.db.SessionLocal() as s:
   for cat in range(1,68):
    parent=Product(name=f'样品牌{cat}',regular_price=1,is_parent=True,default_warehouse_id=self.wid,reference_price_state='SET');s.add(parent);s.flush()
    for index in range(7 if cat<=65 else 6):
     child=Product(name=f'样口味{index}',regular_price=1,parent_id=parent.id,default_warehouse_id=self.wid,reference_price_state='SET');s.add(child);s.flush()
     products.append(dict(id=child.id,name=child.name,parent_id=parent.id,parent_name=parent.name,default_warehouse_id=self.wid,is_active=True,is_archived=False))
   s.commit()
  self.catalog+=products
  aliases=[]
  for i in range(1534):
   p=products[i%len(products)]
   aliases.append(dict(id=f'synthetic-{i}',type='MODEL_ALIASES',alias=f'{p["parent_name"]}叫法{i}',standard=p['parent_name'],category=p['parent_name'],scope='GLOBAL',status='active',readonly=False))
  history=[dict(action='synthetic_audit',at='2026-01-01T00:00:00+00:00',actor='synthetic',padding='x'*380) for _ in range(13578)]
  self.dictionary={'aliases':aliases,'audit':history,'brand_candidates':[],'learning_candidates':[]}
  (self.path/'dictionary.json').write_text(json.dumps(self.dictionary,ensure_ascii=False))
