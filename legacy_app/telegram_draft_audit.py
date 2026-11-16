"""Allowlisted, payload-free draft authorization facts for persistent logs."""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# API root logging need not enable INFO. Dedicated stderr output is retained
# by systemd journal independently of encrypted queue-payload deletion.
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())

def record(event_id, action, actor, chat_id, callback_message_id, row, authorized, allowed):
    if event_id is None or action not in ('name_generate', 'draft_cancel','address_confirm'):
        return
    reason = ('ALLOWED' if allowed else 'INELIGIBLE' if not authorized else
              'NOT_FOUND' if row is None else 'OWNER_MISMATCH' if actor.id != row['user_id'] else
              'NOT_PENDING' if row['status'] != 'pending' else 'NOT_SELECTED')
    fact = dict(at=datetime.now(timezone.utc).isoformat(), event_id=int(event_id), action=action,
                actor_user_id=actor.id if actor else None, chat_id=str(chat_id),
                callback_message_id=callback_message_id,
                owner_user_id=row['user_id'] if row else None,
                source_message_id=row['message_id'] if row else None,
                draft_status=row['status'] if row else None,
                expiry_policy='NO_TTL', authorized=bool(authorized), allowed=bool(allowed), reason=reason)
    logger.info('DRAFT_AUTH_AUDIT %s', json.dumps(fact, sort_keys=True))
