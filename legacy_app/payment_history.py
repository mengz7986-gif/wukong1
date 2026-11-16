"""Read-only, independently paginated payment history."""
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from models import Payment, User


def history(session, *, user_id=None, search='', limit=30, offset=0):
    search=str(search or '').strip()
    if len(search)>100 or not 1<=limit<=100 or offset<0:
        raise ValueError('搜索最多100字，每页1至100条，页码不能为负')
    query=session.query(Payment)
    if user_id is not None:
        query=query.filter(Payment.user_id==user_id)
    if search:
        username=search.lstrip('@').casefold()
        filters=[User.display_name.contains(search,autoescape=True),
                 User.telegram_name.contains(search,autoescape=True)]
        if username:
            filters.append(func.lower(User.username).contains(username,autoescape=True))
        if search.isdigit():filters.append(User.telegram_id==search)
        users=session.query(User.id).filter(or_(*filters))
        query=query.filter(Payment.user_id.in_(users))
    total=query.count()
    rows=query.options(joinedload(Payment.user)).order_by(Payment.created_at.desc(),Payment.id.desc()).offset(offset).limit(limit).all()
    records=[dict(id=p.id,user_id=p.user_id,user_telegram_id=p.user.telegram_id if p.user else None,
        username=p.user.username if p.user else None,
        display_name=p.user.display_name if p.user else None,
        telegram_name=p.user.telegram_name if p.user else None,
        order_id=p.order_id,amount=p.amount,method=p.method,original_amount=p.original_amount,
        exchange_rate=p.exchange_rate,exchange_currency=p.exchange_currency,note=p.note,
        operator=p.operator,created_at=p.created_at) for p in rows]
    return dict(items=records,total=total,limit=limit,offset=offset)
