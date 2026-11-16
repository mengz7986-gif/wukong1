"""Bounded send timeout and read-only delivery UI; no real Telegram network."""
import asyncio,json,os,sys,socket,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]

@pytest.mark.parametrize('name,phase',[('ConnectTimeout','CONNECT'),('ReadTimeout','READ'),('WriteTimeout','WRITE'),('PoolTimeout','POOL')])
def test_transport_cause_persisted_unknown_without_retry(tmp_path,monkeypatch,name,phase):
    import httpx
    from telegram.error import TimedOut
    from success_receipt_audit import ReceiptSender
    path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path));calls=[]
    from telegram import Bot
    from telegram.request import HTTPXRequest
    async def run():
        async def fail(request):
            calls.append(request.method)
            raise getattr(httpx,name)('PRIVATE_ADDRESS_PHONE_TOKEN_MUST_NOT_APPEAR')
        request=HTTPXRequest(connect_timeout=10,read_timeout=15,write_timeout=15,pool_timeout=1,httpx_kwargs={'transport':httpx.MockTransport(fail),'trust_env':False})
        bot=Bot('123456789:offline',request=request)
        try:
            with pytest.raises(TimedOut):await ReceiptSender(bot,dict(order_number='NORMAL-NEW',_automatic_receipt=True)).send_message(chat_id=-123,text='PRIVATE_ORDER_BODY',message_thread_id=12)
        finally:
            for client in bot._request:await client.shutdown()
    asyncio.run(run());rows=[json.loads(x) for x in path.read_text().splitlines()]
    assert len(calls)==1 and [x['status'] for x in rows]==['SENDING','UNKNOWN']
    assert rows[-1]['exception_kind']=='TimedOut' and rows[-1]['cause_kind']==name and rows[-1]['transport_phase']==phase
    assert rows[-1]['timeout_seconds']==dict(connect=10,read=15,write=15,pool=1)
    assert rows[0]['message_thread_id']==12 and rows[0]['intended_destination_sha256']
    assert 'PRIVATE' not in path.read_text() and 'text' not in rows[0]


def test_sender_ack_and_multipart_projection(tmp_path,monkeypatch):
    from order_trace import CURRENT,receipt_plan
    from success_receipt_audit import ReceiptSender,records_for_orders,delivery_summary
    monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(tmp_path/'audit.jsonl'))
    class Target:
        async def send_message(self,**kw):return SimpleNamespace(message_id=10,chat=SimpleNamespace(id=-1,type='group'))
    async def run():
        token=CURRENT.set({'stages':[]})
        try:
            receipt_plan(2)
            sender=ReceiptSender(Target(),dict(order_number='NEW-ACK'))
            await sender.send_message(chat_id=-1,text='ONE')
            assert delivery_summary(records_for_orders(['NEW-ACK'])['NEW-ACK'])=='PARTIAL'
            await sender.send_message(chat_id=-1,text='TWO')
            a=records_for_orders(['NEW-ACK'])['NEW-ACK'];assert delivery_summary(a)=='SENT'
            assert [x['part_index'] for x in a]==[1,2] and all(x['part_count']==2 for x in a)
        finally:CURRENT.reset(token)
    asyncio.run(run())

@pytest.mark.parametrize('attempts,outbox,expected',[
    ([dict(status='UNKNOWN')],[],'UNKNOWN'),
    ([dict(status='SENT')],[dict(status='unknown')],'UNKNOWN'),
    ([],[dict(status='pending')],'PENDING'),
    ([dict(status='NOT_SENT')],[],'NOT_SENT'),
    ([],[],'NO_EVIDENCE'),
    ([dict(status='SENT',part_index=1,part_count=2)],[],'PARTIAL'),
    ([dict(status='NOT_SENT',part_index=1,part_count=1,at=1),dict(status='SENT',part_index=1,part_count=1,at=2)],[],'SENT'),
    ([dict(status='SENT',part_index=1,part_count=2),dict(status='SENT',part_index=2,part_count=3)],[],'UNKNOWN'),
])
def test_delivery_does_not_infer_from_business_completion(attempts,outbox,expected):
    from success_receipt_audit import delivery_summary
    assert delivery_summary(attempts,outbox)==expected


def test_actual_shared_client_policy_and_polling_separation():
    import config
    from telegram.ext import ApplicationBuilder
    async def run():
        request=config.telegram_send_request();app=ApplicationBuilder().token('123456789:offline').request(request).build()
        assert app.bot.request is request
        timeout=request._client.timeout
        assert (timeout.connect,timeout.read,timeout.write,timeout.pool)==(10,15,15,1)
        assert request._client._transport._pool._max_connections==256
        assert app.bot._request[0] is not request
        for client in app.bot._request:await client.shutdown()
    asyncio.run(run())


def test_real_local_http_delayed_ack_old_timeout_vs_candidate(tmp_path,monkeypatch):
    """Real loopback HTTP: same 6s ACK, old 5s times out, new 15s succeeds."""
    import config
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from telegram.error import TimedOut
    from success_receipt_audit import ReceiptSender
    path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path));requests=[];handlers=set()
    async def run():
        async def respond(reader,writer):
            task=asyncio.current_task();handlers.add(task)
            try:
                header=await reader.readuntil(b'\r\n\r\n');size=int(next(x.split(b':',1)[1] for x in header.split(b'\r\n') if x.lower().startswith(b'content-length:')))
                await reader.readexactly(size)
                method=header.split(b' ')[1].split(b'/')[-1].decode();requests.append(method)
                if method=='getMe':result=dict(id=123456789,is_bot=True,first_name='Synthetic',username='synthetic_bot')
                else:
                    assert method=='sendMessage';await asyncio.sleep(6)
                    result=dict(message_id=100+len(requests),date=1,chat=dict(id=-123,type='group'),text='SYNTHETIC')
                body=json.dumps(dict(ok=True,result=result)).encode();writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body);await writer.drain()
            except (ConnectionError,asyncio.CancelledError,asyncio.IncompleteReadError):pass
            finally:
                writer.close();await writer.wait_closed();handlers.discard(task)
        server=await asyncio.start_server(respond,'127.0.0.1',0);port=server.sockets[0].getsockname()[1]
        try:
            for name,request in [('OLD',HTTPXRequest(read_timeout=5)),('NEW',config.telegram_send_request())]:
                async with Bot('123456789:offline',base_url=f'http://127.0.0.1:{port}/bot',request=request) as bot:
                    sender=ReceiptSender(bot,dict(order_number='LOCAL-'+name))
                    if name=='OLD':
                        with pytest.raises(TimedOut):await sender.send_message(chat_id=-123,text='SYNTHETIC')
                    else:assert (await sender.send_message(chat_id=-123,text='SYNTHETIC')).message_id>0
        finally:
            server.close();await server.wait_closed()
            if handlers:await asyncio.gather(*list(handlers),return_exceptions=True)
    asyncio.run(run());assert requests.count('sendMessage')==2
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    assert [(x['order_numbers'],x['status']) for x in rows if x['status']!='SENDING']==[(['LOCAL-OLD'],'UNKNOWN'),(['LOCAL-NEW'],'SENT')]
    assert rows[1]['transport_phase']=='READ' and rows[1]['cause_kind']=='ReadTimeout'

from test_receipt_speed import chain

def test_admin_list_and_existing_detail_show_unknown_without_writes(chain,monkeypatch):
    from success_receipt_audit import append
    p=chain.pipeline;result=asyncio.run(chain.handle(1));assert result['state']=='COMPLETED'
    from models import Order
    with p.db.SessionLocal() as session:order=session.query(Order).one();oid=order.id;number=order.order_number
    row=dict(attempt_id='extra-unknown',order_numbers=[number],task_id='synthetic',method='send_message')
    append(dict(row,status='SENDING'));append(dict(row,status='UNKNOWN',message_id=None))
    counts=p.counts();stock=p.quantities();audit=(chain.path/'receipts.jsonl').read_bytes();sends=len(chain.http_attempts)
    import api
    api.app.dependency_overrides[api.require_admin_or_api_secret]=lambda:'SYNTHETIC_ADMIN'
    try:
        ordinary=chain.http.get('/api/orders',params={'include_meta':True});assert ordinary.status_code==200
        assert 'receipt' not in ordinary.json()['orders'][0]
        listing=chain.http.get('/api/orders',params={'include_meta':True,'include_receipt_evidence':True});assert listing.status_code==200,listing.text
        data=next(x for x in listing.json()['orders'] if x['id']==oid)
        assert data['receipt']==dict(status='UNKNOWN',order_created=True)
        detail=chain.http.get(f'/api/orders/{oid}/receipt-evidence');assert detail.status_code==200
        assert detail.json()['status']=='UNKNOWN' and detail.json()['resend_triggered'] is False
        node=os.getenv('NODE_BINARY','node');source=Path(__file__).resolve().parents[1]/'legacy_app/static/js/orders.js'
        code=source.read_text().split('function receiptDeliveryBadge(receipt) {',1)[1]
        script='function receiptDeliveryBadge(receipt) {'+code+'\nconsole.log(receiptDeliveryBadge(JSON.parse(process.argv[1])));'
        html=subprocess.check_output([node,'-e',script,json.dumps(data['receipt'])],text=True)
        assert '订单已创建' in html and '结果未知' in html and 'text-danger' in html
    finally:api.app.dependency_overrides.pop(api.require_admin_or_api_secret,None)
    assert p.counts()==counts and p.quantities()==stock and (chain.path/'receipts.jsonl').read_bytes()==audit and len(chain.http_attempts)==sends


def test_admin_unknown_read_failure_is_unavailable_not_sent(chain):
    import api
    p=chain.pipeline;assert asyncio.run(chain.handle(1))['state']=='COMPLETED'
    audit=chain.path/'receipts.jsonl';audit.write_text('INVALID AUDIT')
    counts=p.counts();stock=p.quantities();sends=len(chain.http_attempts)
    api.app.dependency_overrides[api.require_admin_or_api_secret]=lambda:'SYNTHETIC_ADMIN'
    try:
        response=chain.http.get('/api/orders',params={'include_meta':True,'include_receipt_evidence':True})
        assert response.status_code==200
        assert response.json()['orders'][0]['receipt']['status']=='UNAVAILABLE'
    finally:api.app.dependency_overrides.pop(api.require_admin_or_api_secret,None)
    assert p.counts()==counts and p.quantities()==stock and len(chain.http_attempts)==sends and audit.read_text()=='INVALID AUDIT'


def test_admin_snapshot_releases_file_lock_before_json_parsing(tmp_path,monkeypatch):
    import fcntl
    import success_receipt_audit as audit
    path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path))
    audit.append(dict(attempt_id='test',order_numbers=['TEST'],status='SENDING'))
    audit.append(dict(attempt_id='test',order_numbers=['TEST'],status='SENT',message_id=1))
    old=json.loads;checked=[]
    def parse(value,*args,**kw):
        with path.open('rb') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);checked.append(True)
        return old(value,*args,**kw)
    monkeypatch.setattr(audit.json,'loads',parse)
    assert audit.records_for_orders(['TEST'])['TEST'][0]['status']=='SENT' and len(checked)==2
