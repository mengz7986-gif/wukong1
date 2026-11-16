"""Concurrent cold loads must share one index, without changing fail-closed reads."""
import sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))

@pytest.fixture
def clean_index(monkeypatch):
 import region_index
 region_index.get_region_index.cache_clear()
 yield region_index
 region_index.get_region_index.cache_clear()

def test_concurrent_cold_read_constructs_one_snapshot_and_explicit_clear_refreshes(clean_index,monkeypatch):
 module=clean_index;calls=[];barrier=threading.Barrier(4)
 def factory():
  result=SimpleNamespace(provinces={1:1},cities={1:1},regions={1:1})
  calls.append(result);time.sleep(.1);return result
 monkeypatch.setattr(module,'RegionIndex',factory)
 def load(_):barrier.wait(timeout=3);return module.get_region_index()
 with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(load,range(4)))
 assert len(calls)==1 and all(row is results[0] for row in results)
 assert module.get_region_index() is results[0]
 assert module.get_region_index.cache_info().currsize==1
 module.get_region_index.cache_clear();assert module.get_region_index() is not results[0] and len(calls)==2

def test_failed_or_empty_index_is_cached_fail_closed_and_can_be_explicitly_reloaded(clean_index,monkeypatch):
 module=clean_index;calls=[]
 def fail():calls.append(1);raise OSError('isolated index read fault')
 monkeypatch.setattr(module,'RegionIndex',fail)
 assert module.get_region_index() is None and module.get_region_index() is None and len(calls)==1
 module.get_region_index.cache_clear();monkeypatch.setattr(module,'RegionIndex',lambda:SimpleNamespace(provinces={},cities={1:1},regions={1:1}))
 assert module.get_region_index() is None
 module.get_region_index.cache_clear();value=SimpleNamespace(provinces={1:1},cities={1:1},regions={1:1});monkeypatch.setattr(module,'RegionIndex',lambda:value)
 assert module.get_region_index() is value
