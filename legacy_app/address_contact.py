"""Address-only cloud fallback, isolated from products, quantities and pricing."""
import asyncio
from contact_fields import diagnose_contact, merge_contact


async def enrich(fields, draft=None):
    result = dict(fields)
    contact = merge_contact(draft, result)
    raw = str(contact.get('raw_address') or contact.get('address') or '').strip()
    if not raw:
        return result
    status = diagnose_contact(contact)
    if 'address' not in status['missing'] + status['invalid']:
        return result
    from aliyun_address_service import complete
    candidate = await asyncio.to_thread(complete, raw)
    if candidate.get('state') == 'CONFIRM_ADDRESS':
        result['_cloud_address_proposal'] = candidate
    elif candidate.get('state') in ('SAFE', 'PROPOSE_ADMIN_COMPLETION'):
        address = candidate.get('address')
        if isinstance(address, str) and address:
            checked = dict(contact, address=address)
            status = diagnose_contact(checked)
            if 'address' not in status['missing'] + status['invalid']:
                result.update(address=address, raw_address=raw)
    elif candidate.get('reason'):
        result['_address_service_reason'] = candidate['reason']
    return result
