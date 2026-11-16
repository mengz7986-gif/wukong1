"""Signed, immutable admin selection; no business or schema writes."""
import base64
import hashlib
import hmac
import json
import secrets


def version(order):
    fields = {c.name: str(getattr(order, c.name)) for c in order.__table__.columns}
    fields['items'] = sorted([
        {c.name: str(getattr(item, c.name)) for c in item.__table__.columns}
        for item in order.items
    ], key=lambda row: row['id'])
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def encode(orders, secret):
    if not orders or len(orders) > 5000:
        raise ValueError('请选择1至5000笔订单')
    payload = {'batch': secrets.token_hex(12),
               'orders': [[o.id, version(o)] for o in orders]}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode() + '.' + signature


def decode(token, secret):
    try:
        if len(token) > 600000:
            raise ValueError()
        encoded, signature = token.rsplit('.', 1)
        raw = base64.b64decode(encoded, altchars=b'-_', validate=True)
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(raw)
        rows = payload['orders']
        ids = [r[0] for r in rows]
        if not 0 < len(ids) <= 5000 or len(set(ids)) != len(ids):
            raise ValueError()
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, IndexError):
        raise ValueError('选择批次无效，请重新明确选择订单') from None
