"""Targeted evidence for the four-specials fixes; synthetic catalog only."""
import json,os,sqlite3,tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

@pytest.fixture
def isolated_rules(tmp_path,monkeypatch):
 import sys
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))
 dictionary=tmp_path/'dictionary.json';dictionary.write_text(json.dumps({'aliases':[],'audit':[],'brand_candidates':[]}))
 database=tmp_path/'catalog.db';c=sqlite3.connect(database)
 c.execute('CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,parent_id INTEGER,is_parent INTEGER,is_archived INTEGER)')
 c.executemany('INSERT INTO products VALUES(?,?,?,?,0)',[(10,'甲牌',None,1),(20,'乙牌',None,1),(1,'薄荷',10,0),(2,'苹果',20,0),(3,'薄荷',20,0)]);c.commit();c.close()
 monkeypatch.setenv('RAINBOW_DICTIONARY_PATH',str(dictionary));monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(database));monkeypatch.setenv('BOT_TOKEN','OFFLINE_TEST')
 monkeypatch.delenv('NEWBOT_INGESTION_POLICY_PATH',raising=False)
 catalog=[dict(id=1,name='薄荷',parent_id=10,parent_name='甲牌',default_warehouse_id=1),dict(id=2,name='苹果',parent_id=20,parent_name='乙牌',default_warehouse_id=1),dict(id=3,name='薄荷',parent_id=20,parent_name='乙牌',default_warehouse_id=1)]
 return dictionary,catalog

@pytest.mark.parametrize('alias',['薄荷2','薄荷两支','薄荷盒','备选薄荷','薄荷（备选）','甲牌薄荷'])
def test_contaminated_flavor_not_learned(isolated_rules,alias):
 import flavor_aliases
 _,catalog=isolated_rules
 assert flavor_aliases.safe_learning_token(alias,catalog[0],catalog,{'aliases':[]}) is None

def test_rejected_manual_mapping_cannot_return_as_candidate(isolated_rules):
 import flavor_aliases,rule_dictionary
 path,catalog=isolated_rules
 row=dict(id='manual-rejected',type='EXPLICIT_FLAVOR',alias='薄荷儿',standard='薄荷',category='甲牌',product_id=1,warehouse_id=1,category_id=10,status='rejected',scope='GLOBAL',readonly=False)
 path.write_text(json.dumps({'aliases':[row],'audit':[],'brand_candidates':[]}))
 assert flavor_aliases.learn('薄荷儿',catalog[0],.95,catalog,event='event-test') is None
 assert not rule_dictionary.read_store().get('brand_candidates')

def test_explicit_category_cannot_fall_back_to_other_brand(isolated_rules):
 from bot import OrderBot
 _,catalog=isolated_rules
 bot=OrderBot.__new__(OrderBot)
 assert bot._match_product('品类(甲牌)口味(苹果)',catalog) is None

def test_bare_ambiguous_flavor_cannot_pick_first_brand(isolated_rules):
 from bot import OrderBot
 _,catalog=isolated_rules
 assert OrderBot.__new__(OrderBot)._match_product('薄荷',catalog) is None

def test_exact_unique_product_remains_valid(isolated_rules):
 from bot import OrderBot
 _,catalog=isolated_rules
 assert OrderBot.__new__(OrderBot)._match_product('苹果',catalog)['id']==2

def test_denial_survives_deleting_rejected_alias(isolated_rules):
 import flavor_aliases,rule_dictionary
 path,catalog=isolated_rules
 row=dict(id='manual-1',type='EXPLICIT_FLAVOR',alias='薄荷儿',standard='薄荷',category='甲牌',product_id=1,warehouse_id=1,category_id=10,status='pending',scope='GLOBAL',readonly=False)
 path.write_text(json.dumps({'aliases':[row],'audit':[],'brand_candidates':[]}))
 flavor_aliases.mutate({'id':'manual-1','action':'reject'},'tester',catalog)
 flavor_aliases.mutate({'id':'manual-1','action':'delete'},'tester',catalog)
 assert flavor_aliases.learn('薄荷儿',catalog[0],.95,catalog) is None
 store=rule_dictionary.read_store()
 assert len(store['rejected_flavor_mappings'])==1 and not store['brand_candidates']

def test_explicit_restore_requires_another_approval(isolated_rules):
 import flavor_aliases,rule_dictionary
 path,catalog=isolated_rules
 row=dict(id='manual-1',type='EXPLICIT_FLAVOR',alias='薄荷儿',standard='薄荷',category='甲牌',product_id=1,warehouse_id=1,category_id=10,status='pending',scope='GLOBAL',readonly=False)
 path.write_text(json.dumps({'aliases':[row],'audit':[],'brand_candidates':[]}))
 flavor_aliases.mutate({'id':'manual-1','action':'reject'},'tester',catalog)
 flavor_aliases.mutate({'id':'manual-1','action':'restore'},'tester',catalog)
 store=rule_dictionary.read_store();assert store['aliases'][0]['status']=='pending'
 assert not flavor_aliases.mapping_rejected(store,store['aliases'][0])
 flavor_aliases.mutate({'id':'manual-1','action':'approve'},'tester',catalog)
 assert rule_dictionary.read_store()['aliases'][0]['status']=='active'

def test_rejection_is_not_a_cross_brand_flavor_ban(isolated_rules):
 import flavor_aliases
 _,catalog=isolated_rules
 rejected=dict(type='EXPLICIT_FLAVOR',alias='薄荷儿',standard='薄荷',category='甲牌',warehouse_id=1,category_id=10,product_id=1,status='rejected',scope='GLOBAL')
 other=dict(rejected,category='乙牌',category_id=20,product_id=3,status='active')
 assert not flavor_aliases.mapping_rejected({'rejected_flavor_mappings':[rejected]},other)

def test_rejected_mapping_is_not_used_even_if_old_active_alias_remains(isolated_rules):
 import rule_dictionary
 path,catalog=isolated_rules
 active=dict(id='manual-active',type='EXPLICIT_FLAVOR',alias='薄荷儿',standard='薄荷',category='甲牌',warehouse_id=1,category_id=10,product_id=1,status='active',scope='GLOBAL')
 path.write_text(json.dumps({'aliases':[active],'audit':[],'rejected_flavor_mappings':[dict(active,status='rejected')]}))
 with rule_dictionary.runtime_snapshot():
  assert rule_dictionary.manual_flavor('甲牌','薄荷儿',['薄荷'],catalog[:1]) is None
  assert rule_dictionary.manual_flavor('甲牌','薄荷',['薄荷'],catalog[:1])=='薄荷'

def test_candidate_ignore_never_requeues_and_can_be_explicitly_restored(isolated_rules):
 import flavor_aliases,rule_dictionary
 _,catalog=isolated_rules
 from test_order_six_final import proof
 candidate=flavor_aliases.learn('薄荷儿',catalog[0],.95,catalog,origin='CUSTOMER_CONFIRMED_AI',event='first',confirmation=proof(catalog[0],'薄荷儿'))
 flavor_aliases.review({'id':candidate['id'],'action':'ignore'},'tester',catalog)
 assert flavor_aliases.learn('薄荷儿',catalog[0],.99,catalog,event='second') is None
 flavor_aliases.review({'id':candidate['id'],'action':'restore'},'tester',catalog)
 assert rule_dictionary.read_store()['brand_candidates'][0]['status']=='pending'

def test_exact_catalog_with_numeric_name_is_preserved(isolated_rules):
 import flavor_aliases
 _,catalog=isolated_rules
 product=dict(catalog[0],name='薄荷2号')
 assert flavor_aliases.safe_learning_token('薄荷2号',product,[product],{'aliases':[]})=='薄荷2号'

def test_runtime_snapshot_reads_grammar_once_and_refreshes_next_request(isolated_rules,monkeypatch):
 import learning_knowledge,rule_dictionary
 path,_=isolated_rules
 path.write_text(json.dumps({'aliases':[],'audit':[],'learning_candidates':[dict(id='one',kind='HABIT',form='ITEM_CHINESE_QUANTITY',scope='GLOBAL',status='approved',revision=1)]}))
 original=rule_dictionary.read_store;calls=[]
 def read():calls.append(1);return original()
 monkeypatch.setattr(rule_dictionary,'read_store',read)
 with rule_dictionary.runtime_snapshot():
  assert len(learning_knowledge.approved_knowledge())==1
  assert len(learning_knowledge.approved_knowledge())==1
 assert len(calls)==1
 path.write_text(json.dumps({'aliases':[],'audit':[],'learning_candidates':[]}))
 with rule_dictionary.runtime_snapshot():assert not learning_knowledge.approved_knowledge()
 assert len(calls)==2

def test_catalog_target_resolution_does_not_mutate_source_aliases(isolated_rules):
 import rule_dictionary
 _,catalog=isolated_rules
 source={'aliases':[dict(id='one',type='EXPLICIT_FLAVOR',category_id=10,product_id=1,standard='old',category='old',status='active')],'audit':[{'actor':'test'}]}
 raw_catalog=[dict(id=10,name='甲牌',is_parent=True,is_archived=False),dict(id=1,name='薄荷',parent_id=10,is_parent=False,is_archived=False)]
 resolved=rule_dictionary.resolve_catalog_targets(source,raw_catalog)
 assert resolved['aliases'][0]['standard']=='薄荷' and source['aliases'][0]['standard']=='old'
 assert resolved['audit']==source['audit']

@pytest.mark.parametrize('alias',['甲牌薄荷','备选薄荷','薄荷两支','薄荷盒'])
def test_existing_polluted_candidate_cannot_be_approved(isolated_rules,alias):
 import flavor_aliases
 _,catalog=isolated_rules
 row=dict(id='old-pending',type='EXPLICIT_FLAVOR',alias=alias,standard='薄荷',category='甲牌',product_id=1,warehouse_id=1,category_id=10,status='pending')
 store={'aliases':[],'brand_candidates':[row],'audit':[]}
 with pytest.raises(ValueError):
  flavor_aliases.review({'id':'old-pending','action':'approve'},'tester',catalog,_store=store)
 assert row['status']=='pending' and not store['aliases']

@pytest.mark.parametrize('quantity',['2','两支'])
def test_order_counts_exclude_contact_amount_and_total_numbers(isolated_rules,quantity):
 from order_parser import OrderParser
 _,catalog=isolated_rules
 text=f'甲牌 薄荷{quantity}\n总计2支\n张三 13800000000\n北京市朝阳区建国路88号\n备注金额100元'
 result=OrderParser.extract_order_info(text,catalog)
 assert result['items']==[('品类(甲牌)口味(薄荷)',2)]
 assert result['recipient']=='张三' and result['phone']=='13800000000'
 assert result['computed_total']==2 and result['declared_total']==2

def test_preference_label_separate_from_flavor_and_backup_retained(isolated_rules):
 from order_parser import OrderParser
 from order_structure import structure_order_text
 _,catalog=isolated_rules
 text='甲牌 首选薄荷2 备选苹果\n张三 13800000000\n北京市朝阳区建国路88号'
 assert OrderParser.extract_order_info(text,catalog)['items']==[('品类(甲牌)口味(薄荷)',2)]
 assert any('备选苹果' in note for note in structure_order_text(text,catalog).fulfillment_notes)

def test_real_catalog_name_beginning_with_preference_word_is_preserved(isolated_rules):
 from order_parser import OrderParser
 _,catalog=isolated_rules
 catalog=[dict(catalog[0],name='首选薄荷')]
 result=OrderParser.extract_order_info('甲牌 首选薄荷2\n张三 13800000000\n北京市朝阳区建国路88号',catalog)
 assert result['items']==[('品类(甲牌)口味(首选薄荷)',2)]
