"""Warehouse/category shipping configuration attached to the existing settings UI."""
import secrets
from fastapi import APIRouter, HTTPException, Request
from models import Product, Warehouse
import warehouse_shipping
from catalog_retirement import operating_query, require_operating


def make_router(database, admin_username, security_ready):
    router = APIRouter()

    def guard(request, write=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401, '请先登录')
        if request.session.get('admin_username') != admin_username():
            raise HTTPException(403, '仅管理员可管理运费')
        if write:
            token = request.session.get('shipping_csrf', '')
            if not token or not secrets.compare_digest(token, request.headers.get('X-Shipping-CSRF', '')):
                raise HTTPException(403, '请求校验失败')
            if not security_ready():
                raise HTTPException(503, '请先配置安全的会话签名密钥')

    @router.get('/api/settings/warehouse-shipping')
    def status(request: Request):
        guard(request)
        request.session.setdefault('shipping_csrf', secrets.token_urlsafe(32))
        with database.SessionLocal() as session:
            return dict(csrf=request.session['shipping_csrf'], rules=warehouse_shipping.read()['rules'],
                warehouses=[dict(id=w.id, name=w.name, is_active=w.is_active)
                            for w in operating_query(session,Warehouse,'warehouse').order_by(Warehouse.id)],
                categories=[dict(id=p.id, name=p.name, warehouse_id=p.default_warehouse_id)
                            for p in operating_query(session,Product,'product').filter_by(is_parent=True, is_archived=False)],
                fallback='未单独配置的分类沿用现有默认运费规则；停用仓库不接新订单')

    @router.post('/api/settings/warehouse-shipping')
    async def save(request: Request):
        guard(request, True)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('请求格式无效')
            warehouse, category = int(body['warehouse_id']), int(body['category_id'])
            with database.SessionLocal() as session:
                require_operating(session,'warehouse',warehouse)
                require_operating(session,'product',category)
                w, p = session.get(Warehouse, warehouse), session.get(Product, category)
                if not w or not p or not p.is_parent or p.is_archived or p.default_warehouse_id != warehouse:
                    raise ValueError('分类不属于所选仓库')
            value = warehouse_shipping.save(warehouse, category, body.get('fee', 0), body.get('units', 1),
                request.session['admin_username'], remove=body.get('action') == 'reset')
            return dict(rule=value)
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(400, str(error)) from None

    return router
