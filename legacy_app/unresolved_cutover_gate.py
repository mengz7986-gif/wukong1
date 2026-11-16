"""Per-ID, fail-closed transition gate for unresolved Telegram outcomes."""
from __future__ import annotations
import hashlib
import json

AMBIGUOUS = {"sending", "unknown", "editing", "deleting", "unknown_edit", "unknown_delete"}

def _index(rows, label):
    if not isinstance(rows, list): raise ValueError(label + "_ROWS_INVALID")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not {"table", "id_ref", "status", "attempts", "row_sha256"} <= set(row): raise ValueError(label + "_ROW_FIELDS_INVALID")
        ident=(row["table"],row["id_ref"])
        if not all(isinstance(x,str) and x for x in ident): raise ValueError(label + "_ROW_ID_INVALID")
        digest=row["row_sha256"]
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest): raise ValueError(label + "_ROW_HASH_INVALID")
        if type(row["attempts"]) is not int or row["attempts"]<0: raise ValueError(label + "_ATTEMPT_COUNT_INVALID")
        if ident in indexed: raise ValueError(label + "_DUPLICATE_ROW_ID")
        indexed[ident]=row
    return indexed

def evaluate_unresolved_transition(*, pre_rows, post_rows, exception_manifest, runtime):
    blockers=[];actions=[]
    try: before=_index(pre_rows,"PRE");after=_index(post_rows,"POST")
    except ValueError as exc: return {"ready":False,"reason":"UNRESOLVED_ROW_EVIDENCE_INVALID","blockers":[str(exc)],"actions":[]}
    if set(before)!=set(after): blockers.append("UNRESOLVED_RECORD_SET_CHANGED")
    added=sorted(set(after)-set(before));removed=sorted(set(before)-set(after))
    changed=sorted(k for k in set(before)&set(after) if any(before[k].get(f)!=after[k].get(f) for f in ("status","attempts","row_sha256")))
    if added: blockers.append("NEW_UNRESOLVED_RECORDS")
    if removed: blockers.append("UNRESOLVED_RECORDS_DISAPPEARED")
    if changed: blockers.append("UNRESOLVED_RECORD_STATE_OR_FINGERPRINT_CHANGED")
    approved={}
    if not isinstance(exception_manifest,dict) or exception_manifest.get("approval_state")!="APPROVED":
        blockers.append("EXACT_HISTORY_EXCEPTION_APPROVAL_UNVERIFIED")
    else:
        entries=exception_manifest.get("records");expected=exception_manifest.get("approved_count")
        if not isinstance(entries,list) or type(expected)is not int or expected!=len(entries): blockers.append("HISTORY_EXCEPTION_MANIFEST_COUNT_INVALID")
        else:
            for row in entries:
                if not isinstance(row,dict) or not {"table","id_ref","row_sha256"}<=set(row): blockers.append("HISTORY_EXCEPTION_ROW_INVALID");continue
                ident=(row["table"],row["id_ref"])
                if ident in approved: blockers.append("DUPLICATE_HISTORY_EXCEPTION_ID")
                approved[ident]=row["row_sha256"]
            if len(approved)!=expected: blockers.append("HISTORY_EXCEPTION_SET_NOT_EXACT")
            digest=hashlib.sha256(json.dumps(sorted((k[0],k[1],v) for k,v in approved.items()),separators=(",",":")).encode()).hexdigest()
            if exception_manifest.get("scope_digest")!=digest: blockers.append("HISTORY_EXCEPTION_SCOPE_DIGEST_MISMATCH")
    for ident,approved_hash in approved.items():
        if ident not in before or ident not in after: blockers.append("APPROVED_HISTORY_ID_NOT_PRESENT_IN_BOTH_SNAPSHOTS")
        elif before[ident]["row_sha256"]!=approved_hash or after[ident]["row_sha256"]!=approved_hash: blockers.append("APPROVED_HISTORY_ROW_FINGERPRINT_MISMATCH")
    if not isinstance(runtime,dict): runtime={}
    required={"services_inactive","main_pids_zero","cgroups_empty","sender_inventory_verified","external_sender_processes","network_inflight_census_verified","active_send_refs","active_leases"}
    if not required<=set(runtime): blockers.append("POST_STOP_RUNTIME_EVIDENCE_INCOMPLETE")
    else:
        for f in ("services_inactive","main_pids_zero","cgroups_empty","sender_inventory_verified","network_inflight_census_verified"):
            if runtime[f] is not True: blockers.append("POST_STOP_"+f.upper()+"_UNVERIFIED")
        if runtime["external_sender_processes"]!=[]: blockers.append("EXTERNAL_SENDER_PROCESS_PRESENT_OR_UNKNOWN")
        if runtime["active_leases"]!=0: blockers.append("ACTIVE_LEASE_PRESENT_OR_UNKNOWN")
        if not isinstance(runtime["active_send_refs"],list): blockers.append("ACTIVE_SEND_CENSUS_INVALID")
        elif runtime["active_send_refs"]: blockers.append("ACTIVE_SEND_REQUEST_PRESENT")
    unapproved=[];active=set()
    if isinstance(runtime.get("active_send_refs"),list):
        for item in runtime["active_send_refs"]:
            if isinstance(item,(tuple,list)) and len(item)==2: active.add((item[0],item[1]))
    for ident,row in after.items():
        if row["status"] in AMBIGUOUS:
            if ident not in approved: unapproved.append(ident)
            if ident in active: blockers.append("AMBIGUOUS_ROW_HAS_ACTIVE_SEND")
    if unapproved: blockers.append("UNAPPROVED_UNRESOLVED_RECORDS_REMAIN")
    blockers=sorted(set(blockers))
    return {"ready":not blockers,"reason":"UNRESOLVED_ROWS_EXACTLY_ISOLATED" if not blockers else "UNRESOLVED_SEND_GATE_BLOCKED",
        "pre_count":len(before),"post_count":len(after),"added_count":len(added),"removed_count":len(removed),"changed_count":len(changed),
        "approved_exact_exception_count":len(approved),"unapproved_unresolved_count":len(unapproved),"actions":actions,"blockers":blockers,
        "delivery_unknown_means_success":False,"auto_replay":False,"mutated":False}
