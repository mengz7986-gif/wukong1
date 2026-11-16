"""Encrypted, bounded evidence for reviewed learning candidates.

Default detail is consistently redacted while preserving layout.  Raw text is
available only through the highest-admin endpoint, with an explicit audit row.
Historical text/model responses that were never retained remain MISSING.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextvars import ContextVar

from cryptography.fernet import Fernet, InvalidToken

RETENTION_DAYS = 90
MAX_RECORDS = 2000
CONTACT_CAPTURE = ContextVar('learning_contact_capture', default=None)


def recognition_status(state, local_success=None):
    """Missing historical output is not proof of a skipped provider call."""
    if not isinstance(state, dict) or not all(k in state for k in ('order', 'contact')):
        return 'HISTORY_UNKNOWN'
    steps = [state['order'], state['contact']]
    if any(s not in ('not_called', 'failed', 'returned', 'validated', 'rejected') for s in steps):
        return 'HISTORY_UNKNOWN'
    if 'rejected' in steps:
        return 'VALIDATOR_REJECTED'
    if 'failed' in steps:
        return 'AI_FAILED_LOCAL_SUCCESS' if local_success is True else ('BOTH_FAILED' if local_success is False else 'AI_FAILED_LOCAL_UNKNOWN')
    if all(s == 'not_called' for s in steps):
        return 'LOCAL_ONLY' if local_success is True else 'NOT_CALLED_LOCAL_UNKNOWN'
    return 'AI_RETURNED'


def _recognition_snapshot(value):
    if not isinstance(value, dict):
        return None
    keys = ('recipient','recipient_name','phone','recipient_phone','address','recipient_address',
            'items','quantity_scopes','section_subtotals','declared_total','computed_total',
            'fulfillment_notes','fulfillment','alternates','ai_alternates','confidence')
    selected = {key: value[key] for key in keys if key in value}
    return selected if len(json.dumps(selected, ensure_ascii=False, default=str).encode()) <= 65536 else None


def path():
    return Path(os.getenv("RAINBOW_LEARNING_EVIDENCE_PATH") or Path(__file__).with_name("learning_evidence.json"))


def audit_path():
    return Path(os.getenv("RAINBOW_LEARNING_ACCESS_AUDIT_PATH") or Path(__file__).with_name("learning_evidence_access.json"))


def _cipher():
    from config import SESSION_SECRET_KEY
    key = hashlib.sha256(("rainbow-learning-evidence-v1:" + SESSION_SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _read(target):
    if not target.exists():
        return {"version": 1, "records": []}
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        raise ValueError("学习证据文件损坏")
    return value


def _write(target, value):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + target.stem + "-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _masked_span(label, value):
    """Keep original line boundaries while removing every character of PII."""
    first=True
    def replace(match):
        nonlocal first
        if first:first=False;return label
        return ''
    return re.sub(r'\S+',replace,str(value or ''))


def _redact(raw, preview):
    """Preserve whitespace/order/punctuation while replacing proven PII spans."""
    result = str(raw or "")
    values = []
    for key in ("recipient_name", "recipient_phone", "recipient_address"):
        value = str(preview.get(key) or "").strip()
        if value:
            values.append((value, {
                "recipient_name": "[姓名已脱敏]",
                "recipient_phone": "[手机号已脱敏]",
                "recipient_address": "[详细地址已脱敏]",
            }[key]))
    try:
        from contact_fields import parse_contact
        parsed = parse_contact(result)
        for key, label in (("recipient", "[姓名已脱敏]"), ("phone", "[手机号已脱敏]"),
                           ("raw_address", "[详细地址已脱敏]"), ("address", "[详细地址已脱敏]")):
            value = str(parsed.get(key) or "").strip()
            if value:
                values.append((value, label))
    except Exception:
        pass
    for value, label in sorted(set(values), key=lambda row: len(row[0]), reverse=True):
        result = result.replace(value, _masked_span(label,value))
        compact = re.sub(r"\s+", "", value)
        if len(compact) >= 2:
            result = re.sub(r"\s*".join(re.escape(char) for char in compact),
                            lambda match:_masked_span(label,match.group(0)),result)
    result = re.sub(r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)", "[手机号已脱敏]", result)
    lines=result.splitlines(keepends=True)
    for index,line in enumerate(lines):
        body=line.rstrip('\r\n');ending=line[len(body):]
        # Fail closed for labelled/obvious contact data that a parser did not
        # confidently extract. Merchandise punctuation and blank lines remain.
        if re.search(r'(?:收件人|联系人|姓名|电话|手机|地址)\s*[:：]',body) or re.search(r'(?:省|市|区|县|街道|路|巷|\d+号|栋|幢|单元|\d+楼|\d+室)',body):
            lines[index]='[联系人信息已脱敏]'+ending
        elif '[手机号已脱敏]' in body:
            # A short adjacent standalone value can be a one-character name.
            for neighbor in (index-1,index+1):
                if 0<=neighbor<len(lines):
                    candidate=lines[neighbor].rstrip('\r\n')
                    suffix=lines[neighbor][len(candidate):]
                    if re.fullmatch(r'[\u4e00-\u9fffA-Za-z·.]{1,12}',candidate.strip()):
                        lines[neighbor]='[姓名已脱敏]'+suffix
    result=''.join(lines)
    return result


def _safe_items(preview):
    rows = []
    for item in preview.get("items") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "category": str(item.get("parent_name") or "")[:100],
            "product": str(item.get("product_name") or item.get("display_name") or "")[:100],
            "quantity": int(item.get("quantity") or 0),
        })
    return rows[:100]


def _safe_parse(value,preview):
    if not isinstance(value,dict):return None
    allowed={str(item.get(key) or '') for item in preview.get('items') or [] if isinstance(item,dict)
             for key in ('parent_name','product_name','display_name') if str(item.get(key) or '')}
    def product_text(raw):
        text=str(raw or '')[:100]
        if text in allowed:return text
        match=re.fullmatch(r'品类\(([^()]*)\)口味\(([^()]*)\)',text)
        if match and all(token in allowed for token in match.groups()):return text
        return '[未验证字段已隐藏]' if text else ''
    def number(raw,kind=float):
        if type(raw) not in (int,float) or isinstance(raw,bool):return None
        try:return kind(raw)
        except (TypeError,ValueError,OverflowError):return None
    items=[]
    for item in value.get('items') or []:
        if isinstance(item,dict):
            items.append({'category':product_text(item.get('category') or item.get('parent_name')),
                          'product':product_text(item.get('flavor') or item.get('product_name')),
                          'quantity':number(item.get('quantity'),int)})
        elif isinstance(item,(list,tuple)) and len(item)>=2:
            items.append({'product':product_text(item[0]),'quantity':number(item[1],int)})
    scopes=[]
    for fact in value.get('quantity_scopes') or []:
        if not isinstance(fact,dict) or fact.get('kind') not in ('ITEM_QUANTITY','SECTION_SUBTOTAL','ORDER_TOTAL'):continue
        scopes.append({'kind':fact['kind'],'quantity':number(fact.get('quantity'),int),
                       'section':product_text(fact.get('section')) if fact.get('section') else None,
                       'item':product_text(fact.get('item')) if fact.get('item') else None})
    return {'items':items[:100],'quantity_scopes':scopes[:200],
            'declared_total':number(value.get('declared_total'),int),
            'computed_total':number(value.get('computed_total'),int),
            'contact_presence':{key:bool(value.get(key)) for key in ('recipient','phone','address')},
            'confidence':number(value.get('confidence',value.get('ai_confidence')))}


def record(*, candidate_id, commit_fingerprint, kind, form, preview, order):
    raw = str(preview.get("raw_text") or "")
    now = datetime.now(timezone.utc)
    order_numbers=order.get('order_numbers') or ([order.get('order_number')] if order.get('order_number') else [])
    order_numbers=[str(value)[:80] for value in order_numbers if re.fullmatch(r'[A-Za-z0-9_-]{1,80}',str(value or ''))][:100]
    record = {
        "candidate_id": candidate_id,
        "commit_fingerprint": commit_fingerprint,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=RETENTION_DAYS)).isoformat(),
        "learning_type": kind,
        "suggested_form": form,
        "redacted_original": _redact(raw, preview) if raw else None,
        "raw_ciphertext": _cipher().encrypt(raw.encode()).decode() if raw else None,
        "raw_status": "AVAILABLE_ENCRYPTED" if raw else "MISSING_NOT_PERSISTED",
        "recognition_ciphertext": _cipher().encrypt(json.dumps({
            "ai": _recognition_snapshot(preview.get('_learning_deepseek_analysis')),
            "local": _recognition_snapshot(preview.get('_learning_local_parser_result')),
            "ai_called": preview.get('_learning_ai_called'),
            "ai_state": preview.get('_learning_ai_state'),
            "local_success": bool(order.get('id') or order_numbers) if preview.get('_learning_ai_state') else None,
        }, ensure_ascii=False, default=str).encode()).decode(),
        "deepseek_analysis": ({"status":"NORMALIZED_PROVIDER_RESULT","result":_safe_parse(preview.get('_learning_deepseek_analysis'),preview)}
                              if preview.get('_learning_deepseek_analysis') else {
            "status": "MISSING_NOT_PERSISTED",
            "reason": "Historical provider evidence was not retained and is not reconstructed.",
        }),
        "local_parser_result": {
            "status": "RAW_LOCAL_PARSER_RESULT" if preview.get('_learning_local_parser_result') else "MISSING_NOT_PERSISTED",
            "result": _safe_parse(preview.get('_learning_local_parser_result'),preview),
        },
        "validator_result": {"status":"COMMITTED_VALIDATED","result":_safe_parse(preview.get('_learning_validator_result') or {},preview),
            "error_count":len((preview.get('_learning_validator_result') or {}).get('errors') or []),
            "order_reference_present":bool(order.get("id") or order.get("order_number") or order.get("order_numbers"))},
        "final_business_result": {"status":"COMMITTED","order_count":len(order.get('order_numbers') or []) or 1,
            "order_numbers":order_numbers,
            "items":([{"product_id":item.get('product_id') if type(item.get('product_id')) is int else None,
                       "quantity":item.get('quantity') if type(item.get('quantity')) is int else None}
                      for item in order.get('items') if isinstance(item,dict)] if isinstance(order.get('items'),list)
                     else {"status":"MISSING_NOT_RETURNED_BY_COMMIT_API"})},
        "trace_id": preview.get('_order_trace_id') if isinstance(preview.get('_order_trace_id'),str) and re.fullmatch(r'[a-f0-9]{32}',preview['_order_trace_id']) else None,
        "evidence_status": "CONFIRMED_COMMITTED",
    }
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = _read(target)
        previous=next((r for r in value['records'] if r.get('candidate_id')==candidate_id
                       and r.get('commit_fingerprint')==commit_fingerprint),None)
        def material(entry):
            # Compare plaintext locally; randomized encryption/timestamps are
            # not a change in recognition content. Never persist plaintext.
            result={k:v for k,v in entry.items() if k not in
                    {'created_at','expires_at','raw_ciphertext','recognition_ciphertext'}}
            for key in ('raw_ciphertext','recognition_ciphertext'):
                encrypted=entry.get(key)
                result[key]=_cipher().decrypt(encrypted.encode()).decode() if encrypted else None
            return hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        changed=previous is not None and material(previous)!=material(record)
        if changed:
            # Invalidate the review token BEFORE replacing evidence, under
            # the dictionary transaction. A racing reviewer fails closed.
            from brand_aliases import transaction
            def invalidate(store):
                row=next((r for r in store.get('learning_candidates',[]) if r.get('id')==candidate_id),None)
                if row is not None:
                    before=row['revision']
                    row.update(revision=before+1,updated_at=now.isoformat())
                    if row.get('status')=='approved':row['status']='withdrawn'
                    store.setdefault('audit',[]).append(dict(action='knowledge_evidence_changed',
                        id=candidate_id,before_revision=before,after_revision=row['revision'],
                        actor='system:evidence',at=now.isoformat()))
        cutoff = now.isoformat()
        rows = [
            row for row in value["records"]
            if str(row.get("expires_at") or "") >= cutoff
            and not (row.get("candidate_id") == candidate_id and row.get("commit_fingerprint") == commit_fingerprint)
        ]
        rows.append(record)
        value["records"] = rows[-MAX_RECORDS:]
        value['corrections'] = [row for row in value.get('corrections', [])
                                if row.get('expires_at', '') > cutoff][-MAX_RECORDS:]
        value["retention_days"] = RETENTION_DAYS
        if changed:
            def persist(store):
                invalidate(store)
                _write(target,value)
            transaction(persist)
        else:
            _write(target, value)
    return record


def detail(candidate_id):
    now=datetime.now(timezone.utc)
    rows = [row for row in _read(path()).get("records", []) if row.get("candidate_id") == candidate_id
            and (_stamp:=_parse_time(row.get('expires_at'))) is not None and _stamp>now]
    if not rows:
        return {
            "candidate_id": candidate_id,
            "evidence_status": "MISSING",
            "reason": "Historical source/model evidence was never persisted; it is not reconstructed.",
            "records": [],
            "raw_access": "UNAVAILABLE",
        }
    safe = []
    for row in rows[-20:]:
        safe.append({key: row.get(key) for key in (
            "commit_fingerprint", "created_at", "expires_at", "learning_type",
            "suggested_form", "redacted_original", "raw_status", "deepseek_analysis",
            "local_parser_result", "validator_result", "final_business_result", "evidence_status","trace_id",
        )})
    return {
        "candidate_id": candidate_id,
        "evidence_status": "AVAILABLE",
        "records": safe,
        "raw_access": "HIGHEST_ADMIN_AUDITED",
        "retention_days": RETENTION_DAYS,
    }


def _parse_time(value):
    try:
        stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except Exception:return None


def authorized_detail(candidate_id, actor):
    """Explicit permission is checked by the caller; every reveal is audited."""
    result = detail(candidate_id)
    stored = _read(path()).get('records', [])
    for entry in result.get('records', []):
        fingerprint = entry['commit_fingerprint']
        row = next(r for r in stored if r.get('candidate_id') == candidate_id
                   and r.get('commit_fingerprint') == fingerprint)
        if row.get('raw_ciphertext'):
            entry['original'] = reveal_raw(candidate_id, fingerprint, actor,
                                           '管理员学习审核查看完整订单')['raw_original']
        if row.get('recognition_ciphertext'):
            try:
                snapshot = json.loads(_cipher().decrypt(row['recognition_ciphertext'].encode()))
            except (InvalidToken, ValueError, TypeError):
                snapshot = {}
            entry['ai_snapshot'] = snapshot.get('ai')
            entry['local_snapshot'] = snapshot.get('local')
            entry['ai_called'] = snapshot.get('ai_called')
            entry['ai_state'] = snapshot.get('ai_state')
            entry['ai_status'] = recognition_status(snapshot.get('ai_state'), snapshot.get('local_success'))
        # Corrections remain a separate administrator artifact; never replace
        # the historical provider/local snapshots with a regenerated result.
        corrections = [c for c in _read(path()).get('corrections', [])
                       if c.get('candidate_id') == row.get('correction_source_id', candidate_id)
                       and c.get('commit_fingerprint') == fingerprint
                       and (_parse_time(c.get('expires_at')) or datetime.now(timezone.utc)) > datetime.now(timezone.utc)]
        if corrections:
            correction = corrections[-1]
            try:
                payload = _cipher().decrypt(correction['ciphertext'].encode())
                if hashlib.sha256(payload).hexdigest() != correction.get('sha256'):
                    raise ValueError('correction digest mismatch')
                entry['administrator_correction'] = dict(text=json.loads(payload)['text'],
                    actor=correction.get('actor'), at=correction.get('at'), sha256=correction.get('sha256'))
            except (InvalidToken, ValueError, KeyError, TypeError):
                entry['administrator_correction_status'] = 'UNAVAILABLE'
    return result


def reveal_raw(candidate_id, commit_fingerprint, actor, reason):
    if not actor or not isinstance(reason,str) or not (3<=len(reason.strip())<=200):
        raise ValueError("访问审计缺少管理员")
    now=datetime.now(timezone.utc)
    row = next((
        row for row in _read(path()).get("records", [])
        if row.get("candidate_id") == candidate_id and row.get("commit_fingerprint") == commit_fingerprint
    ), None)
    if not row or not row.get("raw_ciphertext"):
        raise ValueError("原始证据不存在，不能回填或推测")
    expires=_parse_time(row.get('expires_at'))
    if not expires or expires<=now:raise ValueError('原始证据已过保留期')
    try:
        raw = _cipher().decrypt(row["raw_ciphertext"].encode()).decode()
    except (InvalidToken, UnicodeError, ValueError):
        raise ValueError("原始证据无法解密") from None
    target = audit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = _read(target)
        value["records"] = (value["records"] + [{
            "candidate_id": candidate_id,
            "commit_fingerprint": commit_fingerprint,
            "actor": str(actor)[:100],
            "reason":reason.strip(),
            "action": "REVEAL_MINIMUM_RAW_EVIDENCE",
            "at": datetime.now(timezone.utc).isoformat(),
        }])[-MAX_RECORDS:]
        _write(target, value)
    return {"raw_original": raw, "cache_control": "no-store"}
