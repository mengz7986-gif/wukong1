"""Render cached warehouse-scoped inventory boards for the dropship bot."""
from __future__ import annotations
import hashlib, json, os
import fcntl
import tempfile
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageOps


def catalog_inventory_rows(database, warehouse_id=None):
    """Image-only live catalog source; missing stock records mean zero, not absent.

    No FIFO, Alias, canonical grouping or stock writes are involved.
    """
    from models import Product, Warehouse, WarehouseStock
    result=[]
    with database.SessionLocal() as session:
        warehouses=session.query(Warehouse).filter(Warehouse.is_active == True)
        if warehouse_id is not None:warehouses=warehouses.filter(Warehouse.id == int(warehouse_id))
        for warehouse in warehouses.order_by(Warehouse.id).all():
            categories=session.query(Product).filter(Product.is_parent == True,
                Product.is_archived == False,Product.is_active == True,
                Product.default_warehouse_id == warehouse.id).order_by(Product.id).all()
            for category in categories:
                products=session.query(Product).filter(Product.parent_id == category.id,
                    Product.is_parent == False,Product.is_archived == False,Product.is_active == True,
                    Product.default_warehouse_id == warehouse.id).order_by(Product.created_at,Product.id).all()
                base=dict(warehouse_id=warehouse.id,warehouse_name=warehouse.name,
                    parent_id=category.id,parent_name=category.name)
                if not products:result.append(dict(base,category_only=True))
                for product in products:
                    stock=session.query(WarehouseStock.quantity).filter_by(
                        warehouse_id=warehouse.id,product_id=product.id).first()
                    result.append(dict(base,product_id=product.id,product_name=product.name,
                        quantity=int(stock[0] or 0) if stock else 0,
                        price_offset=__import__('dynamic_prices').offset(product),
                        created_at=product.created_at.isoformat() if product.created_at else ''))
    return result


def rendering_audit(snapshot, cards, include_zero):
    expected={(c.id,i.id) for c in snapshot.categories for i in c.items if include_zero or i.stock>0}
    actual={(c.id,i.id) for c in cards for i in c.items}
    missing=expected-actual
    for category in snapshot.categories:
        for item in category.items:
            if (category.id,item.id) in missing:
                logging.getLogger(__name__).error('inventory_image_filtered warehouse_id=%s category_id=%s product_id=%s name=%s reason=unexpected_render_filter',snapshot.warehouse_id,category.id,item.id,item.name)
    if missing or actual-expected or (include_zero and {c.id for c in cards}!={c.id for c in snapshot.categories}):
        raise ValueError('库存图片目录数量不一致，已停止生成，避免漏商品')
    return dict(warehouse_id=snapshot.warehouse_id,effective_categories=snapshot.category_count,
        effective_flavors=snapshot.product_count,render_categories=len(cards),
        render_flavors=sum(len(c.items) for c in cards),missing=len(missing))


def filter_visible_stock_rows(
    products: Iterable[Dict[str, object]],
    warehouses: Iterable[Dict[str, object]],
    stock_rows: Iterable[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Apply the dropship bot's existing warehouse/catalog visibility gate."""
    visible_ids = {int(row.get("id") or 0) for row in warehouses}
    product_defaults = {
        int(row.get("id") or 0): int(row.get("default_warehouse_id") or 0)
        for row in products
    }
    return [
        row for row in stock_rows
        if int(row.get("warehouse_id") or 0) in visible_ids
        and product_defaults.get(int(row.get("product_id") or 0))
        == int(row.get("warehouse_id") or 0)
    ]


@dataclass(frozen=True)
class InventoryItem:
    id: str
    name: str
    stock: int
    price_offset: int = 0


@dataclass(frozen=True)
class InventoryCategory:
    id: str
    name: str
    items: Tuple[InventoryItem, ...]


@dataclass(frozen=True)
class InventorySnapshot:
    categories: Tuple[InventoryCategory, ...]
    warehouse_id: str
    warehouse_name: str

    @property
    def category_count(self) -> int:
        return len(self.categories)

    @property
    def product_count(self) -> int:
        return sum(len(category.items) for category in self.categories)


def snapshots_from_stock_rows(
    rows: Iterable[Dict[str, object]], *, include_zero: bool = True,
) -> Tuple[InventorySnapshot, ...]:
    """Convert authorized API rows into image snapshots.

    The wholesale image includes zero-stock products so the renderer can mark
    them with its red status dot.  Text inventory filtering remains in bot.py.
    """
    warehouses: Dict[Tuple[str, str], Dict[Tuple[str, str], List[InventoryItem]]] = {}
    seen: set[Tuple[str, str, str, str]] = set()
    for row in sorted(rows, key=lambda value: (
        str(value.get("warehouse_name") or ""),
        str(value.get("warehouse_id") or ""),
        str(value.get("parent_name") or "默认型号"),
        str(value.get("parent_id") or ""),
        str(value.get("created_at") or ""),
        int(value.get("product_id") or 0),
    )):
        warehouse_id = str(row.get("warehouse_id") or "")
        warehouse_name = str(row.get("warehouse_name") or "默认仓库").strip()
        category_id = str(row.get("parent_id") or "")
        category_name = str(row.get("parent_name") or "默认型号").strip()
        product_id = str(row.get("product_id") or "")
        product_name = str(row.get("product_name") or "").strip()
        try:
            stock = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            stock = 0
        if row.get('category_only') and warehouse_id:
            warehouses.setdefault((warehouse_id,warehouse_name),{}).setdefault((category_id,category_name),[])
            continue
        if not warehouse_id or not product_name or (stock <= 0 and not include_zero):
            continue
        identity = (warehouse_id, category_name, product_id, product_name)
        if identity in seen:
            continue
        seen.add(identity)
        categories = warehouses.setdefault((warehouse_id, warehouse_name), {})
        categories.setdefault((category_id, category_name), []).append(
            InventoryItem(product_id, product_name, stock, int(row.get('price_offset') or 0))
        )

    snapshots = []
    for (warehouse_id, warehouse_name), categories in sorted(
        warehouses.items(), key=lambda entry: (entry[0][1], entry[0][0])
    ):
        category_rows = tuple(
            # The wholesale snapshot keeps the database/API product order
            # (created_at, then id); do not replace it with a name sort here.
            InventoryCategory(category_id, name, tuple(items))
            for (category_id, name), items in categories.items()
        )
        snapshots.append(InventorySnapshot(category_rows, warehouse_id, warehouse_name))
    return tuple(snapshots)

DEFAULT_INVENTORY_IMAGE_SETTINGS = {
    "inventory_image_enabled": "1", "inventory_image_brand_title": "彩虹仓",
    "inventory_image_brand_subtitle": "RAINBOW WAREHOUSE", "inventory_image_title": "实时库存",
    "inventory_image_menu_note": "", "inventory_image_description": "",
    "inventory_image_footer_note": "", "inventory_image_show_quantity": "0",
    "inventory_image_jpeg_quality": "94",
    "inventory_image_show_zero": "1", "inventory_image_red_max": "0",
    "inventory_image_green_min": "100",
}
RENDERER_VERSION = 25
SETTINGS_PATH = Path(__file__).resolve().parent / 'data' / 'inventory_image_settings.json'
FONT_CANDIDATES = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/Supplemental/Songti.ttc")

def _warehouse_settings_path(warehouse_id):
    if warehouse_id is None:
        return SETTINGS_PATH
    if isinstance(warehouse_id, bool) or not str(warehouse_id).isdigit() or int(warehouse_id) <= 0:
        raise ValueError('请选择有效仓库')
    return SETTINGS_PATH.with_name(f'inventory_image_warehouse_{int(warehouse_id)}.json')


def inventory_image_settings(values: Dict[str, str] | None = None, *, warehouse_id=None) -> Dict[str, str]:
    if values is None:
        path = _warehouse_settings_path(warehouse_id)
        # Preserve existing global settings only as the initial compatibility
        # default. A warehouse save stores its complete independent configuration.
        if not path.exists() and warehouse_id is not None:
            path = SETTINGS_PATH
        if path.exists():
            values = json.loads(path.read_text(encoding='utf-8'))
    result = dict(DEFAULT_INVENTORY_IMAGE_SETTINGS)
    result.update({k: str(v) for k, v in (values or {}).items() if k in result})
    # Normalize only the obsolete identity, not arbitrary customer settings.
    if result['inventory_image_brand_title'] == '波派代发':
        result['inventory_image_brand_title'] = '彩虹仓'
    if result['inventory_image_brand_subtitle'] == 'POPEYE DROPSHIPPING':
        result['inventory_image_brand_subtitle'] = 'RAINBOW WAREHOUSE'
    return result

def _font_path() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).is_file(): return path
    raise RuntimeError("未找到支持中文的系统字体")

def _font(path: str, size: int): return ImageFont.truetype(path, size=size)
def _width(draw, value: str, font) -> int:
    box = draw.textbbox((0, 0), value or " ", font=font); return box[2] - box[0]

def _wrap(draw, value: str, font, max_width: int) -> List[str]:
    lines = []
    for paragraph in str(value or "").splitlines():
        if not paragraph:
            lines.append('')
            continue
        current = ""
        for char in paragraph:
            if current and _width(draw, current + char, font) > max_width:
                lines.append(current.rstrip()); current = char.lstrip()
            else: current += char
        if current: lines.append(current.rstrip())
    return lines

def _card_height(draw, category: InventoryCategory, width: int, category_font, item_font) -> int:
    title_rows = max(1, len(_wrap(draw, category.name, category_font, width - 48)))
    name_width = width - 48 - 40
    item_rows = sum(max(1, len(_wrap(draw, item.name, item_font, name_width))) for item in category.items)
    return 28 + title_rows * 50 + 14 + item_rows * 45 + 26

def _visual_card_layout(draw, category, width, title_font, item_font):
    """Measured column flow; every item remains whole and in catalog order."""
    inner_columns = 1
    cell_width = (width - 56) // inner_columns
    rows = [max(1, len(_wrap(draw, item.name, item_font, cell_width - 54))) * 48
            for item in category.items]
    # Find the contiguous split that balances measured text, not item counts.
    split = len(rows)
    if inner_columns == 2:
        split = min(range(1, len(rows)), key=lambda n: max(sum(rows[:n]), sum(rows[n:])))
    groups = [category.items[:split]]
    if inner_columns == 2: groups.append(category.items[split:])
    title_height = max(1, len(_wrap(draw, category.name, title_font, width - 56))) * LAYOUT_STYLE['category_line']
    body_height = max([sum(rows[:split]), sum(rows[split:])] or [0])
    return 64 + title_height + body_height + (110 if any(i.price_offset for i in category.items) else 0), cell_width, groups, title_height

def _background_name(warehouse_id):
    if str(warehouse_id)=='1':return 'inventory-long-march-snow-v1.png'
    if str(warehouse_id)=='2':return 'inventory-long-march-grassland-v1.png'
    if str(warehouse_id)=='3':return 'inventory-founding-ceremony-v1.png'
    return 'inventory-waterlight-v1.png'


def _cache_key(snapshot,settings):
    asset=Path(__file__).parent/'static/images'/_background_name(snapshot.warehouse_id)
    payload=dict(renderer_version=RENDERER_VERSION,snapshot=asdict(snapshot),
        settings=inventory_image_settings(settings),layout=LAYOUT_STYLE,
        background_asset=asset.name,background_sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def stock_marker_color(stock: int, settings=None) -> str:
    """Return the exact marker color used by the wholesale renderer."""
    values=inventory_image_settings(settings)
    return "#dc2626" if stock <= int(values['inventory_image_red_max']) else "#eab308" if stock < int(values['inventory_image_green_min']) else "#16a34a"

def _stock_sorted_categories(categories):
    """Image presentation only: stable descending stock, preserving equal-stock order."""
    return [InventoryCategory(c.id,c.name,tuple(sorted(c.items,key=lambda item:-item.stock)))
            for c in categories]

# Fixed JPEG canvas; background contribution is the original warehouse2 value.
LAYOUT_STYLE = dict(width=3200,height=3200,margin=48,gap=24,header=300,
    footer=640,category_font=58,item_font=36,category_line=78,item_line=48,
    max_columns=6,min_card_width=480,card_background_mix=0.14,footer_background_mix=0.10)


def _public_note(value):
    import re
    return '\n'.join(line for line in str(value).splitlines() if not re.search(
        r'客服|联系我|联系方式|电话|二维码|https?://|t\.me/|@[A-Za-z0-9_]+|(?<!\d)1[3-9]\d{9}(?!\d)',line,re.I))


def inventory_page_plan(snapshot,settings):
    """Measured, bounded-font pagination; no source snapshot or settings writes."""
    settings=inventory_image_settings(settings);style=LAYOUT_STYLE
    width,height,margin,gap=style['width'],style['height'],style['margin'],style['gap']
    probe=ImageDraw.Draw(Image.new('RGB',(1,1)));font_path=_font_path()
    category_font,item_font=_font(font_path,style['category_font']),_font(font_path,style['item_font'])
    note_font,footer_font=_font(font_path,36),_font(font_path,54)
    menu=_wrap(probe,_public_note(settings['inventory_image_menu_note']).strip(),note_font,width-2*margin)
    reminder=_public_note(settings['inventory_image_footer_note']).strip()
    if len(menu)>1:reminder='\n'.join(menu[1:])+('\n'+reminder if reminder else '')
    module_width=(width-2*margin-gap)//2
    modules=[('库存状态说明',_public_note(settings['inventory_image_description']).strip(),True),('温馨提示',reminder,False)]
    module_lines=[_wrap(probe,text,footer_font,module_width-100) for _,text,_ in modules]
    note_pages=max(1,(len(module_lines[0])+3)//4,(len(module_lines[1])+5)//6)
    cards=_stock_sorted_categories(snapshot.categories)
    if settings['inventory_image_show_zero']=='0':
        cards=[InventoryCategory(c.id,c.name,tuple(i for i in c.items if i.stock>0)) for c in cards]
        cards=[c for c in cards if c.items]
    rendering_audit(snapshot,cards,settings['inventory_image_show_zero']=='1')
    footer_y=height-margin-style['footer'];body_end=footer_y-gap;available=body_end-style['header']
    choices=[]
    for columns in range(1,style['max_columns']+1):
        card_width=(width-2*margin-gap*(columns-1))//columns
        if card_width<style['min_card_width']:continue
        groups=[];valid=True
        for category in cards:
            ordered=tuple(i for i in category.items if not i.price_offset)+tuple(i for i in category.items if i.price_offset)
            item_heights=[max(1,len(_wrap(probe,i.name,item_font,card_width-110)))*style['item_line'] for i in ordered]
            fragments=[];start=0;part=0
            while start<len(ordered) or (not ordered and part==0):
                display_name=category.name+('（续）' if part else '')
                base=64+max(1,len(_wrap(probe,display_name,category_font,card_width-56)))*style['category_line']
                end=start;rows_height=0;has_special=False
                while end<len(ordered):
                    next_special=has_special or bool(ordered[end].price_offset)
                    next_height=base+rows_height+item_heights[end]+(110 if next_special else 0)
                    if next_height>available:break
                    rows_height+=item_heights[end];has_special=next_special;end+=1
                if base>available or (start<len(ordered) and end==start):valid=False;break
                fragment=InventoryCategory(category.id,display_name,ordered[start:end])
                fragments.append((base+rows_height+(110 if has_special else 0),fragment))
                part+=1;start=end
                if not ordered:break
            if not valid:break
            groups.append(fragments)
        if not valid:continue
        pages=[[]];ys=[[style['header']]*columns]
        for fragments in sorted(groups,key=lambda g:(-max(x[0] for x in g),g[0][1].name)):
            min_page=0
            for card_height,category in fragments:
                found=None
                for page in range(min_page,len(pages)):
                    fits=[c for c in range(columns) if ys[page][c]+card_height<=body_end]
                    if fits:found=(page,min(fits,key=lambda c:(ys[page][c],c)));break
                if found is None:pages.append([]);ys.append([style['header']]*columns);found=(len(pages)-1,0)
                page,slot=found;min_page=page
                pages[page].append((category,slot,ys[page][slot],card_height));ys[page][slot]+=card_height+gap
        while len(pages)<note_pages:pages.append([])
        choices.append((len(pages),columns,card_width,pages))
    if not choices:raise ValueError('商品名称过长，无法在固定画布内完整排版，请检查目录名称')
    _,columns,card_width,pages=min(choices,key=lambda x:(x[0],x[1]))
    actual=[(c.id,i.id) for page in pages for c,_,_,_ in page for i in c.items]
    expected=[(c.id,i.id) for c in cards for i in c.items]
    if len(actual)!=len(set(actual)) or set(actual)!=set(expected):raise ValueError('分页商品归属或数量不一致')
    if {c.id for page in pages for c,_,_,_ in page}!={c.id for c in cards}:raise ValueError('分页分类不完整')
    return dict(width=width,height=height,columns=columns,card_width=card_width,pages=pages,
        footer_y=footer_y,module_width=module_width,module_lines=module_lines,modules=modules,
        menu_lines=menu[:1],settings=settings,font_path=font_path,body_end=body_end)


def _render(snapshot: InventorySnapshot, settings: Dict[str,str], output: Path, *, page_index=0,plan=None):
    plan=plan or inventory_page_plan(snapshot,settings);settings=plan['settings'];font_path=plan['font_path']
    width,height,margin,gap,header=LAYOUT_STYLE['width'],LAYOUT_STYLE['height'],LAYOUT_STYLE['margin'],LAYOUT_STYLE['gap'],LAYOUT_STYLE['header']
    columns,card_width,placements=plan['columns'],plan['card_width'],plan['pages'][page_index]
    category_font,item_font=_font(font_path,LAYOUT_STYLE['category_font']),_font(font_path,LAYOUT_STYLE['item_font'])
    note_font,footer_font=_font(font_path,36),_font(font_path,54)
    menu_lines=plan['menu_lines'];modules=plan['modules'];module_width=plan['module_width'];footer_height=LAYOUT_STYLE['footer']+gap
    ink_theme = True
    mountain_theme = False
    quiet_theme = ink_theme or mountain_theme
    heading_color = '#242424' if quiet_theme else '#193354'
    background_name = _background_name(snapshot.warehouse_id)
    background_path=Path(__file__).parent/'static/images'/background_name
    with Image.open(background_path) as background:
        image=ImageOps.fit(background.convert('RGB'),(width,height),method=Image.Resampling.LANCZOS)
    if ink_theme:
        # Quiet the ink behind the headings without removing the landscape.
        heading_backdrop=image.crop((0,0,width,header-20))
        image.paste(Image.blend(heading_backdrop,Image.new('RGB',heading_backdrop.size,'white'),0.60),(0,0))
    draw = ImageDraw.Draw(image)
    brand_font, sub_font, center_font = _font(font_path, 96), _font(font_path, 38), _font(font_path, 104)
    brand, subtitle = settings["inventory_image_brand_title"].strip(), settings["inventory_image_brand_subtitle"].strip()
    if subtitle: draw.text((margin, 158), subtitle, font=sub_font, fill=heading_color)
    center = snapshot.warehouse_name
    draw.text((margin,32),center,font=brand_font,fill='#242424' if quiet_theme else '#008b79',stroke_width=2)
    title=settings['inventory_image_title'].strip() or '实时库存'
    draw.text((width-margin-_width(draw,title,center_font),32),title,font=center_font,fill='#242424' if quiet_theme else '#10284d',stroke_width=2)
    english_title='REAL-TIME INVENTORY'
    draw.text((width-margin-_width(draw,english_title,sub_font),164),english_title,font=sub_font,fill='#666666' if quiet_theme else '#53657e')
    for index,line in enumerate(menu_lines):
        draw.text((margin,208+index*48),line,font=note_font,fill='#666666' if quiet_theme else '#53657e')
    draw.line((margin,header-20,width-margin,header-20),fill='#cccccc' if quiet_theme else '#cee2e9',width=2)

    for card_index,(category, slot, y, card_height) in enumerate(placements):
        x = margin + slot * (card_width + gap)
        if not quiet_theme:
            draw.rounded_rectangle((x+5,y+7,x+card_width+5,y+card_height+7),28,fill='#dedede' if ink_theme else '#dce5f1')
        palettes=[((207,230,252),(249,253,255),'#c4e8f2'),((231,216,251),(253,251,255),'#e0d5f4'),((251,224,239),(255,252,254),'#f0d9e9')]
        if ink_theme:
            palettes=[((230,230,230),(253,253,253),'#bdbdbd'),((239,239,239),(255,255,255),'#c8c8c8')]
        elif mountain_theme:
            palettes=[((237,234,227),(251,250,247),'#d4d0c7')]
        top,bottom,border=palettes[card_index%len(palettes)]
        panel=Image.new('RGB',(card_width,card_height),'white');pd=ImageDraw.Draw(panel)
        for py in range(card_height):
            blend=min(1,py/max(1,min(card_height,330)))
            pd.line((0,py,card_width,py),fill=tuple(int(a+(b-a)*blend) for a,b in zip(top,bottom)))
        if quiet_theme:
            # Readability first: only a subdued trace of scenery under text;
            # show the full-strength artwork in the surrounding open areas.
            panel=Image.blend(panel,image.crop((x,y,x+card_width,y+card_height)),LAYOUT_STYLE['card_background_mix'])
        mask=Image.new('L',(card_width,card_height),0);ImageDraw.Draw(mask).rounded_rectangle((0,0,card_width-1,card_height-1),28,fill=255)
        image.paste(panel,(x,y),mask)
        draw.rounded_rectangle((x,y,x+card_width,y+card_height),28,outline=border,width=2 if quiet_theme else 3)
        cursor = y + 22
        for line in _wrap(draw, category.name, category_font, card_width - 56) or [category.name]:
            draw.text((x + 28, cursor), line, font=category_font, fill='#242424' if quiet_theme else '#172c50',stroke_width=1); cursor += LAYOUT_STYLE['category_line']
        draw.line((x + 24, cursor + 2, x + card_width - 24, cursor + 2), fill='#cccccc' if quiet_theme else '#cbd5e1', width=2); cursor += 18
        _,cell_width,groups,_ = _visual_card_layout(draw,category,card_width,category_font,item_font)
        for col,group in enumerate(groups):
            item_y=cursor
            item_x=x+28+col*cell_width
            ordinary=[i for i in group if not i.price_offset]
            special=[i for i in group if i.price_offset]
            if special:
                special_height=sum(max(1,len(_wrap(draw,i.name,item_font,cell_width-54)))*48 for i in special)+100
                box_y=item_y+sum(max(1,len(_wrap(draw,i.name,item_font,cell_width-54)))*48 for i in ordinary)+14
                draw.rounded_rectangle((item_x-10,box_y,item_x+cell_width-2,box_y+special_height),18,fill='#f0f0f0' if ink_theme else '#fff4df',outline='#555555' if ink_theme else '#d99a35',width=3)
                draw.text((item_x+8,box_y+10),'以下口味价格 +¥2',font=item_font,fill='#242424' if ink_theme else '#965d10',stroke_width=1)
            for index,item in enumerate(ordinary+special):
                if special and index==len(ordinary): item_y=box_y+72
                color = stock_marker_color(item.stock, settings)
                lines = _wrap(draw, item.name, item_font, cell_width-54) or [""]
                draw.ellipse((item_x,item_y+6,item_x+36,item_y+42),fill=color)
                for i,line in enumerate(lines): draw.text((item_x+48,item_y+i*48),line,font=item_font,fill='#181818' if ink_theme else '#292929' if mountain_theme else '#1e293b')
                item_y+=len(lines)*48

    footer_y = plan['footer_y']
    for module_index,(title,text,markers) in enumerate(modules):
        module_x=margin+module_index*(module_width+gap)
        capacity=4 if markers else 6
        lines=plan['module_lines'][module_index][page_index*capacity:(page_index+1)*capacity]
        module_height=footer_height-gap
        if ink_theme:
            surface=Image.blend(Image.new('RGB',(module_width,module_height),'#fafafa'),image.crop((module_x,footer_y,module_x+module_width,footer_y+module_height)),LAYOUT_STYLE['footer_background_mix'])
            footer_mask=Image.new('L',(module_width,module_height),0)
            ImageDraw.Draw(footer_mask).rounded_rectangle((0,0,module_width-1,module_height-1),28,fill=255)
            image.paste(surface,(module_x,footer_y),footer_mask)
            draw.rounded_rectangle((module_x,footer_y,module_x+module_width,footer_y+module_height),28,outline='#c8c8c8',width=2)
        else:
            draw.rounded_rectangle((module_x,footer_y,module_x+module_width,footer_y+module_height),28,fill='#f8f7f3' if mountain_theme else '#f8fcff',outline='#d4d0c7' if mountain_theme else '#cfe5f0',width=2)
        draw.text((module_x+28,footer_y+20),title,font=category_font,fill=heading_color,stroke_width=1)
        cursor=footer_y+116
        if markers:
            legend=[('库存充足','可正常下单','#16a34a'),('库存预警','请准备备选','#eab308'),('缺货','暂时无法下单','#dc2626')]
            cell=(module_width-56)//3
            small_font=_font(font_path,36)
            for li,(label,help_text,color) in enumerate(legend):
                lx=module_x+28+li*cell
                draw.rounded_rectangle((lx,cursor,lx+cell-12,cursor+164),22,fill='#eceae4' if mountain_theme else '#eeeeee' if ink_theme else '#eef5fa')
                draw.ellipse((lx+16,cursor+14,lx+70,cursor+68),fill=color)
                draw.text((lx+84,cursor+12),label,font=footer_font,fill=heading_color,stroke_width=1)
                draw.text((lx+22,cursor+98),help_text,font=small_font,fill='#666666' if quiet_theme else '#53657e')
            cursor+=190
        for index,line in enumerate(lines):
            draw.text((module_x+28,cursor),('• ' if not markers else '')+line,font=footer_font,fill='#444444' if ink_theme else '#334155',stroke_width=1)
            cursor+=74
    page_label=f"第 {page_index+1} / {len(plan['pages'])} 页"
    draw.text((width-margin-_width(draw,page_label,_font(font_path,24)),height-38),page_label,font=_font(font_path,24),fill='#444444')
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "JPEG", quality=int(settings['inventory_image_jpeg_quality']), subsampling=0, optimize=True, progressive=True)


def render_inventory_image(snapshot,settings,cache_dir):
    paths,hit=render_inventory_image_pages(snapshot,settings,cache_dir)
    return paths[0],hit


def render_inventory_image_pages(snapshot,settings,cache_dir):
    cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True)
    with (cache_dir/'.inventory-preview.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _render_pages_cached(snapshot,settings,cache_dir)


def render_inventory_image_pages_bytes(snapshot,settings,cache_dir):
    """All JPEG pages are copied under the existing lock, before invalidation."""
    cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True)
    with (cache_dir/'.inventory-preview.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        paths,hit=_render_pages_cached(snapshot,settings,cache_dir)
        return tuple(p.read_bytes() for p in paths),hit


def render_inventory_image_page_bytes(snapshot,settings,cache_dir,page=1):
    if type(page) is not int or page<1:raise ValueError('请选择有效的库存图片页码')
    cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True)
    with (cache_dir/'.inventory-preview.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        paths,hit=_render_pages_cached(snapshot,settings,cache_dir)
        if type(page) is not int or not 1<=page<=len(paths):raise ValueError('请选择有效的库存图片页码')
        return paths[page-1].read_bytes(),hit,len(paths)


def render_inventory_image_bytes(snapshot,settings,cache_dir):
    pages,hit=render_inventory_image_pages_bytes(snapshot,settings,cache_dir)
    return pages[0],hit


def _render_pages_cached(snapshot,settings,cache_dir):
    import re
    cache_dir=Path(cache_dir);key=_cache_key(snapshot,settings)
    suffix=hashlib.sha256(snapshot.warehouse_id.encode()).hexdigest()[:12]
    manifest=cache_dir/f'inventory-preview-{suffix}.pages.json'
    old={}
    try:
        old=json.loads(manifest.read_text())
        if not isinstance(old,dict):old={}
        names=old['pages']
        valid=isinstance(names,list) and 0<len(names)<=1000 and names==[f'inventory-preview-{suffix}-{key[:16]}-p{i+1}.jpg' for i in range(len(names))] and old.get('width')==LAYOUT_STYLE['width'] and old.get('height')==LAYOUT_STYLE['height']
        if valid and old.get('key')==key and all((cache_dir/n).is_file() and not (cache_dir/n).is_symlink() for n in names):return tuple(cache_dir/n for n in names),True
    except (OSError,ValueError,KeyError,TypeError):pass
    plan=inventory_page_plan(snapshot,settings);paths=[];temporary=[];new_files=[]
    # The existing file lock and generation manifest give one atomic publication.
    # Failed renders leave the prior complete generation visible, never mixed pages.
    try:
        for index in range(len(plan['pages'])):
            final=cache_dir/f'inventory-preview-{suffix}-{key[:16]}-p{index+1}.jpg'
            fd,tmp=tempfile.mkstemp(dir=cache_dir,prefix='.inventory-page-',suffix='.jpg');os.close(fd);tmp=Path(tmp);temporary.append(tmp)
            _render(snapshot,settings,tmp,page_index=index,plan=plan);paths.append(final)
        for tmp,final in zip(temporary,paths):os.replace(tmp,final);new_files.append(final)
        fd,tmp=tempfile.mkstemp(dir=cache_dir,prefix='.inventory-manifest-');temporary.append(Path(tmp))
        with os.fdopen(fd,'w') as stream:
            json.dump(dict(key=key,pages=[p.name for p in paths],width=plan['width'],height=plan['height']),stream);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,manifest)
    except BaseException:
        # Only unpublished files from this new generation are removed.
        for p in new_files:
            if p.name not in old.get('pages',[]):p.unlink(missing_ok=True)
        raise
    finally:
        for p in temporary:p.unlink(missing_ok=True)
    keep={p.name for p in paths}|{manifest.name}
    for p in cache_dir.glob(f'inventory-preview-{suffix}*'):
        if p.name not in keep and _inventory_cache_name(p.name,suffix) and p.is_file() and not p.is_symlink():
            try:p.unlink()
            except OSError:pass  # Current manifest remains authoritative.
    return tuple(paths),False


def _render_cached(snapshot,settings,cache_dir):
    paths,hit=_render_pages_cached(snapshot,settings,cache_dir)
    return paths[0],hit


def _inventory_cache_name(name,suffix=None):
    import re
    prefix=suffix if suffix else r'[a-f0-9]{12}'
    return bool(re.fullmatch(r'inventory-preview-'+prefix+r'(?:\.jpg|\.sha256|\.pages\.json|-[a-f0-9]{16}-p[1-9][0-9]*\.jpg)',name))


def preview_inventory_settings(values, *, warehouse_id=None):
    """Validate an ephemeral preview using the same constraints as saved settings."""
    if not isinstance(values, dict) or set(values) - set(DEFAULT_INVENTORY_IMAGE_SETTINGS):
        raise ValueError('库存图片设置字段无效')
    for key,value in values.items():
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError('设置必须是最多500字符的文本')
    if (values.get('inventory_image_show_quantity','0') not in ('0','1','yellow')
            or values.get('inventory_image_show_zero','1') not in ('0','1')):
        raise ValueError('库存数字或零库存显示开关无效')
    if values.get('inventory_image_enabled', '1') not in ('0','1'):
        raise ValueError('图片开关无效')
    quality = values.get('inventory_image_jpeg_quality', '94')
    if not quality.isdigit() or not 80 <= int(quality) <= 100:
        raise ValueError('JPEG质量须为80–100')
    merged = inventory_image_settings(warehouse_id=warehouse_id)
    merged.update(values)
    red,green=merged['inventory_image_red_max'],merged['inventory_image_green_min']
    if not red.isdigit() or not green.isdigit() or not 0<=int(red)<int(green)<=1000000:
        raise ValueError('状态阈值必须是非负整数，红色库存上限必须小于绿色库存起点')
    return merged


def save_inventory_settings(values, *, warehouse_id=None):
    destination = _warehouse_settings_path(warehouse_id)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with (SETTINGS_PATH.parent / '.inventory-image-settings.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = preview_inventory_settings(values, warehouse_id=warehouse_id)
        fd, temporary = tempfile.mkstemp(dir=SETTINGS_PATH.parent, prefix='.inventory-settings-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(merged, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return merged


def clear_inventory_cache(cache_dir):
    import re
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return 0
    with (cache_dir / '.inventory-preview.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        files = [p for p in cache_dir.iterdir() if p.is_file() and not p.is_symlink()
                 and _inventory_cache_name(p.name)]
        for path in files:
            path.unlink()
    return len(files)


def invalidate_warehouse_cache(warehouse_id, cache_dir=None):
    """After stock commit, invalidate only this warehouse's two image files.

    Failure cannot undo a committed stock transaction or trigger business retry:
    the full live-snapshot content hash remains the final stale-image guard.
    """
    cache_dir=Path(cache_dir or os.getenv('RAINBOW_INVENTORY_CACHE',str(Path(__file__).resolve().parent/'cache')))
    suffix=hashlib.sha256(str(warehouse_id).encode()).hexdigest()[:12]
    try:
        if not cache_dir.exists():return 0
        with (cache_dir/'.inventory-preview.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            removed=0
            for path in cache_dir.glob('inventory-preview-'+suffix+'*'):
                if _inventory_cache_name(path.name,suffix) and path.is_file() and not path.is_symlink():
                    path.unlink();removed+=1
            return removed
    except OSError:
        logging.getLogger(__name__).warning('inventory cache invalidation failed; live content hash remains authoritative',exc_info=True)
        return 0
