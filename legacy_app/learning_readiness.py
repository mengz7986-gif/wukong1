"""Read-only evidence projection. Never rewrites candidate review states."""
from datetime import datetime, timezone


def readiness_index():
    from learning_evidence import _read, path, _parse_time, _cipher
    import json
    ready = set()
    now = datetime.now(timezone.utc)
    store = _read(path())
    for row in store.get('records', []):
        expires = _parse_time(row.get('expires_at'))
        local = row.get('local_parser_result') or {}
        ai = row.get('deepseek_analysis') or {}
        validator = row.get('validator_result') or {}
        # AI absence is legitimate for local-only orders. A committed marker
        # alone, without any saved recognition, is not review evidence.
        try:
            raw = _cipher().decrypt(row.get('raw_ciphertext', '').encode()).decode()
            snapshot = json.loads(_cipher().decrypt(row.get('recognition_ciphertext', '').encode()))
        except Exception:
            continue
        corrected = any(c.get('candidate_id') == row.get('correction_source_id', row.get('candidate_id'))
            and c.get('commit_fingerprint') == row.get('commit_fingerprint')
            and c.get('sha256') == row.get('correction_sha256')
            and (_parse_time(c.get('expires_at')) or now) > now
            for c in store.get('corrections', []))
        if (expires and expires > now and raw.strip()
                and row.get('raw_status') == 'AVAILABLE_ENCRYPTED'
                and (snapshot.get('local') or snapshot.get('ai') or corrected)
                and validator.get('status') == 'COMMITTED_VALIDATED'
                and not validator.get('error_count')):
            ready.add(row.get('candidate_id'))
    return ready


def require_ready(identity):
    if identity not in readiness_index():
        raise ValueError('证据不完整，请先从可信记录恢复原文及识别证据，再重新校验')


def bucket(row, ready):
    return 'incomplete' if row.get('id') not in ready else row.get('status', 'unknown')
