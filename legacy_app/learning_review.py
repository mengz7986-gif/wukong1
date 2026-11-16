"""Local-only learning correction validation. Never writes business tables."""
import hashlib
import json
from datetime import datetime, timedelta, timezone


def validate(text, catalog):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000:
        raise ValueError('请填写完整识别结果，最多12000字')
    from rules_admin import test_parse
    from address_source_guard import validate_current_region
    result = test_parse(text, catalog)
    errors = list(result.get('diagnostics') or [])
    address = (result.get('contact') or {}).get('address')
    if address:
        region_error = validate_current_region(address)
        if region_error:
            errors.append(str(region_error))
    if errors:
        raise ValueError('；'.join(str(v) for v in errors))
    if not result.get('items') or any(not row.get('product_id') for row in result['items']):
        raise ValueError('商品必须唯一匹配正式目录')
    from category_evidence import prepare
    from rule_dictionary import runtime_snapshot, runtime_store
    from order_parser import OrderParser
    from bot import OrderBot
    from local_fulfillment import attach
    with runtime_snapshot():
        info = OrderParser.extract_order_info(prepare(text, catalog, runtime_store()['aliases']), catalog)
        runtime = object.__new__(OrderBot)
        context = runtime._resolve_order_category_context(info, catalog)
        info = attach(info, catalog, lambda name: runtime._resolve_product_for_order(name, catalog, category_context=context))
    from order_structure import structure_order_text, catalog_names
    result['fulfillment_notes'] = list(structure_order_text(text, names=catalog_names(catalog)).fulfillment_notes)
    result['ai_alternates'] = info.get('ai_alternates') or []
    result['ai_fulfillment'] = info.get('ai_fulfillment') or {}
    result['raw_text'] = text
    result['address'] = (result.get('contact') or {}).get('address')
    return result


def save_correction(identity, fingerprint, actor, text, catalog):
    from learning_evidence import authorized_detail, _cipher, _read, _write, path, MAX_RECORDS
    import fcntl
    import os
    evidence = authorized_detail(identity, actor)
    if not any(row.get('commit_fingerprint') == fingerprint for row in evidence.get('records', [])):
        raise ValueError('缺少对应订单证据，无法审核修改')
    result = validate(text, catalog)
    from rule_dictionary import read_store
    from learning_pipeline import forms_from_validated_order
    candidate = next((row for row in read_store().get('learning_candidates', [])
                      if row.get('id') == identity), None)
    source = next(row for row in evidence['records'] if row.get('commit_fingerprint') == fingerprint)
    original = source.get('original')
    if not candidate or not original:
        raise ValueError('缺少可信原文，不能生成学习规则')
    from category_evidence import prepare
    from order_parser import OrderParser
    from rule_dictionary import runtime_snapshot, runtime_store
    with runtime_snapshot():
        original_info = OrderParser.extract_order_info(prepare(original, catalog, runtime_store()['aliases']), catalog)
        from bot import OrderBot
        from local_fulfillment import attach
        runtime = object.__new__(OrderBot)
        context = runtime._resolve_order_category_context(original_info, catalog)
        original_info = attach(original_info, catalog,
            lambda name: runtime._resolve_product_for_order(name, catalog, category_context=context))
    from order_structure import structure_order_text, catalog_names
    original_info['fulfillment_notes'] = list(structure_order_text(original, names=catalog_names(catalog)).fulfillment_notes)
    original_info['raw_text'] = original
    # A corrected order is not evidence that its newly added syntax existed
    # in the customer's message. Only the intersection may become knowledge.
    supported = set(forms_from_validated_order(original_info)) & set(forms_from_validated_order(result))
    supported = {pair for pair in supported if pair[0] != 'HABIT' or candidate.get('scope') == 'USER'}
    if not supported:
        raise ValueError('修正结果已校验，但原文没有支持可学习规则的证据')
    matching = (candidate.get('kind'), candidate.get('form')) in supported
    now = datetime.now(timezone.utc)
    payload = json.dumps({'text': text, 'result': result}, ensure_ascii=False)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    target = path()
    with open(str(target) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = _read(target)
        rows = [row for row in store.get('corrections', []) if row.get('expires_at', '') > now.isoformat()]
        rows.append({'candidate_id': identity, 'commit_fingerprint': fingerprint,
                     'actor': actor, 'at': now.isoformat(), 'sha256': digest,
                     'expires_at': (now + timedelta(days=90)).isoformat(),
                     'ciphertext': _cipher().encrypt(payload.encode()).decode(),
                     'state': 'VALIDATED_REVIEW_EVIDENCE'})
        store['corrections'] = rows[-MAX_RECORDS:]
        if not matching:
            from learning_knowledge import _identity
            original_record = next((r for r in store.get('records', [])
                if r.get('candidate_id') == identity and r.get('commit_fingerprint') == fingerprint), None)
            if not original_record:
                raise ValueError('原文证据已变化，请刷新后重试')
            for kind, form in sorted(supported):
                new_id = _identity(kind, form, candidate.get('scope', 'GLOBAL'), candidate.get('principal_digest'))
                if not any(r.get('candidate_id') == new_id and r.get('commit_fingerprint') == fingerprint
                           for r in store['records']):
                    store['records'].append(dict(original_record, candidate_id=new_id,
                        learning_type=kind, suggested_form=form, correction_source_id=identity,
                        correction_sha256=digest))
            store['records'] = store['records'][-MAX_RECORDS:]
        _write(target, store)
    return {'sha256': digest, 'commit_fingerprint': fingerprint, 'validator': 'LOCAL_VALIDATED',
            'matches_original_candidate': matching,
            'supported_forms': [list(pair) for pair in sorted(supported)]}


def review_correction(*, identity, expected_revision, actor, correction, expected_content=None):
    """A changed proposal becomes pending, never silently activates knowledge."""
    from learning_knowledge import review, _check_review, _identity, _now, FORMS
    if correction.get('matches_original_candidate'):
        return review(identity=identity, action='approve', expected_revision=expected_revision,
                      actor=actor, correction=correction, expected_content=expected_content)
    from brand_aliases import transaction
    def change(store):
        old = next((r for r in store.get('learning_candidates', []) if r['id'] == identity), None)
        _check_review(old, identity, expected_revision, expected_content)
        if old['status'] not in ('pending', 'withdrawn'):
            raise ValueError('当前候选不能重复审核或替换')
        created = []
        for kind, form in correction['supported_forms']:
            if form not in FORMS.get(kind, set()):
                raise ValueError('学习类型不在安全枚举中')
            new_id = _identity(kind, form, old.get('scope', 'GLOBAL'), old.get('principal_digest'))
            row = next((r for r in store['learning_candidates'] if r['id'] == new_id), None)
            if row is None:
                row = dict(id=new_id, kind=kind, form=form, scope=old.get('scope', 'GLOBAL'),
                           principal_digest=old.get('principal_digest'), status='pending', revision=1,
                           count=1, observations=[correction['commit_fingerprint']],
                           created_at=_now(), updated_at=_now(), suggestion_edited_from=identity,
                           correction_evidence=correction)
                store['learning_candidates'].append(row)
            created.append(new_id)
        old.update(status='superseded', revision=old['revision']+1, updated_at=_now(),
                   reviewed_by=actor, reviewed_at=_now())
        store.setdefault('knowledge_versions', []).append(dict(before={'id':identity,'revision':expected_revision},
            after={'id':identity,'status':'superseded','revision':old['revision']},
            actor=actor, at=_now(), correction=correction, pending_candidates=created))
        store.setdefault('audit', []).append(dict(action='knowledge_correction_replacement', id=identity,
            actor=actor, at=_now(), pending_candidates=created))
        return dict(id=identity, status='superseded', pending_candidates=created, automatic_activation=False)
    return transaction(change)
