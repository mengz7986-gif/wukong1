"""Delta-only safety for proven prefix and JSON write hot paths."""
import os,sys,json,io,copy,stat,importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]

def test_heading_index_is_exact_ordered_unicode_prefilter():
 from category_evidence import _heading_candidates,_heading_match
 aliases=['甲牌',' 甲 牌 ','ICE12K','İ牌','ı牌','K牌','K牌','ſ牌','s牌','正品甲牌','A+B','😀牌','']+[f'样品牌{i}' for i in range(500)]
 mappings={alias:{alias} for alias in aliases};candidates=_heading_candidates(mappings)
 probes=['甲牌','甲 牌','ICE12K','ice12k','I牌','i牌','İ牌','ı牌','K牌','k牌','K牌','S牌','s牌','ſ牌','正品甲牌','A+B','😀牌','其他牌','']
 for prefix in probes:
  for suffix in ['',':薄荷2',' 薄荷2','一代','10000口','仓']:
   text=prefix+suffix
   old=[(a,m.span()) for a in mappings if (m:=_heading_match(a,text))]
   new=[(a,m.span()) for a in candidates(text) if (m:=_heading_match(a,text))]
   assert new==old,(prefix,suffix)

def test_prepare_matches_checkpoint_across_contact_backup_and_review_states():
 from category_evidence import prepare
 path=Path(os.environ['RECEIPT_BASELINE_SOURCE'])/'legacy_app/category_evidence.py'
 spec=importlib.util.spec_from_file_location('checkpoint_category_evidence',path);baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
 products=[dict(id=i,name=flavor,parent_name=brand,parent_id=10+i,default_warehouse_id=1,is_active=True) for i,(brand,flavor) in enumerate([('甲牌','薄荷'),('甲牌','苹果'),('乙牌','薄荷'),('乙牌','青提'),('ICE12K','香蕉'),('İ牌','苹果')],1)]
 corpus=['甲牌 薄荷1','甲牌\n薄荷两支\n共2支','2支甲牌 薄荷','甲牌。2支薄荷','甲牌 薄荷1 共1支','甲牌 薄荷1（备选苹果）','首选甲牌薄荷1，备选苹果','甲牌 薄荷1\n乙牌 青提2\n总计3支','品类：甲牌\n薄荷各一','甲牌(薄荷1)','正品甲牌\n薄荷1','ICE12K 香蕉1','i牌 苹果1','甲薄荷1','甲牌 薄荷1\n张三 13800000000\n北京市朝阳区建国路88号','薄荷1\n备注：金额123元','乙牌 薄荷1','陌生牌 薄荷2','甲牌 一盒苹果','甲牌\n随机2支','甲牌 薄荷1\n精准发货','甲牌 薄荷1\n备选：苹果','甲牌 薄荷1\n合计1支']
 for status in ('active','pending','rejected','disabled'):
  rows=[dict(id='alias',type='MODEL_ALIASES',alias='甲',standard='甲牌',scope='GLOBAL',status=status)]
  for text in corpus:
   old_unknown=[];new_unknown=[]
   assert prepare(text,products,rows,implicit_unknown=new_unknown)==baseline.prepare(text,products,rows,implicit_unknown=old_unknown),(status,text)
   assert new_unknown==old_unknown
 # A fresh invocation must see changed review status, without a persistent cache.
 active=[dict(id='x',type='MODEL_ALIASES',alias='老甲',standard='甲牌',scope='GLOBAL',status='active')]
 rejected=[dict(active[0],status='rejected')]
 assert prepare('老甲 薄荷1',products,active)==baseline.prepare('老甲 薄荷1',products,active)
 assert prepare('老甲 薄荷1',products,rejected)==baseline.prepare('老甲 薄荷1',products,rejected)

@pytest.fixture
def dictionary_path(tmp_path,monkeypatch):
 target=tmp_path/'rules.json';target.write_text(json.dumps({'aliases':[],'audit':[],'brand_candidates':[]}))
 monkeypatch.setenv('RAINBOW_DICTIONARY_PATH',str(target));monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'absent.db'))
 return target

def test_dictionary_fast_write_is_byte_identical_and_locked(dictionary_path):
 import brand_aliases
 expected=io.StringIO()
 def change(store):
  store['audit'].append({'text':'中文😀','numbers':[1,1.25,-0.,None,True],'nested':{'z':'末尾\n'}})
  json.dump(store,expected,ensure_ascii=False)
 brand_aliases.transaction(change)
 assert dictionary_path.read_bytes()==expected.getvalue().encode()
 assert stat.S_IMODE(dictionary_path.stat().st_mode)==0o600
 def append(i):brand_aliases.transaction(lambda store:store['audit'].append({'index':i}))
 with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(append,range(12)))
 assert sorted(r['index'] for r in json.loads(dictionary_path.read_text())['audit'][1:])==list(range(12))

def test_dictionary_write_failure_retains_original(dictionary_path,monkeypatch):
 import brand_aliases
 original=dictionary_path.read_bytes()
 def fail(*args):raise OSError('isolated replace fault')
 monkeypatch.setattr(brand_aliases.os,'replace',fail)
 with pytest.raises(OSError):brand_aliases.transaction(lambda store:store['audit'].append({'index':1}))
 assert dictionary_path.read_bytes()==original and not list(dictionary_path.parent.glob('.brands-*'))

def test_evidence_fast_write_preserves_bytes_and_atomic_failure(tmp_path):
 from learning_evidence import _write,_read
 target=tmp_path/'evidence.json';value={'version':1,'records':[{'text':'中文😀','values':[None,True,1.25]}]}
 expected=io.StringIO();json.dump(value,expected,ensure_ascii=False,separators=(',',':'))
 _write(target,value);assert target.read_bytes()==expected.getvalue().encode() and _read(target)==value
 assert stat.S_IMODE(target.stat().st_mode)==0o600
 before=target.read_bytes()
 with pytest.raises(TypeError):_write(target,{'records':[object()]})
 assert target.read_bytes()==before and not list(tmp_path.glob('.evidence-*'))
