"""Session-only, server-enforced AI administration. API service keys excluded."""
import asyncio
import re
import secrets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from ai_settings import public_status, save_settings, reveal_key, get_settings, encryption_ready, AIConfigError
from ai_transport import test_connection


def make_router(templates, admin_username, security_ready=lambda: True, catalog=lambda: []):
    router = APIRouter()

    def guard(request, sensitive=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401, '请先登录')
        if sensitive and request.session.get('admin_username') != admin_username():
            raise HTTPException(403, '仅最高管理员可执行此操作')
        if sensitive and not security_ready():
            raise HTTPException(503, '请先配置独立、安全的会话签名密钥')
        if sensitive and request.url.scheme != 'https' and request.url.hostname not in ('localhost', '127.0.0.1', 'testserver'):
            raise HTTPException(403, '密钥操作需要HTTPS连接')
        if request.method != 'GET':
            expected = request.session.get('ai_csrf') or ''
            supplied = request.headers.get('X-AI-CSRF') or ''
            if not expected or not secrets.compare_digest(expected, supplied):
                raise HTTPException(403, '请求校验失败，请刷新页面')
        from ai_settings import retired
        if retired() and request.url.path in ('/api/ai/config','/api/ai/test','/api/ai/balance',
                '/api/ai/key/reveal','/api/ai/key/copy','/api/ai/recognition-reviews/analyze'):
            raise HTTPException(410, '外部模型功能已移除；请使用本地审核或独立地址服务')

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @router.get('/admin/ai')
    async def page(request: Request):
        guard(request)
        request.session.setdefault('ai_csrf', secrets.token_urlsafe(32))
        request.session.setdefault('rules_csrf', secrets.token_urlsafe(32))
        try:
            status = public_status()
        except AIConfigError as error:
            status = {'configuration_error': str(error)}
        status['session_security_ready'] = security_ready()
        status['secure_connection'] = request.url.scheme == 'https' or request.url.hostname in ('localhost', '127.0.0.1', 'testserver')
        result = templates.TemplateResponse(request, 'admin/ai_settings.html', {
            'ai_status': status, 'ai_csrf': request.session['ai_csrf'], 'rules_csrf': request.session['rules_csrf'],
            'ai_can_manage': request.session.get('admin_username') == admin_username() and security_ready(),
            'ai_key_access': request.session.get('admin_username') == admin_username() and security_ready() and status['secure_connection'] and encryption_ready(),
        })
        result.headers['Cache-Control'] = 'no-store'
        result.headers['Referrer-Policy'] = 'no-referrer'
        return result

    @router.get('/api/ai/status')
    async def status(request: Request):
        guard(request)
        try:
            return response(public_status())
        except AIConfigError as error:
            raise HTTPException(503, str(error)) from None

    @router.get('/admin/address')
    async def address_page(request: Request):
        guard(request, True)
        request.session.setdefault('ai_csrf', secrets.token_urlsafe(32))
        result = templates.TemplateResponse(request, 'admin/address_settings.html', {
            'address_csrf': request.session['ai_csrf'],
        })
        result.headers['Cache-Control'] = 'no-store'
        return result

    @router.get('/api/address/status')
    async def address_status(request: Request):
        guard(request, True)
        import aliyun_address_settings as address
        from aliyun_address_service import usage_snapshot
        value = address.public()
        value['usage'] = usage_snapshot(value['daily_limit'])
        return response(value)

    @router.post('/api/address/config')
    async def address_config(request: Request):
        guard(request, True)
        import aliyun_address_settings as address
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'enabled', 'timeout_seconds', 'daily_limit'}:
                raise ValueError('地址配置字段无效')
            return response(await asyncio.to_thread(address.save, body, request.session['admin_username']))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @router.get('/api/ai/recognition-reviews')
    async def recognition_reviews(request: Request, page: int = 1, include_archived: bool = False):
        guard(request)
        from failure_analysis import listing
        try:
            result = await asyncio.to_thread(listing, page, include_archived)
            result['automatic_analysis'] = __import__('os').getenv('RAINBOW_FAILURE_ANALYSIS_ENABLED', '0') == '1'
            return response(result)
        except (ValueError, OSError):
            raise HTTPException(409, '复核队列读取失败，请核查存储或页码') from None

    @router.get('/api/ai/recognition-reviews/catalog')
    async def recognition_review_catalog(request: Request):
        guard(request, True)
        return response({'products': [{k: p.get(k) for k in ('id','name','parent_id','parent_name','default_warehouse_id')}
                                      for p in catalog() if not p.get('is_archived')]})

    @router.post('/api/ai/recognition-reviews/{action}')
    async def recognition_review_action(action: str, request: Request):
        guard(request, True)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, '请求须为JSON对象') from None
        fields = {'detail': {'id'}, 'analyze': {'id','revision'},
                  'suggest': {'id','revision','suggestion'}, 'review': {'id','revision','action'}}
        if action not in fields or not isinstance(body,dict) or set(body) != fields[action]:
            raise HTTPException(400, '复核操作字段无效')
        import failure_analysis as queue
        actor = request.session['admin_username']
        try:
            if action == 'detail':
                value = await asyncio.to_thread(queue.detail, body['id'], actor)
            elif action == 'analyze':
                from ai_transport import run_provider_worker
                value = await run_provider_worker(queue.analyze, body['id'], body['revision'], actor, catalog())
            elif action == 'suggest':
                value = await asyncio.to_thread(queue.save_local_suggestion, body['id'], body['revision'],
                                                body['suggestion'], actor, catalog())
            else:
                value = await asyncio.to_thread(queue.review, body['id'], body['revision'], body['action'], actor, catalog())
            return response(value)
        except (ValueError, AIConfigError) as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            # Decryption/storage errors never expose credentials or ciphertext.
            raise HTTPException(503, '复核记录不可用，未执行订单操作；请核查密钥及存储') from None

    @router.get('/api/ai/learning-digest')
    async def learning_digest(request: Request):
        guard(request)
        from daily_learning_candidates import build_digest
        from rule_dictionary import read_store
        try:
            return response(await asyncio.to_thread(lambda: build_digest(read_store())))
        except (ValueError, OSError):
            raise HTTPException(503, '学习候选暂不可读取') from None

    @router.get('/api/ai/learning-center')
    async def learning_center(request: Request, status: str = None, page: int = 1):
        guard(request)
        if status not in (None, 'pending', 'approved', 'rejected', 'withdrawn', 'incomplete') or page < 1:
            raise HTTPException(400, '候选筛选无效')
        def snapshot():
            from rule_dictionary import read_store
            from learning_usage import summary as learning_usage_summary
            store=read_store()
            from learning_knowledge import review_version
            rows=[{k:r.get(k) for k in ('id','kind','form','scope','status','count','revision','reviewed_at','suggestion_edited_from')} for r in store.get('learning_candidates',[])]
            for public, private in zip(rows,store.get('learning_candidates',[])):
                public['review_version']=review_version(private)
                public['organized_sources']=len(private.get('recognition_review_sources',[]))
            from learning_readiness import readiness_index, bucket
            ready = readiness_index()
            buckets = {key: 0 for key in ('pending', 'approved', 'rejected', 'withdrawn', 'incomplete')}
            for row in rows:
                row['review_bucket'] = bucket(row, ready)
                row['evidence_complete'] = row['id'] in ready
                key = row['review_bucket']
                buckets[key] = buckets.get(key, 0) + 1
            selected = rows if status is None else [r for r in rows if r['review_bucket'] == status]
            visible = selected if status is None else selected[(page-1)*20:page*20]
            usage=learning_usage_summary()
            observed=max(0,int(usage.get('reviewed_hit_orders',0) or 0))
            local=max(0,int(usage.get('local_without_ai',0) or 0))
            ai=max(0,int(usage.get('ai_calls',0) or 0))
            # These are observations among orders that hit reviewed knowledge.
            # They are not an accuracy measure or a causal before/after saving.
            denominator=local+ai
            usage_public=dict(
                reviewed_hit_orders=observed,
                local_without_ai=local,
                ai_calls=ai,
                denominator=denominator,
                local_share_percent=(round(local*100/denominator,1) if denominator else None),
                ai_share_percent=(round(ai*100/denominator,1) if denominator else None),
                updated_at=usage.get('updated_at'),
                cost_cny=None,
                cost_evidence='暂无学习命中范围的独立费用归因',
                baseline_evidence='暂无固定对照基线，不能据此计算AI调用减少量或识别率提升',
                ai_call_reduction=None,
                saved_tokens=None,
                saved_cost=None,
                correction_rate=None,
                correction_rate_evidence='未建立人工标注真值集，学习后纠错率未统计',
            )
            statuses={}
            for row in rows:statuses[row.get('status','unknown')]=statuses.get(row.get('status','unknown'),0)+1
            import os
            return dict(candidates=visible, bucket_counts=buckets, page=page, page_size=20, total=len(selected), versions=store.get('knowledge_versions',[])[-100:],daily_runs=store.get('learning_ai_review_runs',[])[-30:],
                        organization_summary=[r for r in store.get('audit',[]) if r.get('action')=='organize_recognition_reviews'][-1:], usage=usage_public,
                        statistics={'candidate_statuses':statuses,
                                    'rule_hit_orders':observed,
                                    'ai_call_reduction':None,
                                    'saved_tokens':None,
                                    'saved_cost':None,
                                    'correction_rate':None},
                        scheduler_enabled=os.getenv('RAINBOW_DAILY_LEARNING_ENABLED','1')=='1',schedule='北京时间每日18:00后空闲时一次；失败至多重试2次，间隔15分钟',automatic_activation=False)
        return response(await asyncio.to_thread(snapshot))

    @router.get('/api/ai/learning-center/detail')
    async def learning_detail(request:Request,id:str):
        guard(request)
        if not re.fullmatch(r'[a-f0-9]{64}',id):raise HTTPException(400,'候选编号无效')
        from learning_evidence import detail
        return response(await asyncio.to_thread(detail,id))

    @router.post('/api/ai/learning-center/detail/raw')
    async def learning_raw(request:Request):
        guard(request,True)
        body=await request.json()
        if not isinstance(body,dict) or set(body)!={'id','commit_fingerprint','reason'}:
            raise HTTPException(400,'原文访问字段无效')
        if not all(isinstance(body.get(k),str) and re.fullmatch(r'[a-f0-9]{64}',body[k])
                   for k in ('id','commit_fingerprint')):
            raise HTTPException(400,'原文访问凭据无效')
        if not isinstance(body.get('reason'),str) or not (3<=len(body['reason'].strip())<=200):
            raise HTTPException(400,'请填写3-200字的原文访问原因')
        from learning_evidence import reveal_raw
        try:return response(await asyncio.to_thread(
            reveal_raw,body['id'],body['commit_fingerprint'],request.session['admin_username'],body['reason']))
        except ValueError as error:raise HTTPException(404,str(error)) from None

    @router.post('/api/ai/learning-center/detail/authorized')
    async def authorized_learning_detail(request: Request):
        guard(request, True)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {'id'} or not re.fullmatch(r'[a-f0-9]{64}', str(body['id'])):
            raise HTTPException(400, '候选编号无效')
        from learning_evidence import authorized_detail
        try:
            return response(await asyncio.to_thread(authorized_detail, body['id'], request.session['admin_username']))
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.get('/api/ai/product-candidates')
    async def product_candidates(request: Request):
        guard(request)
        from rule_dictionary import read_store
        return response({'candidates': read_store().get('brand_candidates', [])})

    @router.post('/api/ai/product-candidates/review')
    async def product_candidate_review(request: Request):
        guard(request, True)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {'ids', 'action'} or body['action'] not in ('approve', 'reject'):
            raise HTTPException(400, '审核字段无效')
        ids = body['ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200 or any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, '请选择1至200条不同候选')
        from brand_aliases import review
        from rule_dictionary import read_store
        results = []
        live_catalog = catalog()
        for identity in ids:
            try:
                candidate = next((r for r in read_store().get('brand_candidates', []) if r.get('id') == identity), None)
                if not candidate or candidate.get('status') not in ('pending','conflict'):
                    raise ValueError('候选已处理或不存在，请刷新后重试')
                value = await asyncio.to_thread(review, {'id': identity, 'action': 'approve' if body['action'] == 'approve' else 'ignore', '_expected_pending': True}, request.session['admin_username'], live_catalog)
                results.append({'id': identity, 'success': True})
            except ValueError as error:
                results.append({'id': identity, 'success': False, 'reason': str(error)})
        return response({'results': results})

    @router.post('/api/ai/learning-center/review')
    async def review_learning(request: Request):
        guard(request,True)
        body=await request.json()
        if not isinstance(body,dict) or set(body) not in ({'id','action','revision'},{'id','action','revision','review_version'}):raise HTTPException(400,'审核字段无效')
        from learning_knowledge import review
        try:
            if body['action'] == 'approve':
                from learning_readiness import require_ready
                await asyncio.to_thread(require_ready, body['id'])
            result=await asyncio.to_thread(review,identity=body['id'],action=body['action'],expected_revision=body['revision'],actor=request.session['admin_username'],expected_content=body.get('review_version'))
            return response(result)
        except ValueError as error:raise HTTPException(409,str(error)) from None

    @router.post('/api/ai/learning-center/revise')
    async def revise_learning(request:Request):
        guard(request,True)
        body=await request.json()
        if not isinstance(body,dict) or set(body) not in ({'id','form','revision'},{'id','form','revision','review_version'}):
            raise HTTPException(400,'修改建议字段无效')
        if not isinstance(body.get('form'),str) or not re.fullmatch(r'[A-Z_]{3,80}',body['form']):
            raise HTTPException(400,'修改建议不在安全枚举中')
        from learning_knowledge import revise
        try:
            return response(await asyncio.to_thread(
                revise,identity=body['id'],form=body['form'],
                expected_revision=body['revision'],actor=request.session['admin_username'],expected_content=body.get('review_version')))
        except ValueError as error:raise HTTPException(409,str(error)) from None

    @router.post('/api/ai/learning-center/correct')
    async def correct_learning(request: Request):
        guard(request, True)
        body = await request.json()
        if not isinstance(body, dict) or set(body) not in ({'id', 'revision', 'commit_fingerprint', 'text'}, {'id', 'revision', 'commit_fingerprint', 'text','review_version'}):
            raise HTTPException(400, '修改审核字段无效')
        if (type(body['revision']) is not int or not isinstance(body['text'], str)
                or not all(isinstance(body[k], str) and re.fullmatch(r'[a-f0-9]{64}', body[k]) for k in ('id','commit_fingerprint'))):
            raise HTTPException(400, '修改审核凭据无效')
        from learning_review import save_correction
        from learning_knowledge import review
        try:
            correction = await asyncio.to_thread(save_correction, body['id'], body['commit_fingerprint'],
                                                  request.session['admin_username'], body['text'], catalog())
            from learning_review import review_correction
            return response(await asyncio.to_thread(review_correction, identity=body['id'],
                expected_revision=body['revision'], actor=request.session['admin_username'], correction=correction,expected_content=body.get('review_version')))
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.get('/api/ai/performance')
    async def performance(request: Request):
        guard(request)
        from performance_metrics import summary
        result=await asyncio.to_thread(summary)
        from ai_settings import retired
        if retired():
            result['groups']=[row for row in result.get('groups',[]) if row.get('provider') in ('Local','AliyunAddress')]
            result['provider_costs']={}
            return response(result)
        from ai_usage_ledger import summary as usage_summary
        usage=await asyncio.to_thread(usage_summary)
        result['ai_usage']=usage
        result['provider_costs']={
            'DeepSeek':{
                'official_actual':usage.get('official_actual',{'status':'UNAVAILABLE'}),
                'today':usage.get('today',{}),
                'month':usage.get('month',{}),
                'rolling_24h':usage.get('rolling_24h',{}),
                'rolling_7d':usage.get('rolling_7d',{}),
                'balance':{'status':'NOT_REFRESHED','reason':'仅在最高管理员手动刷新时读取官方当前余额'},
                'remaining_after_cost':None,
                'auto_recharge':False,
                'scope':'订单AI / Contact AI / 口味候选AI',
            },
        }
        return response(result)

    @router.post('/api/ai/balance')
    async def balance(request:Request):
        guard(request,True)
        from deepseek_balance import fetch
        return response(await asyncio.to_thread(fetch))

    @router.get('/api/ai/offline-address-status')
    async def offline_address_status(request: Request):
        guard(request)
        from region_index import get_region_index
        index=get_region_index()
        if index is None:raise HTTPException(503,'离线行政区库不可用')
        return response(index.inventory())

    @router.post('/api/ai/config')
    async def save(request: Request):
        guard(request, True)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise AIConfigError('配置格式无效')
            result = await asyncio.to_thread(save_settings, body, request.session['admin_username'])
            return response(result)
        except AIConfigError as error:
            raise HTTPException(400, str(error)) from None
        except ValueError:
            raise HTTPException(400, '配置格式无效') from None

    @router.post('/api/ai/key/{action}')
    async def reveal(request: Request, action: str):
        guard(request, True)
        if action not in ('reveal', 'copy'):
            raise HTTPException(400, '操作无效')
        if not encryption_ready():
            raise HTTPException(503, 'AI加密主密钥未配置')
        try:
            key = await asyncio.to_thread(reveal_key, request.session['admin_username'], action.upper())
            return response({'api_key': key})
        except AIConfigError as error:
            raise HTTPException(503, str(error)) from None

    @router.post('/api/ai/test')
    async def connection(request: Request):
        guard(request, True)
        try:
            settings = get_settings()
            result = await asyncio.to_thread(test_connection, settings, request.session['admin_username'])
            result['status'] = public_status()
            return response(result)
        except AIConfigError as error:
            raise HTTPException(503, str(error)) from None

    return router
