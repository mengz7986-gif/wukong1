"""Safe rejection evidence: structure and catalog fields, never customer text."""
import hashlib
import json
import logging
import re

logger=logging.getLogger(__name__)


def summarize(raw,info=None,ai=None,rejected_fields=(),reason=''):
    info=info if isinstance(info,dict) else {}; facts=info.get('quantity_scopes') or []
    by_line={f.get('line_number'):f for f in facts}
    from deepseek_prompt import MODEL_ALIASES
    from alias_policy import legacy_aliases_enabled
    known_categories=set(MODEL_ALIASES.values()) if legacy_aliases_enabled() else set()
    rows=[]
    for number,line in enumerate(str(raw or '').splitlines(),1):
        if re.search(r'authorization|api[_ -]?key|master[_ -]?key|sk-[a-z]',line,re.I):
            continue
        if not re.search(r'\d',line):continue
        fact=by_line.get(number,{})
        phone=bool(re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)',line))
        # Keep numeric separators/context shape only. Chinese/English text can
        # contain names, full addresses or secrets, so it is never logged here.
        shape=re.sub(r'[^\d\s+＋=＝xX×*,:：;；()（）.-]','□',line)
        shape=re.sub(r'\d{7,}','[长数字已脱敏]',shape)
        shape=re.sub(r'□+','□',shape)[:160]
        rows.append(dict(line_number=number,numeric_context=shape,
            category=fact.get('section') if fact.get('section') in known_categories else None,
            section_index=fact.get('section_index'),
            section_fingerprint=hashlib.sha256(str(fact.get('section') or '').encode()).hexdigest()[:12],
            kind='CONTACT_PHONE' if phone else fact.get('kind','UNCLASSIFIED')))
    ai_fields=sorted(set(ai if isinstance(ai,dict) else {}) & {'items','recipient','phone','address','missing','confidence','needs_manual_review','is_aftersale','total_quantity','declared_total'})
    # Field presence is safe; field values and full source_text/Prompt are not.
    return dict(text_fingerprint=hashlib.sha256(str(raw or '').encode()).hexdigest(),
        lines=rows,ai_returned_fields=ai_fields,
        ai_rejected_fields=[str(x) for x in rejected_fields if re.fullmatch(r'[A-Za-z_]+',str(x))],
        rejection_reason_code=reason if re.fullmatch(r'[A-Z_]+',reason) else 'UNCLASSIFIED_REJECTION')


def log_rejection(raw,info=None,ai=None,rejected_fields=(),reason=''):
    logger.info('parser_diagnostic %s',json.dumps(summarize(raw,info,ai,rejected_fields,reason),ensure_ascii=False))
