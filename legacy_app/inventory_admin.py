"""Inventory image management; previews never contact Telegram or mutate stock."""
import asyncio
import secrets
import hashlib
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import inventory_preview as images

ROOT = Path(__file__).resolve().parent


def make_router(templates, stock_rows, security_ready, admin_username=None, warehouse_provider=None):
    router = APIRouter()
    cache = ROOT / 'cache'

    def checked(value):
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            raise HTTPException(400, '请选择有效仓库')
        warehouse = int(value)
        if warehouse_provider and not any(w['id'] == warehouse for w in warehouse_provider()):
            raise HTTPException(404, '仓库不存在')
        return warehouse

    def guard(request, write=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401, '请先登录')
        if admin_username and request.session.get('admin_username') != admin_username():
            raise HTTPException(403, '仅管理员可管理库存图片')
        if request.method != 'GET':
            expected = request.session.get('inventory_csrf', '')
            if not expected or not secrets.compare_digest(expected, request.headers.get('X-Inventory-CSRF','')):
                raise HTTPException(403, '请求校验失败')
        if write and not security_ready():
            raise HTTPException(503, '当前安全门禁不允许修改设置或清除缓存')

    @router.get('/admin/settings/inventory-image')
    @router.get('/admin/warehouses/{warehouse_id}/inventory-image')
    async def page(request: Request, warehouse_id: int = None):
        guard(request)
        if warehouse_id is not None:
            checked(warehouse_id)
        request.session.setdefault('inventory_csrf', secrets.token_urlsafe(32))
        return templates.TemplateResponse(request, 'admin/inventory_image.html', {
            'inventory_csrf':request.session['inventory_csrf'],
            'can_manage':security_ready(),
            'image_warehouse_id':warehouse_id,
        }, headers={'Cache-Control':'no-store'})

    @router.get('/api/settings/inventory-image')
    @router.get('/api/warehouses/{warehouse_id}/inventory-image')
    def status(request: Request, warehouse_id: int = None):
        guard(request)
        if warehouse_id is not None:
            checked(warehouse_id)
        files = list(cache.glob('inventory-preview-*.jpg')) if cache.exists() else []
        if warehouse_id is not None:
            suffix=hashlib.sha256(str(warehouse_id).encode()).hexdigest()[:12]
            files=[f for f in files if images._inventory_cache_name(f.name,suffix)]
        recent = max((f.stat().st_mtime for f in files), default=None)
        settings=images.inventory_image_settings(warehouse_id=warehouse_id)
        return JSONResponse(dict(settings=settings,
            renderer_version=images.RENDERER_VERSION, cache_count=len(files),
            last_generated_at=recent, format='JPEG', progressive=True,
            subsampling=0, include_zero=settings['inventory_image_show_zero']=='1',
            show_quantity=False, quantity_mode='0',
            canvas_width=images.LAYOUT_STYLE['width'],canvas_height=images.LAYOUT_STYLE['height'],
            red_max=int(settings['inventory_image_red_max']),yellow_max=int(settings['inventory_image_green_min'])-1,
            green_min=int(settings['inventory_image_green_min'])), headers={'Cache-Control':'no-store'})

    @router.post('/api/settings/inventory-image')
    @router.post('/api/warehouses/{warehouse_id}/inventory-image')
    async def save(request: Request, warehouse_id: int = None):
        guard(request, True)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '设置格式无效')
        warehouse = checked(warehouse_id if warehouse_id is not None else body.pop('warehouse_id', None))
        body.pop('warehouse_id', None)
        try:
            settings = images.save_inventory_settings(body, warehouse_id=warehouse)
            images.invalidate_warehouse_cache(warehouse,cache)
            return settings
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post('/api/settings/inventory-image/preview')
    @router.post('/api/warehouses/{warehouse_id}/inventory-image/preview')
    async def preview(request: Request, warehouse_id: int = None):
        guard(request)
        body = await request.json()
        if not isinstance(body,dict):
            raise HTTPException(400,'请求格式无效')
        warehouse=checked(warehouse_id if warehouse_id is not None else body.get('warehouse_id'))
        rows = await stock_rows(warehouse)
        snapshots = images.snapshots_from_stock_rows(rows, include_zero=True)
        snapshot = next((s for s in snapshots if s.warehouse_id == str(warehouse)), None)
        if snapshot is None:
            raise HTTPException(404, '仓库不存在、已停用或没有可预览商品')
        try:
            settings=images.preview_inventory_settings(body.get('settings',{}),warehouse_id=warehouse)
            page=body.get('page',1)
            content,hit,page_count=await asyncio.to_thread(images.render_inventory_image_page_bytes,
                snapshot,settings,cache,page)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        except Exception:
            raise HTTPException(503,'库存图片生成失败，请稍后重试') from None
        return Response(content, media_type='image/jpeg', headers={
            'Cache-Control':'no-store', 'X-Inventory-Cache':'hit' if hit else 'miss',
            'X-Inventory-Page':str(page),'X-Inventory-Pages':str(page_count),
        })

    @router.post('/api/settings/inventory-image/clear-cache')
    @router.post('/api/warehouses/{warehouse_id}/inventory-image/clear-cache')
    async def clear(request: Request, warehouse_id: int = None):
        guard(request, True)
        body = await request.json()
        if not isinstance(body, dict) or body.get('confirmation') != '清除库存图片缓存':
            raise HTTPException(400, '请再次确认清除库存图片缓存')
        warehouse=checked(warehouse_id if warehouse_id is not None else body.get('warehouse_id'))
        removed = await asyncio.to_thread(images.invalidate_warehouse_cache, warehouse, cache)
        return dict(removed_files=removed, stock_modified=False)

    return router
