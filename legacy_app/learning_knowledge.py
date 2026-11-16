"""Versioned, private-free observations in the existing dictionary ledger.

No candidate can execute parser code or authorize merchandise/address identity.
Alias learning/review continues to use flavor_aliases and brand_aliases. This
ledger captures only enumerated grammar observations from committed orders.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from contextvars import ContextVar
from brand_aliases import transaction
from rule_dictionary import read_store, runtime_store

FORMS = {
    'QUANTITY': {'ITEM_SUFFIX', 'ITEM_PREFIX', 'SECTION_SUBTOTAL', 'ORDER_TOTAL', 'AMOUNT_EXPRESSION'},
    'ALTERNATE': {'EXPLICIT_BACKUP', 'EXPLICIT_OR', 'OUT_OF_STOCK_REPLACEMENT'},
    'ADDRESS': {'EXPLICIT_ADMIN', 'UNIQUE_ADMIN_COMPLETION', 'MULTILINE_ADDRESS'},
    'HABIT': {'PRODUCT_BEFORE_CONTACT', 'CONTACT_BEFORE_PRODUCT', 'MULTI_SECTION_ORDER', 'ITEM_CHINESE_QUANTITY'},
}
PROOFS = {'COMMITTED_VALIDATED', 'CUSTOMER_CONFIRMED_COMMITTED', 'ADMIN_VERIFIED_COMMITTED'}
_CURRENT_PRINCIPAL=ContextVar('rainbow_learning_principal',default=None)
_CN_DIGITS={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
_CN_QUANTITY=r'(?:[一二三四五六七八九]|十|十[一二三四五六七八九]|[二三四五六七八九]十(?:[一二三四五六七八九])?)'


def _chinese_quantity(value):
    """Strict 1..99 grammar; malformed forms never receive a fallback."""
    import re
    if not re.fullmatch(_CN_QUANTITY,str(value or '')):return None
    if value in _CN_DIGITS:return _CN_DIGITS[value]
    if value=='十':return 10
    if value.startswith('十'):return 10+_CN_DIGITS[value[1]]
    tens=_CN_DIGITS[value[0]]*10
    return tens+(_CN_DIGITS[value[2]] if len(value)==3 else 0)


@contextmanager
def principal_scope(value):
    token=_CURRENT_PRINCIPAL.set(value)
    try:yield
    finally:_CURRENT_PRINCIPAL.reset(token)


def current_principal():return _CURRENT_PRINCIPAL.get()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_version(row):
    """Content token, excluding only additive observation statistics.

    Unknown future fields are material by default. No customer text is
    returned in this token. Approval/status/revision changes remain material.
    """
    return _digest({k:v for k,v in row.items() if k not in
                    {'observations','count','created_at','updated_at','last_seen_at','last_seen'}})


def _check_review(row, identity, expected_revision, expected_content=None):
    if (not row or type(expected_revision) is not int
            or row.get('revision') != expected_revision
            or (expected_content is not None and
                (not isinstance(expected_content,str) or
                 not hmac.compare_digest(review_version(row),expected_content)))):
        raise ValueError('候选已变化，请刷新后重新审核')
    if _identity(row['kind'],row['form'],row.get('scope','GLOBAL'),row.get('principal_digest')) != identity:
        raise ValueError('候选内容与身份不一致，请重新核查')


def _principal(value):
    if value is None:return None
    from config import SESSION_SECRET_KEY
    return hmac.new(SESSION_SECRET_KEY.encode(),str(value).encode(),hashlib.sha256).hexdigest()


def _identity(kind, form, scope='GLOBAL', principal_digest=None):
    if kind not in FORMS or form not in FORMS[kind]:
        raise ValueError('仅允许预定义脱敏表达类型，不接收原文或地址')
    if scope not in ('GLOBAL','USER') or (scope=='USER' and not principal_digest):
        raise ValueError('学习作用域无效')
    return _digest([kind, form, scope, principal_digest if scope=='USER' else None])


def observe(*, kind, form, commit_fingerprint, proof, scope='GLOBAL', principal=None):
    """Internal caller must establish a committed/validated source first.

    No public observation endpoint: a model's claimed confirmation is not
    proof. Only an opaque commit fingerprint is accepted, never user IDs.
    """
    import re
    principal_digest=_principal(principal) if scope=='USER' else None
    identity = _identity(kind, form, scope, principal_digest)
    if proof not in PROOFS or not isinstance(commit_fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', commit_fingerprint):
        raise ValueError('学习仅接受已核验提交证据')
    def change(store):
        rows = store.setdefault('learning_candidates', [])
        row = next((r for r in rows if r['id'] == identity), None)
        if row is None:
            row = dict(id=identity, kind=kind, form=form, scope=scope, principal_digest=principal_digest,
                       status='pending', revision=1,
                       observations=[], count=0, created_at=_now(), updated_at=_now())
            rows.append(row)
        # Stable source keys make retries and multiple workers idempotent.
        if commit_fingerprint not in row['observations']:
            if len(row['observations']) >= 10000:
                raise ValueError('候选证据容量已满，需归档后继续')
            row['observations'].append(commit_fingerprint)
            row['count'] += 1
            # Occurrences are evidence, not a change to the reviewed rule.
            # Existing numeric revisions are retained; no migration/reset.
            row['updated_at'] = _now()
        return {k:row[k] for k in ('id','kind','form','status','count','revision')}
    return transaction(change)


def review(*, identity, action, expected_revision, actor, correction=None, expected_content=None):
    if action not in ('approve', 'withdraw', 'reject') or not isinstance(actor, str) or not actor.strip():
        raise ValueError('审核操作或审核人无效')
    def change(store):
        row = next((r for r in store.get('learning_candidates', []) if r['id'] == identity), None)
        _check_review(row,identity,expected_revision,expected_content)
        if action == 'approve' and row['status'] not in ('pending', 'withdrawn'):
            raise ValueError('此候选不能重复批准')
        if action == 'withdraw' and row['status'] != 'approved':
            raise ValueError('仅可撤回已批准知识')
        if action == 'reject' and row['status'] not in ('pending','withdrawn'):
            raise ValueError('此候选不能重复拒绝；已生效知识请先撤销')
        before = {k:row[k] for k in ('id','kind','form','status','count','revision')}
        row['status'] = {'approve':'approved','withdraw':'withdrawn','reject':'rejected'}[action]
        row.update(revision=row['revision']+1, reviewed_by=actor, reviewed_at=_now())
        row['updated_at'] = row['reviewed_at']
        snapshot = {k:row[k] for k in ('id','kind','form','status','count','revision')}
        versions = store.setdefault('knowledge_versions', [])
        versions.append(dict(version=_digest(snapshot), before=before, after=snapshot, at=_now(), actor=actor,
                             correction=correction))
        store.setdefault('audit', []).append(dict(action='knowledge_'+action,id=identity,actor=actor,at=_now()))
        return snapshot
    return transaction(change)


def revise(*, identity, form, expected_revision, actor, expected_content=None):
    """Edit an enum suggestion, revalidate it, and create a fresh pending ID.

    Arbitrary parser code, regex, products, aliases, addresses, prices and
    inventory facts are never accepted.  Scope and principal ownership stay
    unchanged; an approved row must be withdrawn before revision.
    """
    if not isinstance(actor,str) or not actor.strip():
        raise ValueError('审核人无效')
    def change(store):
        row=next((r for r in store.get('learning_candidates',[]) if r['id']==identity),None)
        _check_review(row,identity,expected_revision,expected_content)
        if row.get('status')=='approved':
            raise ValueError('已生效知识请先撤销，再修改建议')
        kind=row.get('kind')
        if form not in FORMS.get(kind,set()):
            raise ValueError('建议类型不在安全枚举中')
        new_id=_identity(kind,form,row.get('scope','GLOBAL'),row.get('principal_digest'))
        if new_id==identity:
            raise ValueError('建议未发生变化')
        existing=next((r for r in store.get('learning_candidates',[]) if r['id']==new_id),None)
        if existing:
            raise ValueError('修改后的建议已存在，请审核现有候选')
        before={k:row.get(k) for k in ('id','kind','form','scope','status','count','revision')}
        row.update(status='superseded',revision=row['revision']+1,updated_at=_now(),
                   reviewed_by=actor,reviewed_at=_now())
        new=dict(row,id=new_id,form=form,status='pending',revision=1,
                 reviewed_by=None,reviewed_at=None,created_at=_now(),updated_at=_now(),
                 suggestion_edited_from=identity)
        store.setdefault('learning_candidates',[]).append(new)
        after={k:new.get(k) for k in ('id','kind','form','scope','status','count','revision')}
        store.setdefault('knowledge_versions',[]).append(
            dict(version=_digest([before,after]),before=before,after=after,at=_now(),actor=actor,action='revise'))
        store.setdefault('audit',[]).append(
            dict(action='knowledge_revise',id=identity,new_id=new_id,actor=actor,at=_now()))
        return after
    return transaction(change)


def daily_review(*, now=None):
    """Once per Beijing calendar day, with the existing cross-process lock.

    This is a local review digest, not a claim of model training. It never
    activates a candidate. The scheduler/optional AI analysis remain adapters.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('复盘时间必须包含时区')
    day = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    def change(store):
        reports = store.setdefault('learning_daily_reviews', [])
        prior = next((r for r in reports if r['day'] == day), None)
        if prior is not None:
            return prior
        from daily_learning_candidates import build_digest
        aliases = build_digest(store, generated_at=now.isoformat())
        rows = [{k:r[k] for k in ('id','kind','form','status','count','revision')}
                for r in store.get('learning_candidates', [])]
        result = dict(day=day, at=now.isoformat(), mode='LOCAL_REVIEW_ONLY', automatic_activation=False,
                      alias_digest_version=aliases['version'], alias_candidates=aliases['candidate_count'],
                      observations=rows, version=_digest(rows))
        reports.append(result)
        return result
    return transaction(change)


def approved_knowledge(principal=None):
    """Read-only structured hints, never executable replacements or aliases."""
    digest=_principal(principal) if principal is not None else None
    return [{k:r.get(k) for k in ('id','kind','form','scope','revision')} for r in runtime_store().get('learning_candidates', [])
            if r.get('status') == 'approved' and r.get('kind') in FORMS and r.get('form') in FORMS[r['kind']]
            and (r.get('scope','GLOBAL')=='GLOBAL' or (digest and r.get('principal_digest')==digest))]


def matched_approved(forms,principal=None):
    """Return only enumerated reviewed facts actually present in this parse."""
    available={(row['kind'],row['form']):row for row in approved_knowledge(principal)}
    return [available[pair] for pair in forms if pair in available]


def apply_reviewed_grammar(text, products=(), principal=None):
    """Apply only fixed, catalog-bound grammar approved for this principal.

    This cannot create aliases, change contact/address text, or execute stored
    patterns.  The ledger stores only the enum ``ITEM_CHINESE_QUANTITY``; the parser code
    below remains the sole implementation.
    """
    import re
    enabled={(row['kind'],row['form']) for row in approved_knowledge(principal)}
    if ('HABIT','ITEM_CHINESE_QUANTITY') not in enabled:
        return str(text or '')
    names={str(row.get('name') or '') for row in products
           if row.get('name') and row.get('is_active',True) and not row.get('is_archived')}
    categories={str(row.get('parent_name') or '') for row in products
                if row.get('parent_name') and row.get('is_active',True) and not row.get('is_archived')}
    if not names:return str(text or '')
    output=[];in_goods=False
    for line in str(text or '').splitlines():
        stripped=line.strip()
        if stripped in categories:
            in_goods=True;output.append(line);continue
        if re.match(r'^(?:姓名|收件人|收货人|电话|手机|地址|收货地址|配送地址)\s*[:：]',stripped):
            in_goods=False;output.append(line);continue
        match=re.fullmatch(r'\s*(.{1,80}?)[(（]('+_CN_QUANTITY+r')(?:支|根|件|个|条)[)）]\s*',line)
        name=match.group(1).strip() if match else ''
        token=match.group(2) if match else ''
        quantity=_chinese_quantity(token)
        output.append(name+'×'+str(quantity) if in_goods and match and name in names and quantity is not None else line)
    return '\n'.join(output)


def delete_principal(principal, actor):
    """Erase all user-scoped learning for one principal; global rows remain."""
    if principal is None or not str(actor or '').strip():raise ValueError('隐私删除参数无效')
    digest=_principal(principal)
    def change(store):
        rows=store.setdefault('learning_candidates',[])
        removed=[row for row in rows if row.get('scope')=='USER' and hmac.compare_digest(str(row.get('principal_digest') or ''),digest)]
        store['learning_candidates']=[row for row in rows if row not in removed]
        store.setdefault('audit',[]).append(dict(action='knowledge_privacy_delete',count=len(removed),actor=actor,at=_now()))
        return {'removed':len(removed)}
    return transaction(change)


def requires_reviewed_grammar(text, products=(), principal=None):
    """Whether a fixed recognized-but-not-approved grammar must route to AI."""
    import re
    names={str(row.get('name') or '') for row in products
           if row.get('name') and row.get('is_active',True) and not row.get('is_archived')}
    categories={str(row.get('parent_name') or '') for row in products
                if row.get('parent_name') and row.get('is_active',True) and not row.get('is_archived')}
    found=False;in_goods=False
    for line in str(text or '').splitlines():
        stripped=line.strip()
        if stripped in categories:in_goods=True;continue
        if re.match(r'^(?:姓名|收件人|收货人|电话|手机|地址|收货地址|配送地址)\s*[:：]',stripped):
            in_goods=False;continue
        match=re.fullmatch(r'\s*(.{1,80}?)[(（]'+_CN_QUANTITY+r'(?:支|根|件|个|条)[)）]\s*',line)
        if in_goods and match and match.group(1).strip() in names:
            found=True;break
    enabled={row['form'] for row in approved_knowledge(principal) if row['kind']=='HABIT'}
    return found and 'ITEM_CHINESE_QUANTITY' not in enabled
