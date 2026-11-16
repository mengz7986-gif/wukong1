"""Permanent operating-catalog tombstones; historical audit rows stay intact."""
from sqlalchemy import inspect, text
from fastapi import HTTPException


def retired_ids(session, kind):
    if not inspect(session.connection()).has_table('catalog_retirements'):
        return ()
    return tuple(row[0] for row in session.execute(text(
        'SELECT object_id FROM catalog_retirements WHERE kind=:kind'
    ), {'kind': kind}))


def operating_query(session, model, kind):
    ids = retired_ids(session, kind)
    query = session.query(model)
    return query.filter(model.id.notin_(ids)) if ids else query


def require_operating(session, kind, object_id):
    if object_id is not None and int(object_id) in retired_ids(session, kind):
        raise HTTPException(410, '验收对象已永久退出经营目录，仅保留历史审计')
