"""Transactional, versioned whole-warehouse allocation settings."""
import json
from sqlalchemy import text

def load(session):
    session.execute(text('CREATE TABLE IF NOT EXISTS warehouse_routing_policy '
                         '(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)'))
    row=session.execute(text('SELECT revision,payload FROM warehouse_routing_policy WHERE id=1')).first()
    if not row:
        return dict(revision=0,weights={},stock_priority=True)
    return dict(json.loads(row[1]),revision=row[0])

def save(session, payload):
    from models import Warehouse
    if session.bind.dialect.name=='sqlite' and not session.in_transaction():
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    current=load(session)
    if set(payload)!={'revision','weights','stock_priority'} or type(payload['revision']) is not int or payload['revision']!=current['revision']:
        raise ValueError('设置已变化，请刷新后重试')
    if type(payload['stock_priority']) is not bool or not isinstance(payload['weights'],dict):
        raise ValueError('分配设置格式不正确')
    ids={str(w.id) for w in session.query(Warehouse).all()}
    weights=payload['weights']
    if set(weights)!=ids or any(type(v) is not int or not 1<=v<=10000 for v in weights.values()):
        raise ValueError('请为每个仓库填写1至10000的整数权重')
    result=dict(weights=weights,stock_priority=payload['stock_priority'])
    parameters=dict(r=current['revision']+1,p=json.dumps(result),previous=current['revision'])
    changed=session.execute(text('UPDATE warehouse_routing_policy SET revision=:r,payload=:p '
                                 'WHERE id=1 AND revision=:previous'),parameters).rowcount
    if not changed and current['revision']==0:
        changed=session.execute(text('INSERT OR IGNORE INTO warehouse_routing_policy(id,revision,payload) VALUES(1,:r,:p)'),parameters).rowcount
    if not changed:
        session.rollback()
        raise ValueError('设置已变化，请刷新后重试')
    session.commit()
    return dict(result,revision=current['revision']+1)
