"""Aliyun address adapter with legacy strict and cloud-primary policies.

Cloud-primary completion does not require local coverage. Unproven additions
require customer confirmation; detailed delivery text is never replaced.
"""
from dataclasses import dataclass
import re
import json
from address_admin_equivalence import same,represented_at_source_start


ADMIN_FIELDS = ('province', 'city', 'district')
DETAIL_FIELDS = ('town', 'road', 'roadno', 'houseno', 'floorno', 'roomno')


def _clean(value):
    return re.sub(r'\s+', '', str(value or '')).strip()


def _fields(payload):
    if not isinstance(payload, dict):
        return {}
    source = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    if 'Data' in payload:
        source=payload['Data']
        if isinstance(source,str):source=json.loads(source)
        if not isinstance(source,dict) or source.get('status')!='OK':raise ValueError('ADDRESS_RESPONSE_INVALID')
        encoded=source.get('structure') or source.get('complete')
        if not isinstance(encoded,str):raise ValueError('ADDRESS_RESPONSE_INVALID')
        parsed={}
        for part in encoded.split('\t'):
            key,separator,value=part.partition('=')
            if separator:
                if key in parsed and parsed[key]!=value:raise ValueError('ADDRESS_RESPONSE_CONFLICT')
                parsed[key]=value
        source=parsed
    aliases = {
        'province': ('province', 'prov'), 'city': ('city',),
        'district': ('district', 'area'), 'town': ('town', 'township'),
        'road': ('road',), 'roadno': ('roadno', 'road_no', 'roadNo'),
        'houseno': ('houseno', 'house_no'), 'floorno': ('floorno', 'floor_no'),
        'roomno': ('roomno', 'room_no'),
    }
    result = {}
    for target, names in aliases.items():
        for name in names:
            value = _clean(source.get(name))
            if value:
                result[target] = value
                break
    return result


@dataclass(frozen=True)
class GeographicProof:
    field: str
    value: str
    unique: bool
    source: str


class AliyunAddressCandidate:
    """Generate a fail-closed completion proposal without business writes."""

    def __init__(self, structure, complete, prove, *, cloud_primary=False):
        self.structure = structure
        self.complete = complete
        self.prove = prove
        self.cloud_primary = cloud_primary

    def evaluate(self, raw_address):
        raw = _clean(raw_address)
        if not raw:
            return self._failed(raw, 'ADDRESS_MISSING')
        try:
            structured = _fields(self.structure(raw))
        except TimeoutError:
            # Local daily/monthly budget exhaustion is an operational state,
            # not evidence that the customer's address is malformed.
            raise
        except Exception:
            return self._failed(raw, 'STRUCTURE_UNAVAILABLE')

        if not structured:return self._failed(raw,'STRUCTURE_EMPTY')
        missing_admin = [key for key in ADMIN_FIELDS if not structured.get(key) or structured[key] not in raw]
        detail_changes = [key for key in DETAIL_FIELDS if structured.get(key) and structured[key] not in raw]
        if detail_changes:
            return self._failed(raw, 'UNSUPPORTED_DETAIL_GUESS', rejected=detail_changes)

        candidates = dict(structured)
        if missing_admin:
            try:
                completed = _fields(self.complete(raw))
            except TimeoutError:
                raise
            except Exception:
                return self._failed(raw, 'COMPLETE_UNAVAILABLE')
            for key in ADMIN_FIELDS:
                if completed.get(key):
                    if structured.get(key) and not same(key,structured[key],completed[key]):
                        return self._failed(raw,'CLOUD_ADMIN_CONFLICT',rejected=[key])
                    candidates.setdefault(key, completed[key])

        accepted = []
        ambiguous = False
        for field in ADMIN_FIELDS:
            value = candidates.get(field)
            if not value or value in raw:
                continue
            try:proof = self.prove(raw, field, value)
            except Exception:
                if not self.cloud_primary:raise
                proof = None
            if self.cloud_primary:
                ambiguous = ambiguous or not (isinstance(proof, GeographicProof) and proof.unique)
                accepted.append((field, value, 'aliyun_structure_complete'))
                continue
            if not isinstance(proof, GeographicProof) or proof.field != field or _clean(proof.value) != value:
                return self._failed(raw, 'INDEPENDENT_PROOF_INVALID', rejected=[field])
            if not proof.unique or not _clean(proof.source) or proof.source == 'aliyun_structure_complete':
                return self._failed(raw, 'INDEPENDENT_PROOF_NOT_UNIQUE', rejected=[field])
            accepted.append((field, value, proof.source))

        if missing_admin and not accepted:
            return self._failed(raw, 'NO_SAFE_COMPLETION')
        completed_address = raw
        for field, value, _ in accepted:
            if value in completed_address:
                continue
            # Only after the independent proof above: retain a proven source
            # spelling rather than prefixing both 浙江省 and already-written 浙江.
            if represented_at_source_start(raw,field,value,candidates):continue
            if field in ('city','district'):
                anchors = ([candidates.get('province')] if field=='city' else [candidates.get('city'), candidates.get('province')])
                anchor = next((item for item in anchors if item and item in completed_address), None)
                if anchor:
                    completed_address = completed_address.replace(anchor, anchor + value, 1)
                    continue
            completed_address = value + completed_address
        return {
            'state': 'CONFIRM_ADDRESS' if ambiguous else 'SAFE' if not accepted else 'PROPOSE_ADMIN_COMPLETION',
            'address': completed_address,
            'source_address': raw,
            'accepted_fields': [dict(field=f, value=v, evidence_source=s) for f, v, s in accepted],
            'cloud_fields': candidates,
        }

    @staticmethod
    def _failed(raw, reason, rejected=()):
        return {
            'state': 'FAIL_COMPLETE_RESEND', 'address': raw,
            'reason': reason, 'rejected_fields': list(rejected),
            'message': '收货地址不完整或无法安全补全，请补全省市区及详细地址后重新发送完整订单。',
        }
