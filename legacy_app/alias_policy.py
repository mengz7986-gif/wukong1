"""Legacy builtin mappings removed after explicit conditional cleanup approval.

The original 170 entries remain only in recoverable release backups/audit
manifests. Empty exported dictionaries keep imports rollback-compatible.
Generation/contact/quantity safety rules and formal manual aliases are retained.
"""
QUARANTINED_TYPES = frozenset({'MODEL_ALIASES', 'FLAVOR_SYNONYMS', 'EXPLICIT_FLAVOR', 'CATEGORY_CHAR_FIX'})


def legacy_aliases_enabled():
    return False
