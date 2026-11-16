"""Explicit administrator recipient policy; read-only, no implicit fake name.

The administrator may configure settings.json/recipient_policy. A missing or
malformed configuration never authorizes a temporary recipient. This module
does not save settings, modify orders, or contact a logistics provider.
"""
import json
import os
import hashlib
from pathlib import Path


def policy():
    target = Path(os.getenv('RAINBOW_RECIPIENT_POLICY_PATH') or 'settings.json')
    try:
        if not target.exists():
            return {'temporary_names': [], 'require_real_name': False}
        if target.stat().st_size > 1024 * 1024:
            raise ValueError('policy too large')
        section = json.loads(target.read_text(encoding='utf-8')).get('recipient_policy', {})
        name = section.get('temporary_name', '')
        names = section.get('temporary_names', [name] if name else [])
        real = section.get('require_real_name', False)
        if (not isinstance(name, str) or type(real) is not bool or not isinstance(names, list)
                or len(names)>20 or (name and names != [name])):
            raise ValueError('invalid recipient policy')
        from contact_fields import valid_name
        if any(not isinstance(value,str) or value != value.strip() or not valid_name(value) for value in names):
            raise ValueError('invalid temporary label')
        return {'temporary_names': list(dict.fromkeys(names)), 'require_real_name': real}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'temporary_names': [], 'require_real_name': True}


def requires_real_name(fields=None):
    return policy()['require_real_name'] or bool((fields or {}).get('_requires_real_name'))


def temporary_name(fields=None):
    settings = policy()
    if settings['require_real_name'] or (fields or {}).get('_requires_real_name'):
        return ''
    names=settings['temporary_names']
    if not names:
        return ''
    # No random real-name fabrication. Replaying the same durable contact
    # selects the same explicitly authorized label, including after restart.
    contact=fields or {}
    identity=json.dumps([contact.get('phone') or contact.get('recipient_phone'),
                         contact.get('raw_address') or contact.get('address') or contact.get('recipient_address')],
                        ensure_ascii=False,separators=(',',':'))
    index=int(hashlib.sha256(identity.encode()).hexdigest(),16)%len(names)
    return names[index]
