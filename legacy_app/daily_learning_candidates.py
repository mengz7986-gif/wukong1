"""Read-only daily learning digest over the existing Alias workflow.

This module never schedules itself, calls a model, changes an Alias status, or
stores customer text. A future paused scheduler may export this digest for a
human reviewer; approval remains in brand_aliases/flavor_aliases.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import unicodedata


def build_digest(store, *, generated_at=None):
    if not isinstance(store, dict):
        raise ValueError('学习候选数据无效')
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    candidates = []
    grouped = {}
    for source in ('brand_candidates', 'aliases'):
        for row in store.get(source, ()): 
            if not isinstance(row, dict) or row.get('status') != 'pending':
                continue
            alias = str(row.get('alias') or '').strip()
            if not alias or any(key in row for key in ('recipient', 'phone', 'address', 'raw_text')):
                continue
            from brand_aliases import clean_alias
            try:
                clean_alias(alias)
                clean_alias(str(row.get('standard') or ''))
            except ValueError:
                continue
            kind = row.get('type') or ('MODEL_ALIASES' if source == 'brand_candidates' else 'UNKNOWN')
            key = (kind, unicodedata.normalize('NFKC', alias).casefold(),
                   row.get('standard'), row.get('category'), row.get('scope'),
                   row.get('warehouse_id'), row.get('category_id'), row.get('product_id'))
            count = max(1, int(row.get('count') or row.get('occurrences') or 1))
            if key in grouped:
                grouped[key]['occurrences'] += count
                grouped[key]['review_ids'].append(row.get('id'))
                continue
            candidate = {
                'id': row.get('id'), 'type': row.get('type'), 'alias': alias,
                'standard': row.get('standard'), 'category': row.get('category'),
                'status': 'pending', 'source': source,
                'occurrences': count, 'review_ids': [row.get('id')],
                'analysis': '仅候选；需当前目录、作用域和冲突检查后人工审核',
            }
            candidate['type'] = kind
            grouped[key] = candidate
            candidates.append(candidate)
    candidates.sort(key=lambda row: (-row['occurrences'], row['type'] or '', row['alias']))
    version = hashlib.sha256(json.dumps(candidates, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {
        'generated_at': generated_at,
        'mode': 'REVIEW_ONLY', 'automatic_activation': False,
        'model_training': False, 'scheduler_enabled': False,
        'candidate_count': len(candidates),
        'by_type': dict(Counter(row['type'] or 'UNKNOWN' for row in candidates)),
        'candidates': candidates,
        'version': version, 'review_route': '/admin/rules',
        'withdrawal': '沿用原Alias停用与候选忽略；不得撤销其他既有映射',
    }
