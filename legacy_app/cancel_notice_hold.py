"""Independent exact15920 and exact18086 cancellation holds, never replayed.

Only digests of the approved original event and artifact are retained here.
Other notifications for the same order do not share this event identity.
"""
import contextlib
import contextvars
import hashlib
import json
import logging

ORDER_ID = 15920
EVENT_REF = '9bf7cc66a9740ef5e128f65676bbe5ded38ba68ac95445155817b61ae33f7338'
ROW_SHA256 = '942112d28dfed6cee9d944720af5f6596d8693d2f298069af323cf7e531abc20'
FACT_SHA256 = '54950875183eaa1f3ad118a866d360f1451ff0cb8e878d1f993435d3260d0557'
ARTIFACT_SHA256 = '7f3029ac86bcce2d7a4670c79b13ca224e55a1be700f0d0bed4f2916b785b910'

ORDER_18086 = 18086
EVENT_18086_REF = '69d22791963d47b87a3e702c28aedf698d9e20172b37a1388f928549710c4304'
ROW_18086_SHA256 = 'ffb9c732b0734c9d7226c645fd569550b2aeb4257fad6ba8500bca5b9784e230'
FACT_18086_SHA256 = '517bb4d8c746c50a5a5c8f1da2d7586d89c9f095dace84d1eae4cfb575a6b600'
ARTIFACT_18086_SHA256 = 'ea3f4c3119469e2bbb0db17175b7098b8c25451c5706dfa589b2187e9b9b7723'
CURRENT_EVENT = contextvars.ContextVar('cancellation_send_identity', default=None)


class CancellationReplayHeld(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def event_ref(key):
    return hashlib.sha256(str(key).encode()).hexdigest()


def artifact(payload):
    # Same original artifact with Reply omitted after a definitive rejection is
    # still this cancellation. Formatting flags cannot turn it into a new event.
    return digest({'chat_id': str(payload.get('chat_id')), 'text': payload.get('text')})


def held(key):
    return key is not None and event_ref(key) in (EVENT_REF, EVENT_18086_REF)


def deny(channel):
    logging.getLogger(__name__).warning(
        'CANCEL_15920_ORIGINAL_EVENT_HELD event_ref=%s channel=%s attempt_created=false',
        EVENT_REF, channel)
    raise CancellationReplayHeld('CANCEL_15920_ORIGINAL_EVENT_HELD')


def deny_18086(channel):
    reason='CANCEL_18086_ORIGINAL_EVENT_HELD'
    logging.getLogger(__name__).warning('%s event_ref=%s channel=%s attempt_created=false',reason,EVENT_18086_REF,channel)
    raise CancellationReplayHeld(reason)


def guard_event(key, channel):
    if key is not None and event_ref(key)==EVENT_18086_REF:
        deny_18086(channel)
    if held(key):
        deny(channel)


@contextlib.contextmanager
def sending(key):
    guard_event(key, 'cancel_sender')
    token = CURRENT_EVENT.set(event_ref(key))
    try:
        yield
    finally:
        CURRENT_EVENT.reset(token)


def guard_transport(payload, key=None):
    # Identity travels through the sync cancellation path and async account
    # context. The pinned artifact additionally closes legacy identity-less
    # direct calls for this exact target+original text, never an order-wide ban.
    from notice_observation import CURRENT
    ref = CURRENT_EVENT.get() or (CURRENT.get() or {}).get('event_ref')
    if (key is not None and event_ref(key)==EVENT_18086_REF) or ref==EVENT_18086_REF or artifact(payload)==ARTIFACT_18086_SHA256:
        deny_18086('transport')
    if held(key) or ref == EVENT_REF or artifact(payload) == ARTIFACT_SHA256:
        deny('transport')


def verify_15920(connection):
    """Independent read-only companion gate; does not replace the 92 review."""
    facts = [dict(r) for r in connection.execute(
        'SELECT * FROM order_cancellation_facts WHERE order_id=?', (ORDER_ID,))]
    if len(facts) != 1 or digest(facts[0]) != FACT_SHA256:
        raise RuntimeError('CANCEL_15920_FACT_CHANGED')
    key = 'cancel:' + facts[0]['event_id']
    if event_ref(key) != EVENT_REF:
        raise RuntimeError('CANCEL_15920_EVENT_IDENTITY_CHANGED')
    row = connection.execute('SELECT * FROM telegram_notification_events WHERE event_key=?', (key,)).fetchone()
    if row is None or digest(dict(row)) != ROW_SHA256:
        raise RuntimeError('CANCEL_15920_HISTORY_CHANGED')
    row = dict(row)
    if row['status'] != 'sending' or row['attempts'] != 1 or row['sent_at'] is not None:
        raise RuntimeError('CANCEL_15920_ORIGINAL_STATE_REQUIRED')
    order = connection.execute('SELECT status FROM orders WHERE id=?', (ORDER_ID,)).fetchone()
    if order is None or order[0] != 'cancelled':
        raise RuntimeError('CANCEL_15920_ORDER_FACT_CHANGED')
    payload = json.loads(row['payload'])
    if artifact(dict(payload, chat_id=row['chat_id'])) != ARTIFACT_SHA256:
        raise RuntimeError('CANCEL_15920_ARTIFACT_CHANGED')
    for other in connection.execute(
            "SELECT event_key,chat_id,payload FROM telegram_notification_events WHERE kind='cancelled' AND chat_id=? AND event_key<>?",
            (row['chat_id'], key)):
        if artifact(dict(json.loads(other['payload']), chat_id=other['chat_id'])) == ARTIFACT_SHA256:
            raise RuntimeError('CANCEL_15920_DUPLICATE_ARTIFACT')
    return {'event_ref': EVENT_REF, 'historical_state': 'sending', 'attempts': 1,
            'delivery_outcome': 'UNPROVEN', 'future_replay': 'HELD'}


def verify_18086(connection):
    """Independent immutable cancellation proof, never a delivery ACK claim."""
    facts=[dict(r) for r in connection.execute('SELECT * FROM order_cancellation_facts WHERE order_id=?',(ORDER_18086,))]
    if len(facts)!=1 or digest(facts[0])!=FACT_18086_SHA256:
        raise RuntimeError('CANCEL_18086_FACT_CHANGED')
    key='cancel:'+facts[0]['event_id']
    if event_ref(key)!=EVENT_18086_REF:
        raise RuntimeError('CANCEL_18086_EVENT_IDENTITY_CHANGED')
    row=connection.execute('SELECT * FROM telegram_notification_events WHERE event_key=?',(key,)).fetchone()
    if row is None or digest(dict(row))!=ROW_18086_SHA256:
        raise RuntimeError('CANCEL_18086_HISTORY_CHANGED')
    row=dict(row)
    if row['status']!='sending' or row['attempts']!=1 or row['sent_at'] is not None or row['kind']!='cancelled':
        raise RuntimeError('CANCEL_18086_ORIGINAL_STATE_REQUIRED')
    order=connection.execute('SELECT status FROM orders WHERE id=?',(ORDER_18086,)).fetchone()
    if order is None or order[0]!='cancelled':
        raise RuntimeError('CANCEL_18086_ORDER_FACT_CHANGED')
    payload=json.loads(row['payload'])
    if artifact(dict(payload,chat_id=row['chat_id']))!=ARTIFACT_18086_SHA256:
        raise RuntimeError('CANCEL_18086_ARTIFACT_CHANGED')
    for other in connection.execute("SELECT event_key,chat_id,payload FROM telegram_notification_events WHERE kind='cancelled' AND chat_id=? AND event_key<>?",(row['chat_id'],key)):
        if artifact(dict(json.loads(other['payload']),chat_id=other['chat_id']))==ARTIFACT_18086_SHA256:
            raise RuntimeError('CANCEL_18086_DUPLICATE_ARTIFACT')
    return dict(event_ref=EVENT_18086_REF,historical_state='sending',attempts=1,delivery_outcome='UNPROVEN',future_replay='HELD')


def verify(connection):
    # All existing release callers now require BOTH independent checks. held()
    # alone never admits a new row without this exact companion verification.
    original=verify_15920(connection)
    companion=verify_18086(connection)
    return dict(original,independent_18086=companion)
