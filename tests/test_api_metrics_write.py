"""Successful API telemetry only; synthetic files, subprocess crash boundaries."""
import sys,os,json,io,stat,time,subprocess
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))
import performance_metrics as metrics


def sample(i):
 return dict(mode='application',stage='API_TOTAL',provider='Local',elapsed_ms=1.25,evidence_id=f'{i:064x}',trace_id=f'{i:032x}',source=f'{i:016x}')


def test_same_bytes_mode_and_fsync_before_replace(tmp_path,monkeypatch):
 target=tmp_path/'metrics.json';value={'version':1,'samples':[sample(1),{'unicode':'荔枝😀','v':[None,True,1.5,-0.0]}]}
 expected=io.StringIO();json.dump(value,expected,ensure_ascii=False)
 order=[];fsync=os.fsync;replace=os.replace
 def sync(fd):order.append('fsync');return fsync(fd)
 def swap(a,b):order.append('replace');return replace(a,b)
 monkeypatch.setattr(metrics.os,'fsync',sync);monkeypatch.setattr(metrics.os,'replace',swap)
 metrics._write_committed_api(target,value)
 assert target.read_bytes()==expected.getvalue().encode() and stat.S_IMODE(target.stat().st_mode)==0o600
 assert order==['fsync','replace']


@pytest.mark.parametrize('stage,error,stages,fast',[
 ('API_TOTAL',False,[('PERSIST_TO_COMMIT',2)],True),
 ('API_TOTAL',True,[('PERSIST_TO_COMMIT',2)],False),
 ('API_TOTAL',False,[('ORDER_COMMIT',2)],False),
 ('COMPLETE_RECEIPT',False,[('PERSIST_TO_COMMIT',2)],False),
])
def test_only_successful_committed_api_opts_in(monkeypatch,stage,error,stages,fast):
 import order_trace
 seen=[];monkeypatch.setattr(metrics,'record_samples',lambda rows,**kw:seen.append(kw))
 order_trace.persist({'stages':stages},3,stage,error)
 assert seen==[{'committed_api':True} if fast else {}]


@pytest.mark.parametrize('fault',['encode','fsync','replace'])
def test_failed_atomic_write_preserves_previous_and_is_observable(tmp_path,monkeypatch,caplog,fault):
 target=tmp_path/'metrics.json';monkeypatch.setenv('RAINBOW_PERFORMANCE_PATH',str(target))
 metrics.record_samples([sample(1)],committed_api=True);before=target.read_bytes()
 def fail(*a,**kw):raise OSError('SYNTHETIC_FAULT_NO_CUSTOMER_DATA')
 if fault=='encode':monkeypatch.setattr(metrics.json,'dumps',fail)
 else:monkeypatch.setattr(metrics.os,fault,fail)
 metrics.record_samples([sample(2)],committed_api=True)
 assert target.read_bytes()==before and not list(tmp_path.glob('.api-metrics-*'))
 assert 'API_PERFORMANCE_WRITE_FAILED kind=OSError' in caplog.text
 assert 'SYNTHETIC_FAULT' not in caplog.text


def test_retention_and_deduplication_unchanged(tmp_path,monkeypatch):
 target=tmp_path/'metrics.json';monkeypatch.setenv('RAINBOW_PERFORMANCE_PATH',str(target))
 metrics.record_samples([sample(i) for i in range(5001)],committed_api=True)
 metrics.record_samples([sample(5000)],committed_api=True)
 values=json.loads(target.read_text());assert list(values)==['version','samples']
 assert [r['evidence_id'] for r in values['samples']]==[f'{i:064x}' for i in range(1,5001)]


def child(tmp_path,code,*args):
 env=dict(os.environ,PYTHONPATH=str(Path(metrics.__file__).parent),RAINBOW_PERFORMANCE_PATH=str(tmp_path/'metrics.json'))
 return subprocess.Popen([sys.executable,'-c',code,*map(str,args)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def test_concurrent_processes_lose_no_successful_samples(tmp_path):
 code='''import sys,performance_metrics as m
for i in range(int(sys.argv[1])*20,(int(sys.argv[1])+1)*20):
 m.record_samples([dict(mode='application',provider='Local',stage='API_TOTAL',elapsed_ms=1,evidence_id=f'{i:064x}')],committed_api=True)
'''
 processes=[child(tmp_path,code,i) for i in range(4)]
 for p in processes:
  out,err=p.communicate(timeout=30);assert p.returncode==0 and not err
 values=json.loads((tmp_path/'metrics.json').read_text())['samples']
 assert len(values)==80 and {r['evidence_id'] for r in values}=={f'{i:064x}' for i in range(80)}


@pytest.mark.parametrize('after_replace',[False,True])
def test_sigkill_leaves_complete_previous_or_complete_new_file(tmp_path,monkeypatch,after_replace):
 target=tmp_path/'metrics.json';monkeypatch.setenv('RAINBOW_PERFORMANCE_PATH',str(target));metrics.record_samples([sample(1)],committed_api=True)
 marker=tmp_path/'ready'
 code='''import time,sys
from pathlib import Path
import performance_metrics as m
original=m.os.replace
def fence(a,b):
 if sys.argv[2]=='1':original(a,b)
 Path(sys.argv[1]).write_text('ready')
 time.sleep(60)
 if sys.argv[2]=='0':original(a,b)
m.os.replace=fence
m.record_samples([dict(mode='application',provider='Local',stage='API_TOTAL',elapsed_ms=1,evidence_id=f'{2:064x}')],committed_api=True)
'''
 p=child(tmp_path,code,marker,int(after_replace))
 try:
  deadline=time.monotonic()+10
  while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
  assert marker.exists();p.kill();p.communicate(timeout=5)
  rows=json.loads(target.read_text())['samples'];assert {r['evidence_id'] for r in rows}=={f'{i:064x}' for i in ([1,2] if after_replace else [1])}
  metrics.record_samples([sample(3)],committed_api=True)
  assert f'{3:064x}' in {r['evidence_id'] for r in json.loads(target.read_text())['samples']}
 finally:
  if p.poll() is None:p.kill();p.wait(timeout=5)
