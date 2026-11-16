import re
import unicodedata
from typing import Dict, List, Optional, Tuple
from config import PRODUCTS
from order_structure import structure_order_text, quantity_text
from contact_fields import parse_contact, merge_contact, diagnose_contact, contact_prompt, valid_name, name_has_evidence
from parser_rules import (
    PARSER_RULESET_VERSION,
    analyze_quantity_scopes,
    normalize_order_text,
)

try:
    from address_parser import parse_record as _parse_address_record
except Exception:
    _parse_address_record = None

try:
    from address_utils import normalize_cn_address as _normalize_cn_address
except Exception:
    _normalize_cn_address = None


def _parse_memo_key(message, products=(), knowledge_principal=None):
    # Only exact source/catalog/rule snapshots can reuse extraction. No
    # cross-message cache and no authorization/stock/price decision is cached.
    if not isinstance(message, str) or len(message) > 16000 or not isinstance(products, (list, tuple)) or not products or len(products) > 2000:
        return None
    try:
        import hashlib,json
        if knowledge_principal is None:
            from learning_knowledge import current_principal
            knowledge_principal=current_principal()
        from region_index import get_region_index
        from rule_dictionary import runtime_store
        store=runtime_store()
        grammar_fields=('id','kind','form','scope','principal_digest','status','revision')
        candidate_fields=('id','type','alias','standard','category','scope','warehouse_id','category_id','product_id','status','rejection_revoked_at')
        rules=dict(aliases=store.get('aliases',[]),
                   learning_candidates=[{k:r.get(k) for k in grammar_fields} for r in store.get('learning_candidates',[])],
                   brand_candidates=[{k:r.get(k) for k in candidate_fields} for r in store.get('brand_candidates',[])],
                   rejected_flavor_mappings=store.get('rejected_flavor_mappings',[]),
                   dispatch_reference_prefixes=store.get('dispatch_reference_prefixes',[]))
        encoded=json.dumps([products,rules,PRODUCTS],sort_keys=True,ensure_ascii=False,separators=(',',':'))
        if len(encoded)>1048576:
            return None
        principal=hashlib.sha256(str(knowledge_principal).encode()).digest() if knowledge_principal is not None else None
        return (message,hashlib.sha256(encoded.encode()).digest(),get_region_index(),PARSER_RULESET_VERSION,principal)
    except (TypeError,ValueError,OSError):
        return None


from request_memo import request_memoized


class OrderParser:
    """订单信息提取器"""

    @staticmethod
    @__import__('order_trace').timed_sync('PARSER_EXTRACT')
    @request_memoized(_parse_memo_key)
    def extract_order_info(message: str, products=(), knowledge_principal=None) -> Optional[Dict]:
        """
        从消息中提取订单信息（移除仓库相关解析）
        Returns: {
            "items": [(商品名, 数量), ...],  # 不包含仓库信息
            "address": 地址,
            "recipient": 收件人,
            "phone": 电话
        }
        """
        if knowledge_principal is None:
            from learning_knowledge import current_principal
            knowledge_principal=current_principal()
        original_message = str(message or "")
        from learning_knowledge import apply_reviewed_grammar
        message = apply_reviewed_grammar(original_message, products, knowledge_principal)
        from order_structure import normalize_dispatch_markers
        message,_dispatch_notes=normalize_dispatch_markers(message,products)
        from numeric_metadata import separate
        message,_references,_amounts=separate(message,products)
        from contact_fields import merchandise_text
        # 标准化消息（全角转半角）
        message = OrderParser._normalize_text(structure_order_text(merchandise_text(message,contact_labels=True,products=products),products).text)

        # 优先兼容参考机器人的多行模板
        vape = OrderParser.extract_vape_style(message,products)
        if vape:
            return OrderParser._attach_parse_metadata(original_message, vape, products,knowledge_principal)

        # 对没有完全套用模板的消息，先把混合行拆成“商品片段”和“收货片段”。
        # 这样“地址... 姓名... 电话... 商品...”仍能进入同一套商品解析逻辑。
        raw_lines = [line.strip() for line in message.splitlines() if line.strip()]
        lines = raw_lines[:]
        if lines and re.fullmatch(r"(下单|购买|订单|下单模板)", lines[0].replace(" ", "")):
            lines = lines[1:]
        embedded_item_lines = OrderParser._extract_embedded_item_lines("\n".join(lines))
        delivery_text = OrderParser._strip_embedded_items_for_delivery("\n".join(lines))
        delivery_lines = [line.strip() for line in delivery_text.splitlines() if line.strip()]
        parsed_address, parsed_recipient, parsed_phone, delivery_indices = (
            OrderParser._extract_flexible_delivery(delivery_lines, delivery_text)
        )
        item_lines = OrderParser._extract_item_lines(
            lines,
            delivery_indices,
            parsed_address,
            parsed_recipient,
            parsed_phone,
        )
        items = OrderParser._parse_contextual_items(item_lines,products)
        if not items:
            # 保留旧式“商品: 苹果x2”解析作为兜底。
            # Contact rows were already consumed above. The legacy adapter
            # must not read the full message again and turn a room number or
            # recipient label into merchandise when the first pass is empty.
            items = OrderParser._extract_items('\n'.join(item_lines))

        address = parsed_address or OrderParser._extract_address(message)
        recipient = parsed_recipient or OrderParser._extract_recipient(message)
        phone = parsed_phone or OrderParser._extract_phone(message)
        if not address or not recipient or not phone:
            fallback_address, fallback_recipient, fallback_phone = OrderParser._delivery_from_text(message)
            address = address or fallback_address
            recipient = recipient or fallback_recipient
            phone = phone or fallback_phone
        if address and (
            len(str(address).strip()) < 4
            or str(address).strip() in {"地址", "收货地址", "配送地址"}
        ):
            address = None
        if recipient and str(recipient).strip() in {
            "地址", "收货地址", "配送地址", "电话", "手机", "联系电话",
            "联系方式", "号码", "商品", "货品", "物品",
        }:
            recipient = None

        address, recipient, phone = OrderParser._normalize_delivery_fields(address, recipient, phone)
        if not (items or address or recipient or phone or re.search(
            r"(下单|购买|订单|商品|货品|物品|地址|收件人|收货人|姓名|电话|手机)",
            message,
        )):
            return None
        return OrderParser._attach_parse_metadata(original_message, {
            "items": items,
            "address": address,
            "recipient": recipient,
            "phone": phone
        }, products,knowledge_principal)

    @staticmethod
    def extract_vape_style(message: str, products=()) -> Optional[Dict]:
        """兼容参考机器人模板：
        下单
        地址
        姓名
        电话
        型号
        口味x数量
        口味数量
        """
        flexible = OrderParser._extract_flexible_vape_order(message,products)
        if flexible:
            return flexible

        raw_lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
        if not raw_lines:
            return None
        lines = raw_lines[:]
        # 允许第一行是“下单/购买/订单”
        if re.fullmatch(r"(下单|购买|订单|下单模板)", lines[0].replace(" ", "")):
            lines = lines[1:]
        if len(lines) < 4:
            if len(lines) >= 3:
                parsed_address, parsed_recipient, parsed_phone = OrderParser._delivery_from_text(lines[0])
                if parsed_address and parsed_recipient and parsed_phone:
                    items = OrderParser._parse_contextual_items(lines[1:],products)
                    if not items:
                        return None
                    address, recipient, phone = OrderParser._normalize_delivery_fields(
                        parsed_address, parsed_recipient, parsed_phone
                    )
                    return {
                        "items": items,
                        "address": address,
                        "recipient": recipient,
                        "phone": phone,
                    }
            return None

        # The positional fallback is only for an actual address/name/phone
        # template. Labelled arbitrary-order contact fields must stay in the
        # flexible path, not become "address / name / goods" by line number.
        if not OrderParser._is_address_like(lines[0]):
            return None
        if not (valid_name(lines[1]) and name_has_evidence(lines[1], message)):
            return None

        # 实际模板: 1地址 2姓名 3电话 4..商品
        # 有时电话在第3行
        address = OrderParser._extract_address(lines[0]) or lines[0]
        recipient = OrderParser._extract_recipient(lines[1]) or lines[1]
        phone_line = lines[2]
        item_lines = lines[3:]

        phone = OrderParser._clean_phone(phone_line)
        if not phone:
            # 尝试在前几行找电话
            for i, ln in enumerate(lines[:5]):
                p = OrderParser._clean_phone(ln)
                if p:
                    phone = p
                    # reconstruct around this
                    if i >= 2:
                        address = lines[0]
                        recipient = lines[1]
                        item_lines = lines[i+1:]
                    break
        if not phone:
            return None

        items = OrderParser._parse_contextual_items(item_lines,products)
        if not items:
            return None
        # 过滤明显不是地址的情况
        if len(address) < 4:
            return None
        address, recipient, phone = OrderParser._normalize_delivery_fields(address, recipient, phone)
        return {
            "items": items,
            "address": address,
            "recipient": recipient,
            "phone": phone,
        }

    @staticmethod
    def _is_address_like(text: str) -> bool:
        value = str(text or "").strip()
        if len(value) < 6:
            return False
        markers = (
            "省", "自治区", "市", "区", "县", "旗", "镇", "乡",
            "街道", "路", "街", "巷", "村", "社区", "号", "栋",
            "单元", "室",
        )
        return sum(1 for marker in markers if marker in value) >= 2

    @staticmethod
    def _looks_like_phone_quantity(value: int) -> bool:
        return bool(re.fullmatch(r"1[3-9]\d{9}", str(value or "")))

    @staticmethod
    def _embedded_item_pattern() -> re.Pattern:
        return re.compile(
            r"(?:品类|型号)\([^)]+\)\s*口味\([^)]+\)\s*"
            r"(?:[xX×\*]\s*|\s+)?\d+\s*(?:个|支|条|颗|盒|件)?",
            re.IGNORECASE,
        )

    @staticmethod
    def _extract_embedded_item_lines(text: str) -> List[str]:
        """提取粘在地址/姓名/电话同一行里的完整商品片段。"""
        return [match.group(0).strip() for match in OrderParser._embedded_item_pattern().finditer(text or "")]

    @staticmethod
    def _strip_embedded_items_for_delivery(text: str) -> str:
        """地址解析前移除完整商品片段，避免商品名混进地址或姓名。"""
        return OrderParser._embedded_item_pattern().sub(" ", text or "")

    @staticmethod
    def _label_value(line: str, labels: str) -> Optional[str]:
        pattern = rf"^\s*(?:{labels})\s*[:：]?\s*(.*?)\s*$"
        match = re.match(pattern, str(line or ""), re.IGNORECASE)
        if not match:
            return None
        value = match.group(1).strip()
        return value or None

    @staticmethod
    def _extract_flexible_delivery(
        lines: List[str],
        message: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], set[int]]:
        """从任意行序中提取地址、姓名、电话，并返回应排除的联系信息行。"""
        phone = OrderParser._extract_phone(message)
        address: Optional[str] = None
        recipient: Optional[str] = None
        phone_indices = {
            index for index, line in enumerate(lines)
            if OrderParser._extract_phone(line)
        }
        delivery_indices = set(phone_indices)

        # 标签格式优先，例如“地址：... / 姓名：... / 电话：...”
        label_expr = (
            r"收货地址|配送地址|所在地区|详细地址|地区|地址|收件人|收货人|姓名|名字|联系人|"
            r"手机号码|手机号|手机|联系电话|电话|联系方式|号码"
        )
        label_re = re.compile(
            rf"(?P<label>{label_expr})(?:\s*[:：]\s*|\s+)",
            re.IGNORECASE,
        )
        for index, line in enumerate(lines):
            matches = list(label_re.finditer(line))
            if matches:
                delivery_indices.add(index)
                for match_index, match in enumerate(matches):
                    end = (
                        matches[match_index + 1].start()
                        if match_index + 1 < len(matches)
                        else len(line)
                    )
                    value = line[match.end():end].strip(" \t,，;；|")
                    if not value and index + 1 < len(lines):
                        next_line = lines[index + 1].strip()
                        if next_line and not label_re.search(next_line):
                            value = next_line
                            delivery_indices.add(index + 1)
                    label = match.group("label")
                    if label in {"收货地址", "配送地址", "所在地区", "详细地址", "地区", "地址"} and value:
                        address = (address or '') + value if label == '详细地址' else value
                    elif label in {"收件人", "收货人", "姓名", "名字", "联系人"} and value:
                        recipient = value

            if re.fullmatch(
                r"\s*(?:收货地址|配送地址|所在地区|地区|地址|收件人|收货人|姓名|名字|联系人|"
                r"手机号|手机号码|手机|联系电话|电话|联系方式|号码)\s*[:：]?\s*",
                line,
            ) and index + 1 < len(lines):
                label=line.strip(' :：')
                following=lines[index+1].strip()
                if label in {'手机号','手机号码','手机','联系电话','电话','联系方式','号码'}:
                    if OrderParser._extract_phone(following):delivery_indices.add(index+1)
                elif label in {'地址','收货地址','配送地址','所在地区','地区'}:
                    if OrderParser._is_address_like(following):delivery_indices.add(index+1)
                elif valid_name(following):
                    delivery_indices.add(index+1)

        address_indices: set[int] = set()
        for index, line in enumerate(lines):
            if OrderParser._is_address_like(line):
                address_indices.add(index)
                delivery_indices.add(index)

        # 地址、姓名、电话粘在同一行时，调用地址解析器解析这一行。
        candidate_indices = sorted(phone_indices | address_indices)
        for index in candidate_indices:
            if index in address_indices and index not in phone_indices:
                if _normalize_cn_address:
                    try:
                        normalized = _normalize_cn_address(lines[index])
                        normalized_address = str(
                            normalized.get("normalized_address") or ""
                        ).strip()
                        if normalized_address and not address:
                            address = normalized_address
                    except Exception:
                        pass
                if not address:
                    address = lines[index].strip()
                continue
            parsed_address, parsed_name, parsed_phone = OrderParser._delivery_from_text(lines[index])
            if parsed_phone and not phone:
                phone = parsed_phone
            if (
                index in phone_indices
                and parsed_name
                and parsed_name not in {
                    "地址", "收货地址", "配送地址", "电话", "手机",
                    "联系电话", "联系方式", "号码",
                }
                and not recipient
            ):
                recipient = parsed_name
            if parsed_address and OrderParser._is_address_like(parsed_address) and not address:
                address = parsed_address

        # 电话所在行通常包含“姓名 手机号 地址”，再用现有规则补一次姓名。
        if not recipient:
            for index in sorted(phone_indices):
                candidate = OrderParser._extract_recipient(lines[index])
                if candidate and candidate not in {
                    "地址", "收货地址", "配送地址", "电话", "手机",
                    "联系电话", "联系方式", "号码",
                }:
                    recipient = candidate
                    break

        # 地址单独一行时只取地址字段，避免把地址中的词误当收件人。
        if not address:
            for index in sorted(address_indices):
                if _normalize_cn_address:
                    try:
                        normalized = _normalize_cn_address(lines[index])
                        address = str(normalized.get("normalized_address") or "").strip()
                    except Exception:
                        address = ""
                if not address:
                    address = lines[index].strip()
                if address:
                    break

        # 没有“姓名：”标签且姓名单独一行时，按手机号附近的短中文行择优。
        if not recipient and phone:
            phone_index = min(phone_indices, key=lambda i: abs(i - (len(lines) // 2))) if phone_indices else -1
            candidates: list[tuple[int, str]] = []
            for index, line in enumerate(lines):
                value = line.strip()
                if index in delivery_indices or OrderParser._is_address_like(value):
                    continue
                if OrderParser._parse_item_quantity(value):
                    continue
                if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,12}", value):
                    distance = abs(index - phone_index) if phone_index >= 0 else 99
                    if phone_index >= 0 and index > phone_index:
                        distance += 1
                    candidates.append((distance, value))
            if candidates:
                candidates.sort(key=lambda item: (item[0], -len(item[1])))
                recipient = candidates[0][1]

        return address, recipient, phone, delivery_indices

    @staticmethod
    def _extract_flexible_vape_order(message: str, products=()) -> Optional[Dict]:
        """支持地址、姓名、电话、商品按任意顺序发送。"""
        normalized = OrderParser._normalize_text(message or "")
        raw_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not raw_lines:
            return None
        lines = raw_lines[:]
        if re.fullmatch(r"(下单|购买|订单|下单模板)", lines[0].replace(" ", "")):
            lines = lines[1:]
        if not OrderParser._extract_phone(normalized):
            return None

        embedded_item_lines = OrderParser._extract_embedded_item_lines("\n".join(lines))
        delivery_text = OrderParser._strip_embedded_items_for_delivery("\n".join(lines))
        delivery_lines = [line.strip() for line in delivery_text.splitlines() if line.strip()]
        address, recipient, phone, delivery_indices = OrderParser._extract_flexible_delivery(
            delivery_lines,
            delivery_text,
        )
        if not (address and recipient and phone):
            return None

        item_lines = OrderParser._extract_item_lines(
            lines,
            delivery_indices,
            address,
            recipient,
            phone,
        )

        items = OrderParser._parse_contextual_items(item_lines,products)
        if not items:
            return None
        # Distinct customer occurrences accumulate; equal quantities are not
        # evidence of an accidental duplicate. Source spans are consumed once
        # in _extract_item_lines before this deterministic aggregation.
        accumulated = {}
        for name, quantity in items:
            accumulated[name] = accumulated.get(name, 0) + quantity
        items = list(accumulated.items())
        address, recipient, phone = OrderParser._normalize_delivery_fields(
            address,
            recipient,
            phone,
        )
        return {
            "items": items,
            "address": address,
            "recipient": recipient,
            "phone": phone,
        }

    @staticmethod
    def _extract_inline_item_segments(line: str) -> List[str]:
        """提取同一行中的“商品: ...”片段，保留后续商品文本。"""
        value = str(line or "").strip()
        if not value:
            return []
        field_re = re.compile(
            r"(?P<label>地址|收货地址|配送地址|收件人|收货人|姓名|名字|联系人|"
            r"电话|手机|联系电话|联系方式|商品|货品|物品|购买|下单)\s*[:：]\s*"
        )
        matches = list(field_re.finditer(value))
        if not matches:
            return []
        segments: List[str] = []
        for index, match in enumerate(matches):
            if match.group("label") not in {"商品", "货品", "物品", "购买", "下单"}:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            segment = value[match.end():end].strip(" \t,，、;；")
            if segment:
                segments.append(segment)
        return segments

    @staticmethod
    def _extract_item_lines(
        lines: List[str],
        delivery_indices: set[int],
        address: Optional[str],
        recipient: Optional[str],
        phone: Optional[str],
    ) -> List[str]:
        """从任意顺序文本中提取商品行，同时保留混合行里的商品片段。"""
        item_lines: List[str] = []
        embedded_pattern = OrderParser._embedded_item_pattern()
        embedded = OrderParser._extract_embedded_item_lines("\n".join(lines))
        item_lines.extend(embedded)
        pure_delivery_label = re.compile(
            r"\s*(?:地址|收货地址|配送地址|收件人|收货人|姓名|名字|联系人|"
            r"电话|手机|联系电话|联系方式|商品|货品|物品|购买|下单)\s*[:：]?\s*$"
        )
        for index, raw_line in enumerate(lines):
            value = str(raw_line or "").strip()
            if not value:
                continue

            inline_segments = OrderParser._extract_inline_item_segments(value)
            if inline_segments:
                item_lines.extend(inline_segments)
                continue
            # Explicit 商品: segments above can share a contact line. Other
            # contact labels must not become generic product headings.
            from contact_fields import LABEL_RE
            if LABEL_RE.match(value):
                continue
            if embedded_pattern.search(value):
                continue  # Already extracted above, including mixed phone rows.
            if index in delivery_indices:
                # 若手机号后面仍粘着商品，保留手机号之后的尾部。
                if phone:
                    phone_match = re.search(
                        rf"(?<!\d){re.escape(str(phone))}(?!\d)",
                        value,
                    )
                    if phone_match:
                        tail = value[phone_match.end():].strip(" \t,，、;；")
                        tail = re.sub(
                            r"^(?:商品|货品|物品|购买|下单)\s*[:：]?\s*",
                            "",
                            tail,
                        ).strip()
                        if tail and not OrderParser._is_address_like(tail) and (re.search(r"\d", tail) or embedded_pattern.search(tail)):
                            item_lines.append(tail)
                continue
            if embedded_pattern.search(value):
                continue
            if pure_delivery_label.fullmatch(value):
                continue
            if recipient and value == recipient:
                continue
            if address and (value == address or value in address):
                continue
            if OrderParser._is_address_like(value):
                continue
            item_lines.append(value)
        return item_lines

    @staticmethod
    def _clean_phone(text: str) -> Optional[str]:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        if re.fullmatch(r"1\d{10}", digits):
            return digits
        if re.fullmatch(r"\d{7,12}", digits):
            return digits
        return None

    def _normalize_text(text: str) -> str:
        """标准化文本（全角转半角）"""
        text = normalize_order_text(text)
        return (
            text.replace('：', ':').replace('，', ',').replace('。', '.')
            .replace('（', '(').replace('）', ')')
            .replace('【', '[').replace('】', ']').replace('、', ',')
        )

    @staticmethod
    def _extract_items(message: str) -> List[Tuple[str, int]]:
        """提取商品列表（无仓库信息）"""
        items = []

        # 匹配模式1: "商品: 苹果 x2, 香蕉 x1"
        # 匹配模式2: "商品: 苹果2个, 香蕉1个"
        # 匹配模式3: 直接列出的商品
        patterns = [
            r'商品[:：]\s*([^\n]+)',
            r'购买[:：]\s*([^\n]+)',
            r'下单[:：]\s*([^\n]+)',
            r'物品[:：]\s*([^\n]+)',
        ]

        product_section = None
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                product_section = match.group(1).strip()
                break

        if not product_section:
            # 如果没有明确的商品标记，尝试在整个消息中找
            product_section = message

        # 提取单个商品
        items = OrderParser._parse_products(product_section)

        return items

    @staticmethod
    def _parse_products(text: str) -> List[Tuple[str, int]]:
        """解析商品文本"""
        items = []

        # 模式1: "苹果 x2" 或 "苹果X2"
        pattern1 = r'([^,，\n]+?)\s*X\s*(\d+)'
        matches1 = re.findall(pattern1, text, re.IGNORECASE)
        for product, quantity in matches1:
            product = OrderParser._clean_product_name(product)
            if product and not OrderParser._looks_like_phone_quantity(int(quantity)):
                items.append((product, int(quantity)))

        # 模式2: "苹果2个" 或 "苹果2个"
        pattern2 = r'([^,，\n]+?)(\d+)\s*个'
        matches2 = re.findall(pattern2, text)
        for product, quantity in matches2:
            product = OrderParser._clean_product_name(product)
            qty = int(quantity)
            if product and not (
                OrderParser._looks_like_phone_quantity(qty)
            ):
                items.append((product, qty))

        # 模式3: "苹果 5" （数字在商品名后面）
        pattern3 = r'([^,，\n]+?)\s+(\d+)(?!\s*个)'
        matches3 = re.findall(pattern3, text)
        for product, quantity in matches3:
            product = OrderParser._clean_product_name(product)
            qty = int(quantity)
            if product and not (
                OrderParser._looks_like_phone_quantity(qty)
            ):
                items.append((product, qty))

        # 模式4: 如果只有一个商品且没有数量，默认为1
        if not items:
            for product in PRODUCTS.keys():
                if product in text and len(product) > 1:
                    items.append((product, 1))
                    break

        return items

    @staticmethod
    def _strip_product_token(text: str) -> str:
        text = str(text or "").strip()
        text = re.sub(r"^(?:购买|商品|物品|下单)[:：]?", "", text).strip()
        # 兼容“口味 青柠：50”“口味：青柠：50”等标签写法。
        # 口味/味道标签后的冒号经常被省略，型号/品类则仍要求冒号，
        # 防止把单独一行的“型号 鸭嘴”误去掉分类名称。
        text = re.sub(r"^(?:口\s*味|味\s*道)\s*(?:[:：]\s*)?", "", text).strip()
        text = re.sub(r"^(?:型号|品类)[:：]\s*", "", text).strip()
        # Emoji may be an audited category/flavor alias. Preserve its token;
        # catalog-scoped resolution, not punctuation cleaning, determines validity.
        def token_character(character):
            return bool(re.match(r'[A-Za-z0-9\u4e00-\u9fff+()（）]',character)
                        or unicodedata.category(character)=='So'
                        or character in '%％\ufe0e\ufe0f')
        while text and not token_character(text[0]):text=text[1:]
        while text and not token_character(text[-1]):text=text[:-1]
        text=text.strip()
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @staticmethod
    def _strip_inventory_status_suffix(text: str) -> str:
        """移除库存预览转发时附在数量后的状态，例如“30（正常）”。"""
        value = str(text or "").strip()
        return re.sub(
            r"\s*[\(（]\s*(?:"
            r"正常|"
            r"少量(?:\s*[≤<]\s*\d+)?(?:[，,、;；\s]*(?:暂停售卖|暂不出货))?|"
            r"缺货(?:[，,、;；\s]*(?:暂停售卖|暂不出货))?"
            r")\s*[\)）]\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

    @staticmethod
    def _forwarded_inventory_category(line: str) -> Optional[str]:
        """从“高维库存 15号陆续，开始接单”这类库存标题取得品类。"""
        value = OrderParser._strip_product_token(line)
        match = re.match(r"^(?P<model>.+?)\s*库存(?:\s|[:：]|$)", value)
        if not match:
            return None
        model = OrderParser._strip_product_token(match.group("model"))
        return model or None

    @staticmethod
    def _parse_item_quantity(text: str) -> Optional[Tuple[str, int]]:
        text = OrderParser._strip_inventory_status_suffix(text)
        text = OrderParser._strip_product_token(text)
        if not text:
            return None
        if re.fullmatch(r'[gG]8', text) or re.search(r'\d\s*[+＋=＝]\s*\d', text):
            return None
        if re.match(r'^(?:金额|总金额|货款|单价|应付|价格)\s*[:：]?', text):
            return None
        if re.fullmatch(r'\d+\s*(?:号|栋|幢|楼|层|室|单元|门|铺|档|座).+', text):
            return None
        prefix = re.fullmatch(r'(\d+)\s*(?:根|支|个|件|盒)\s*([^\d].+)', text)
        if prefix and int(prefix.group(1)) > 0:
            return OrderParser._strip_product_token(prefix.group(2)), int(prefix.group(1))
        prefix = re.fullmatch(r'(\d{1,3})([\u4e00-\u9fff]{2,})', text)
        if prefix and int(prefix.group(1)) > 0:
            return OrderParser._strip_product_token(prefix.group(2)), int(prefix.group(1))
        # 客户经常把末尾数量拆成“1 0”，统一为 10，避免被识别成型号文本。
        previous = None
        while previous != text:
            previous = text
            text = re.sub(
                r"(?<=\D)(\d)\s+(\d)(?=\s*(?:个|支|条|颗|盒|件)?\s*$)",
                r"\1\2",
                text,
            )
        patterns = [
            r"^(.*?)(?:\s*[:：]\s*|\s*[xX×\*]\s*|\s+)(\d+)\s*(?:根|个|支|只|条|颗|盒|件)?$",
            r"^(.*?)(\d+)\s*(?:根|个|支|只|条|颗|盒|件)?$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            name = OrderParser._strip_product_token(match.group(1))
            qty = int(match.group(2))
            if name and qty > 0 and not OrderParser._looks_like_phone_quantity(qty):
                return name, qty
        return None

    @staticmethod
    def _contextual_product_name(model: Optional[str], flavor: str) -> str:
        ref = OrderParser._canonical_product_reference(flavor)
        if ref:
            return ref
        flavor = OrderParser._strip_product_token(flavor)
        if model:
            model = OrderParser._strip_product_token(model)
            if model:
                # “板砖绿豆20个”这类连写，去掉已知型号前缀后再匹配口味。
                if flavor.startswith(model) and len(flavor) > len(model):
                    flavor = flavor[len(model):].strip()
                return f"品类({model})口味({flavor})"
        # Preserve the complete customer token even when the category heading
        # was not retained by a legacy delivery adapter. The demo fruit price
        # table is not the live catalog: substring matching here turns e.g.
        # a longer flavor into a different product before catalog validation.
        # Existing scoped Alias/catalog resolution remains authoritative.
        return flavor

    @staticmethod
    def _is_order_summary_line(line: str) -> bool:
        """识别合计、订单编号、独立总数量等非商品行。"""
        value = re.sub(r"\s+", "", str(line or "")).strip("()（）[]【】")
        if not value:
            return True
        if re.fullmatch(r"(?:订单|新订单)\d*", value):
            return True
        if re.fullmatch(r'(?:共|总|共计|总计|合计|小计|一共|总共)[:：]?',value):return True
        return bool(
            re.fullmatch(
                r"(?:共|总|共计|一共|合计|小计|订单合计|订单总数|整单(?:合计|总计|总数)?|总计|总共|总数|数量)?[:：]?\d+(?:根|跟|个|支|条|颗|盒|件)?",
                value,
            )
        )

    @staticmethod
    def _is_ambiguous_distribution_line(line: str, products=()) -> bool:
        """识别“均配随机/每个口味”这类缺少具体口味的说明。"""
        raw = re.sub(r"\s+", "", str(line or ""))
        if not re.search(r'\d',raw) or not re.search(r'每个口味|全口味|均配|随机',raw):
            return False
        from order_structure import syntax_text,catalog_names
        source=syntax_text(str(line or ''),catalog_names(products)) if products else str(line or '')
        value = re.sub(r"\s+", "", source)
        if not re.search(r"\d", value):
            return False
        return bool(
            re.search(r"(?:每个口味|全口味)", value)
            or re.search(r"(?:均配|随机)", value)
        )

    @staticmethod
    def _looks_like_model_total_line(line: str) -> bool:
        """区分“型号 + 总数”与普通“口味 + 数量”行。"""
        value = OrderParser._strip_product_token(line)
        if not value:
            return False
        has_model_hint = bool(
            re.search(r"(?:板砖|鸭嘴|外星人|雪茄|悦刻|UFO|小野|GO)", value, re.IGNORECASE)
        )
        if not has_model_hint:
            return False
        if re.search(r"\d+\s*(?:根|跟|个|支|条|颗|盒|件)\s*$", value):
            return True
        if re.match(r"^\d{2,4}(?=[\u4e00-\u9fff])", value):
            return True
        if re.match(r"^\d+\s*(?:根|跟|个|支|条|颗|盒|件)\s*", value):
            return True
        if re.search(r"(?:新|旧|老)版本\s*\d+\s*$", value):
            return True
        return bool(re.search(r"(?:共|共计|合计|总计|总共)\s*\d+", value))

    @staticmethod
    def _strip_model_total(line: str) -> str:
        """从型号描述中删除总根数/总个数，保留可用于匹配的型号名称。"""
        value = OrderParser._strip_product_token(line)
        value = re.sub(
            r"(?:[，,。.…]+)?\s*(?:共|共计|合计|总计|总共)\s*\d+\s*(?:根|跟|个|支|条|颗|盒|件)?\s*$",
            "",
            value,
        )
        value = re.sub(r"\d+\s*(?:根|跟|个|支|条|颗|盒|件)\s*$", "", value)
        value = re.sub(r"^\d+\s*(?:根|跟|个|支|条|颗|盒|件)\s*", "", value)
        value = re.sub(r"^\d{2,4}(?=[\u4e00-\u9fff])", "", value)
        if re.search(r"(?:新|旧|老)版本\s*\d+\s*$", value):
            value = re.sub(r"\d+\s*$", "", value)
        return OrderParser._strip_product_token(value)

    @staticmethod
    def _parse_comma_item_line(line: str, current_model: Optional[str]) -> Tuple[Optional[str], List[Tuple[str, int]]]:
        tokens = [
            OrderParser._strip_product_token(part)
            for part in re.split(r"[,，、;；/／]+", line)
            if OrderParser._strip_product_token(part)
        ]
        items: List[Tuple[str, int]] = []
        if not tokens:
            return current_model, items
        index = 0
        first_parsed = OrderParser._parse_item_quantity(tokens[0])
        if (
            not first_parsed
            and current_model is None
            and len(tokens) > 1
            and not re.fullmatch(r"\d+", tokens[1])
        ):
            current_model = tokens[0]
            index = 1
        while index < len(tokens):
            token = tokens[index]
            if OrderParser._is_order_summary_line(token):
                index+=1;continue
            parsed = OrderParser._parse_item_quantity(token)
            if parsed:
                flavor, qty = parsed
                items.append((OrderParser._contextual_product_name(current_model, flavor), qty))
                index += 1
                continue
            if index + 1 < len(tokens) and re.fullmatch(r"\d+", tokens[index + 1]):
                qty = int(tokens[index + 1])
                if qty > 0:
                    items.append((OrderParser._contextual_product_name(current_model, token), qty))
                index += 2
                continue
            if current_model and not OrderParser._looks_like_category_token(token):
                items.append((OrderParser._contextual_product_name(current_model,token),1))
            else:current_model = token
            index += 1
        return current_model, items

    @staticmethod
    def _parse_compact_item_line(line: str, current_model: Optional[str]) -> List[Tuple[str, int]]:
        """解析同一行多个“口味+数量”粘连的写法。

        例如：
        - 香芋 20龙井20薄荷 60
        - 香芋20 龙井20 薄荷60
        - 香芋x20龙井x20薄荷x60

        返回多条时才生效；单条仍交给 _parse_item_quantity，避免误拆型号名中的数字。
        """
        text = OrderParser._strip_product_token(line)
        if not text:
            return []
        if OrderParser._canonical_product_reference(text):
            return []
        if any(sep in text for sep in [",", "，", "、", ";", "；"]):
            return []
        # 实际订单常用句号连接口味，例如“西瓜冰50.桃子冰20”。
        # 小数规格中的点不是商品分隔符（例如型号 ``1.2w``）；若把它
        # 拆开，会把规格两侧各伪造为一条默认数量商品。
        text = re.sub(r"。|(?<!\d)\.|\.(?!\d)", " ", text)

        pattern = re.compile(
            r"\s*(?P<name>.+?)\s*"
            r"(?:[xX×\*]\s*)?"
            r"(?P<qty>\d+)(?!\.\d)\s*"
            r"(?:根|个|支|只|条|颗|盒|件)?"
            r"(?=\s*(?:$|[^0-9A-Za-z]))"
        )
        pos = 0
        raw_parsed: List[Tuple[str, int]] = []
        while pos < len(text):
            match = pattern.match(text, pos)
            if not match:
                # 剩余只有空白/分隔符时允许结束，否则判定为非紧凑多商品行。
                if re.fullmatch(r"[\s|/]+", text[pos:]):
                    break
                return []
            flavor = OrderParser._strip_product_token(match.group("name"))
            try:
                qty = int(match.group("qty"))
            except (TypeError, ValueError):
                return []
            if not flavor or qty <= 0:
                return []
            if OrderParser._looks_like_phone_quantity(qty) and OrderParser._is_address_like(flavor):
                return []
            raw_parsed.append((flavor, qty))
            pos = match.end()
            while pos < len(text) and text[pos] in " \t|/":
                pos += 1

        if len(raw_parsed) < 2:
            return []

        # “型号 口味20 口味20”在同一行时，正则的首个 name 会包含型号。
        # 根据型号/口味特征选择切分点，兼容“板砖二代 西瓜 冰50”与“雪加板砖 二代 2W口 桃子冰20”。
        inferred_model = current_model
        first_flavor, first_qty = raw_parsed[0]
        if not inferred_model and " " in first_flavor:
            words = first_flavor.split()
            model_hints = ("板砖", "鸭嘴", "外星人", "雪茄", "悦刻", "UFO", "小野", "GO")
            flavor_hints = (
                "冰", "桃", "西瓜", "青柠", "草莓", "蓝莓", "可乐", "椰子",
                "柠檬", "龙井", "百香", "葡萄", "薄荷", "香芋", "菠萝",
                "茉莉", "蜜瓜", "绿豆", "苹果", "西柚",
            )
            best = None
            for split_at in range(1, len(words)):
                possible_model = " ".join(words[:split_at]).strip()
                possible_flavor = " ".join(words[split_at:]).strip()
                if not possible_model or not possible_flavor:
                    continue
                score = 0
                if any(hint.lower() in possible_model.lower() for hint in model_hints):
                    score += 4
                if re.search(r"(?:\d+[Ww]|新版本|旧版本|老版本|二代|一代)", possible_model):
                    score += 2
                if any(hint in possible_flavor for hint in flavor_hints):
                    score += 2
                if len(re.sub(r"\s+", "", possible_flavor)) <= 6:
                    score += 1
                candidate = (score, split_at, possible_model, possible_flavor)
                if (
                    best is None
                    or score > best[0]
                    or (score == best[0] and split_at < best[1])
                ):
                    best = candidate
            if best:
                _, _, possible_model, possible_flavor = best
                inferred_model = possible_model
                raw_parsed[0] = (possible_flavor, first_qty)

        return [
            (OrderParser._contextual_product_name(inferred_model, flavor), qty)
            for flavor, qty in raw_parsed
        ]

    @staticmethod
    def _looks_like_category_token(value: str) -> bool:
        """识别没有写“品类/型号”标签的常见型号词。"""
        text = re.sub(r"\s+", "", str(value or ""))
        if not text:
            return False
        hints = (
            "板砖", "鸭嘴", "外星人", "雪茄", "悦刻", "UFO", "小野", "GO",
            "烟弹", "单颗", "大容量", "二代", "一代",
        )
        return any(hint.lower() in text.lower() for hint in hints) or bool(
            re.search(r"(?:\d+[Ww]|新版本|旧版本|老版本)", text)
        )

    @staticmethod
    def _split_category_prefix(line: str) -> Tuple[Optional[str], str]:
        """先拆出“型号/品类”，再把后半段交给口味数量解析。"""
        text = OrderParser._strip_product_token(line)
        if not text or OrderParser._canonical_product_reference(text):
            return None, text

        # 鸭嘴（西瓜2，苹果3）/ 鸭嘴(西瓜2,苹果3)
        match = re.match(r"^(?P<model>[^()（）,，;；/]+?)\s*[\(（](?P<body>.+)[\)）]\s*$", text)
        if match and re.search(r"\d", match.group("body")):
            return match.group("model").strip(), match.group("body").strip()

        # 品类：鸭嘴 / 型号：鸭嘴 口味：西瓜2 苹果3
        explicit = re.match(r"^(?:品类|型号)\s*[:：]?\s*(?P<rest>.+)$", text)
        if explicit:
            rest = explicit.group("rest").strip()
            labeled_flavor = re.match(
                r"^(?P<model>[^,，;；]+?)\s*(?:口味|味道)\s*[:：]\s*(?P<body>.+)$",
                rest,
            )
            if labeled_flavor:
                return labeled_flavor.group("model").strip(), labeled_flavor.group("body").strip()
            tokens = rest.split(None, 1)
            if len(tokens) == 1:
                return tokens[0].strip(), ""
            if re.search(r"\d", tokens[1]):
                return tokens[0].strip(), tokens[1].strip()

        # 鸭嘴：西瓜2，苹果3 / 鸭嘴:西瓜2,苹果3
        match = re.match(r"^(?P<model>[^:：,，;；/]+?)\s*[:：]\s*(?P<body>.+)$", text)
        if (
            match
            and re.search(r"\d", match.group("body"))
            # “青柠：50”是口味 + 数量，不是“分类：口味列表”。
            and not re.fullmatch(r"\s*\d+\s*(?:个|支|条|颗|盒|件)?\s*", match.group("body"))
        ):
            return match.group("model").strip(), match.group("body").strip()

        # 鸭嘴 口味：西瓜2 苹果3
        match = re.match(
            r"^(?P<model>.+?)\s*(?:口味|味道)\s*[:：]\s*(?P<body>.+)$",
            text,
        )
        if match and re.search(r"\d", match.group("body")):
            return match.group("model").strip(), match.group("body").strip()

        # 鸭嘴/西瓜2/苹果3
        slash_parts = [part.strip() for part in re.split(r"[/／]", text) if part.strip()]
        if len(slash_parts) >= 2 and not re.search(r"\d", slash_parts[0]) and re.search(
            r"\d", "/".join(slash_parts[1:])
        ):
            return slash_parts[0], "/".join(slash_parts[1:])

        # “要鸭嘴的，西瓜2个，苹果3个”这类口语化前缀。
        match = re.match(
            r"^(?:要|我要|来|给我|拿)\s*(?P<model>[^,，:：]+?)(?:的|款|型号)?\s*[,，:：]\s*(?P<body>.+)$",
            text,
        )
        if match and re.search(r"\d", match.group("body")):
            return match.group("model").strip(), match.group("body").strip()

        # 型号 + 多个带数量口味。仅在首词带有明显型号特征时推断，
        # 避免把“香芋 20龙井20薄荷60”或“冰镇蓝莓 1 0”误当成型号开头。
        tokens = text.split(None, 1)
        if len(tokens) == 2 and not re.search(r"\d", tokens[0]) and re.search(r"\d", tokens[1]):
            if OrderParser._looks_like_category_token(tokens[0]):
                return tokens[0].strip(), tokens[1].strip()
        return None, text

    @staticmethod
    def _parse_contextual_items(item_lines: List[str], products=()) -> List[Tuple[str, int]]:
        items: List[Tuple[str, int]] = []
        current_model: Optional[str] = None
        for raw_line in item_lines:
            # Contact/template splitting can leave a standalone field label.
            # It introduces the following items, not an implicit empty flavor.
            if re.fullmatch(r'(?:口味|味道)\s*[:：]?',raw_line.strip()):
                continue
            bare=re.fullmatch(r'([^:：\n]+)[:：]\s*([^\d\s:：,，;；()（）]+)',raw_line.strip())
            if (bare and OrderParser._looks_like_category_token(bare.group(1))
                    and not OrderParser._is_address_like(raw_line)):
                # Preserve unknown explicit model identity for normal catalog
                # confirmation; do not guess which model "新大板砖" means.
                current_model=bare.group(1).strip()
                items.append((OrderParser._contextual_product_name(current_model,bare.group(2)),1))
                continue
            explicit_heading=re.fullmatch(r'(?:品类|型号)[:：]\s*(.+)',raw_line.strip())
            if explicit_heading:
                current_model=explicit_heading.group(1).strip()
                continue
            line = OrderParser._strip_inventory_status_suffix(raw_line)
            if not line:
                continue
            # 「松66 / 崧66」是完整品类名，不是“松/崧 + 66 件”。
            # 这类品类常以字形变体开头并带数字，必须在通用的“口味+数量”
            # 解析前保留整行，否则后续口味会失去品类上下文。
            standalone_category = re.fullmatch(r"(?:松|崧)\s*[A-Za-z]?\d{1,4}", line)
            if current_model is None and standalone_category:
                current_model = line
                continue
            forwarded_category = OrderParser._forwarded_inventory_category(line)
            if forwarded_category:
                current_model = forwarded_category
                continue
            category, category_body = OrderParser._split_category_prefix(line)
            if category:
                current_model = OrderParser._strip_model_total(category) or category
                if not category_body:
                    continue
                line = category_body
            # A generation marker left after a proven category prefix is
            # model metadata, never a quantity of a flavor named "代".
            # Keep raw source for identity validation; this is not an Alias
            # mapping and must not rewrite which generation the customer asked.
            if current_model and re.fullmatch(r'(?:\d+|[一二两三四五六七八九十]+)\s*代', line):
                continue
            if OrderParser._is_order_summary_line(line):
                continue
            if OrderParser._is_ambiguous_distribution_line(line,products):
                continue
            # A whole parenthesized flavor followed by an explicit count is
            # merchandise syntax, not a note. Keep the original flavor token
            # for the final catalog validator; never resolve it across brands.
            wrapped_flavor = re.fullmatch(r'[（(]([^()（）]+)[）)]\s*(\d+)\s*(?:枝|支|根|个|件)', line)
            if current_model and wrapped_flavor and int(wrapped_flavor.group(2)) > 0:
                items.append((OrderParser._contextual_product_name(current_model, wrapped_flavor.group(1).strip()),
                              int(wrapped_flavor.group(2))))
                continue
            if OrderParser._looks_like_model_total_line(line):
                model = OrderParser._strip_model_total(line)
                if model:
                    current_model = model
                continue
            if any(sep in line for sep in [",", "，", "、", ";", "；", "/", "／"]):
                current_model, parsed_items = OrderParser._parse_comma_item_line(line, current_model)
                items.extend(parsed_items)
                continue
            # Entire named prefix-quantity sequences, not an unbounded number
            # scan. This must run before the suffix compact parser so the 2 in
            # "3西瓜 2香蕉" cannot become 西瓜's quantity.
            prefix_items = list(re.finditer(r'(\d{1,3})\s*(?:根|支|个|件|盒)?\s*([^\d\s,:：，、]+)', line))
            if prefix_items:
                residue = re.sub(r'(\d{1,3})\s*(?:根|支|个|件|盒)?\s*([^\d\s,:：，、]+)', '', line)
                if (not residue.strip() and all(int(m.group(1)) > 0 for m in prefix_items)
                        and not any(re.match(r'(?:号|栋|幢|楼|层|室|单元|门|铺|档|座)',m.group(2)) for m in prefix_items)):
                    items.extend((OrderParser._contextual_product_name(current_model,m.group(2)),int(m.group(1))) for m in prefix_items)
                    continue
            compact_items = OrderParser._parse_compact_item_line(line, current_model)
            if compact_items:
                items.extend(compact_items)
                if not current_model:
                    match = re.match(r"^品类\((.+)\)口味\(", str(compact_items[0][0]))
                    if match:
                        current_model = match.group(1).strip() or current_model
                continue
            parsed = OrderParser._parse_item_quantity(line)
            if parsed:
                flavor, qty = parsed
                if OrderParser._looks_like_phone_quantity(qty) and OrderParser._is_address_like(flavor):
                    continue
                items.append((OrderParser._contextual_product_name(current_model, flavor), qty))
                continue
            if current_model and not OrderParser._looks_like_category_token(line):
                if re.fullmatch(r'[（(]\s*(?:蔬菜|勿放单|不放单|代发)\s*[）)]',line):continue
                # An unquantified unknown flavor stays in its explicit
                # category and defaults to one. It must not become the
                # category for the next flavor or trailing declared total.
                items.append((OrderParser._contextual_product_name(current_model,line),1))
            else:
                current_model = OrderParser._strip_product_token(line)
        return items

    @staticmethod
    def _clean_product_name(product: str) -> str:
        """清理商品名称"""
        product = product.strip()
        ref = OrderParser._canonical_product_reference(product)
        if ref:
            return ref
        # 移除常见的前缀
        prefixes = ['购买', '商品', '物品', '下单']
        for prefix in prefixes:
            if product.startswith(prefix):
                product = product[len(prefix):].strip()
        product = re.sub(r'^[^A-Za-z0-9\u4e00-\u9fa5]+', '', product).strip()
        product = re.sub(r'\s+', ' ', product).strip()
        if not product:
            return product

        candidates = sorted(PRODUCTS.keys(), key=len, reverse=True)
        for name in candidates:
            if product == name:
                return name
        for name in candidates:
            if product.endswith(name):
                return name
        for name in candidates:
            if name in product:
                return name
        if ' ' in product:
            tail = product.split()[-1].strip()
            if tail:
                return tail
        return product

    @staticmethod
    def _canonical_product_reference(product: str) -> Optional[str]:
        """保留并标准化 品类(型号)口味(口味) 这种可消除歧义的写法。"""
        if not product:
            return None
        text = product.strip()
        patterns = [
            r"(?:品类|型号)\(([^)]+)\)\s*口味\(([^)]+)\)",
            r"(?:品类|型号)[:：]?\s*([^\s,，]+)\s+口味[:：]?\s*([^\s,，]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                category = match.group(1).strip()
                flavor = match.group(2).strip()
                if category and flavor:
                    return f"品类({category})口味({flavor})"
        return None

    @staticmethod
    def _normalize_delivery_fields(address: str, recipient: str, phone: str) -> Tuple[str, str, str]:
        address = str(address or "").strip()
        recipient = str(recipient or "").strip()
        phone = OrderParser._clean_phone(phone or "") or str(phone or "").strip()
        combined = " ".join(part for part in [recipient, phone, address] if part).strip()

        if _parse_address_record and combined:
            try:
                record = _parse_address_record(combined)
                parsed_name = str(getattr(record, "name", "") or "").strip()
                parsed_phone = str(getattr(record, "phone", "") or "").strip()
                parsed_address = str(getattr(record, "address", "") or "").strip()
                # 外部地址解析器会把地址开头的“广东/北京”等误当姓名。
                # 调用方已拆出独立收件人时，以原字段为准，只补缺失字段。
                if parsed_name and not recipient:
                    recipient = parsed_name
                if parsed_phone and not phone:
                    phone = parsed_phone
                if parsed_address and not address:
                    address = parsed_address
            except Exception:
                pass

        if _normalize_cn_address and address:
            try:
                normalized = _normalize_cn_address(address)
                normalized_address = str(normalized.get("normalized_address") or "").strip()
                if normalized_address:
                    address = normalized_address
            except Exception:
                pass

        return address, recipient, phone

    @staticmethod
    def _attach_parse_metadata(original_message: str, order_info: Dict, products=(), knowledge_principal=None) -> Dict:
        """给解析结果补充可展示的整理说明，不改变旧字段兼容性。"""
        structure = structure_order_text(original_message,products)
        from order_trace import stage
        with stage('CONTACT_LOCAL'):
            # Contact parsing owns its structural normalization. Passing the
            # already rewritten quantity text here loses the original inline
            # catalog/list boundary and can retain the model in the address.
            contacts = parse_contact(original_message, products=products)
            from contact_fields import with_generated_name
        result = merge_contact(order_info, contacts)
        from category_evidence import prepare as prepare_categories
        from rule_dictionary import runtime_store
        implicit_unknown=[]
        prepare_categories(original_message,products,runtime_store().get('aliases',[]),
                           implicit_unknown=implicit_unknown)
        result['implicit_unresolved_items']=implicit_unknown
        if 'recipient' not in contacts:
            # External address parsers may invent a name from an address
            # substring. Only retain an old-parser name with text boundaries.
            candidate = str(result.get('recipient') or '')
            delivery_text = OrderParser._strip_embedded_items_for_delivery(
                OrderParser._normalize_text(original_message)
            )
            delivery_text = re.sub(r'(?:商品|购买|货品|物品)\s*[:：][^\n]*', '', delivery_text)
            from contact_fields import contact_catalog_names
            if (not name_has_evidence(candidate, delivery_text) or not valid_name(candidate)
                    or candidate.casefold() in contact_catalog_names(products)):
                result['recipient'] = None
        result=with_generated_name(result)
        result['contact_diagnostics'] = diagnose_contact(result)
        original = str(original_message or "").strip()
        normalized = OrderParser._normalize_text(structure.text)
        address = str(result.get("address") or "").strip()
        recipient = str(result.get("recipient") or "").strip()
        phone = str(result.get("phone") or "").strip()
        summary_names={'小计','整单','共','共计','一共','合计','总计','总共','总数','数量'}
        items = [item for item in result.get("items") or []
                 if not OrderParser._looks_like_phone_quantity(int(item[1]))
                 and str(item[0] or '').strip() not in summary_names]
        result['items'] = items
        quantity_source=quantity_text(original,products)
        from catalog_quantity_scopes import analyze as analyze_catalog_scopes
        quantity_scope = analyze_catalog_scopes(quantity_source, items, products=products, ignore_leading_bare_capacity=True)
        result['customer_order_ref'] = structure.customer_order_ref
        result['customer_warehouse_notes'] = list(structure.customer_warehouse_notes)
        result['customer_amount_notes'] = list(structure.customer_amount_notes)
        result["parser_ruleset_version"] = PARSER_RULESET_VERSION
        from quantity_source_positions import locate
        result["quantity_scopes"] = locate(original_message,quantity_scope.facts,products,
                                          runtime_store().get('aliases',[]))
        result["section_subtotals"] = quantity_scope.to_dict()["section_subtotals"]
        result["declared_total"] = quantity_scope.declared_total
        result["computed_total"] = quantity_scope.computed_total
        result["quantity_scope_errors"] = list(quantity_scope.errors)
        # Catalog-bound parenthesized Chinese quantities outside the reviewed
        # strict grammar must never silently fall back to the business default
        # of one. Keep the source intact and force AI/manual rejection.
        from learning_knowledge import _chinese_quantity
        for product in products or ():
            name=str(product.get('name') or '')
            if not name:continue
            for match in re.finditer(re.escape(name)+r'[(（]([^()（）]{1,8})(?:支|根|件|个|条)[)）]',original):
                token=match.group(1)
                # Parenthesized section totals are NOT item numeral tokens.
                declaration=re.fullmatch(r'(?:共计|共|合计|总计|总共|一共)\s*(?:\d+|[一二两三四五六七八九十]+)',token)
                if (not declaration and not token.isdigit()
                        and _chinese_quantity(token.replace('两','二')) is None):
                    result['quantity_scope_errors'].append('商品中文数量格式无效，请使用一至九、十至九十九的规范写法。')
                    break
        if structure.customer_amount_notes and not any(f.kind=='ITEM_QUANTITY' for f in quantity_scope.facts):
            result['quantity_scope_errors'].append('商品数量未明确，收件信息后的金额不能作为数量；请填写商品数量后重新发送完整订单。')
        # Include explicit quantities of unknown catalog tokens: a partial
        # catalog match is not proof that the entire source order survived.
        source_units=sum(f.quantity for f in quantity_scope.facts if f.kind=='ITEM_QUANTITY')
        # Without a catalog, model/capacity headers cannot be reliably
        # distinguished from merchandise. Authoritative source conservation
        # is performed with the live catalog at final commit.
        if products and source_units and source_units!=quantity_scope.computed_total:
            result['quantity_scope_errors'].append(
                f'原文商品数量未完整保留：明确填写{source_units}支，识别明细{quantity_scope.computed_total}支；请核对全部商品后重新发送完整订单。')
        # Compare each unambiguous catalog item against its own source count;
        # equal grand totals alone do not prove merchandise coverage.
        from source_coverage import catalog_quantity_errors
        result['quantity_scope_errors'].extend(catalog_quantity_errors(quantity_scope.facts,items,products))
        # Proven live flavours without a written quantity are normalized to
        # one by category_evidence.  Anything still unresolved here is an
        # unknown/ambiguous token and must not disappear behind a partial AI
        # match.
        from source_coverage import unquantified_flavors
        unresolved_flavors=unquantified_flavors(quantity_source,products)
        if unresolved_flavors:
            result['unquantified_flavors']=unresolved_flavors
            result['quantity_scope_errors'].append('以下口味无法确认所属品类或商品：'+ '、'.join(unresolved_flavors)+'；请核对后重新发送完整订单。')
        if '[并列口味数量冲突]' in quantity_source:
            result['quantity_scope_errors'].append('并列口味数与声明总数不一致，请逐项明确数量。')

        delivery_text = OrderParser._strip_embedded_items_for_delivery(normalized)
        delivery_lines = [line.strip() for line in delivery_text.splitlines() if line.strip()]
        raw_address, raw_recipient, raw_phone = (None, None, None)
        if delivery_lines:
            raw_address, raw_recipient, raw_phone, _ = OrderParser._extract_flexible_delivery(
                delivery_lines,
                delivery_text,
            )

        corrections: List[str] = []
        if original != normalized:
            corrections.append("已统一全角标点、空格和换行")
        if any(
            len(line.splitlines()) == 1 and OrderParser._extract_inline_item_segments(line)
            for line in normalized.splitlines()
        ):
            corrections.append("已从混合行中拆分地址、姓名、电话和商品")
        if raw_address and address and raw_address != address:
            corrections.append(f"已整理收货地址：{address}")
        if raw_recipient and recipient and raw_recipient != recipient:
            corrections.append(f"已整理收件人：{recipient}")
        if items and any(
            re.match(r"^品类\(.+\)口味\(.+\)$", str(name or ""))
            for name, _ in items
        ):
            if not re.search(r"品类\s*[:：]?", original):
                corrections.append("已把型号、口味和数量归并为标准商品项")
        if not corrections and (items or address or recipient or phone):
            corrections.append("已识别并整理为标准订单字段")

        unresolved_fields: List[str] = []
        for key, label in (
            ("address", "收货地址"),
            ("recipient", "收件人姓名"),
            ("phone", "联系电话"),
            ("items", "商品和数量"),
        ):
            if not result.get(key):
                unresolved_fields.append(label)

        warnings: List[str] = []
        normalized_phone_text = re.sub(r"[\s\-]+", "", original)
        phone_candidates = list(dict.fromkeys(re.findall(r"1[3-9]\d{9}", normalized_phone_text)))
        multiple_orders_detected = len(phone_candidates) > 1
        requires_item_confirmation = any(
            OrderParser._is_ambiguous_distribution_line(line,products)
            for line in normalized.splitlines()
        )
        if multiple_orders_detected:
            warnings.append("检测到多个收货手机号，请按收件地址分开发送订单")
        if requires_item_confirmation:
            warnings.append("检测到均配/随机/每个口味说明，请补充每个口味的具体数量")
        if address and _normalize_cn_address:
            try:
                address_meta = _normalize_cn_address(address)
                address_warnings = [
                    str(item).strip()
                    for item in (address_meta.get("warnings") or [])
                    if str(item).strip()
                ]
                # 省、市、区县已经齐全时，镇/街道识别不到不影响正常收货，
                # 不在确认单中制造多余提示。
                has_region = all(
                    str(address_meta.get(key) or "").strip()
                    for key in ("province", "city", "district")
                )
                if not has_region:
                    warnings.extend(address_warnings[:2])
            except Exception:
                pass
        if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
            warnings.append("联系电话不是标准大陆手机号，请重点核对")
        if recipient and not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·\-\s]{2,20}", recipient):
            warnings.append("收件人字段包含较多符号，请重点核对")
        # 去重并保持顺序。
        warnings = list(dict.fromkeys(warnings))
        warnings.extend(error for error in quantity_scope.errors if error not in warnings)

        scores: List[float] = []
        if address:
            address_score = 0.92
            if warnings and any("地址" in warning or "省" in warning or "市" in warning for warning in warnings):
                address_score = 0.72
            scores.append(address_score)
        if recipient:
            scores.append(0.92 if raw_recipient or re.search(r"(收件人|收货人|姓名|名字|联系人)", original) else 0.76)
        if phone:
            scores.append(0.98 if re.fullmatch(r"1[3-9]\d{9}", phone) else 0.72)
        if items:
            scores.append(0.92 if any(
                re.match(r"^品类\(.+\)口味\(.+\)$", str(name or ""))
                for name, _ in items
            ) else 0.82)
        confidence = round(sum(scores) / len(scores), 2) if scores else 0.0

        canonical_lines: List[str] = []
        if address:
            canonical_lines.append(f"地址：{address}")
        if recipient:
            canonical_lines.append(f"收件人：{recipient}")
        if phone:
            canonical_lines.append(f"电话：{phone}")
        if items:
            canonical_lines.append("商品：")
            canonical_lines.extend(
                f"{name}x{quantity}" for name, quantity in items
            )

        result.update({
            "raw_text": original,
            "normalized_text": "\n".join(canonical_lines),
            "corrections": corrections,
            "warnings": warnings,
            "confidence": confidence,
            "unresolved_fields": unresolved_fields,
            "multiple_orders_detected": multiple_orders_detected,
            "requires_item_confirmation": requires_item_confirmation,
        })
        from learning_pipeline import forms_from_validated_order
        from learning_knowledge import matched_approved
        forms=forms_from_validated_order(result)
        result['reviewed_knowledge_hits']=matched_approved(forms,knowledge_principal)
        return result

    @staticmethod
    def _delivery_from_text(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if not _parse_address_record:
            return None, None, None
        try:
            record = _parse_address_record(text or "")
            address = str(getattr(record, "address", "") or "").strip()
            recipient = str(getattr(record, "name", "") or "").strip()
            phone = str(getattr(record, "phone", "") or "").strip()
            return address or None, recipient or None, phone or None
        except Exception:
            return None, None, None

    @staticmethod
    def _extract_address(message: str) -> Optional[str]:
        """提取地址"""
        patterns = [
            r'地址[:：]\s*([^\n]+)',
            r'收货地址[:：]\s*([^\n]+)',
            r'配送地址[:：]\s*([^\n]+)',
            r'所在地区[:：]\s*([^\n]+)',
            r'送达[:：]\s*([^\n]+)',
            r'地址\s*[:：]?\s*([\u4e00-\u9fa5a-zA-Z0-9\s\-#()（）.,，、]+?)(?=\n|$|收件人|收货人|姓名|电话|手机|联系电话|联系方式)',
        ]

        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                address = match.group(1).strip()
                # 过滤掉可能的误匹配
                if any(keyword in address for keyword in ['商品', '购买', '下单', '数量']):
                    continue
                return address

        # 如果没有找到明确的地址，尝试从第一行提取（针对简化格式）
        lines = message.split('\n')
        if lines:
            first_line = lines[0].strip()
            # 如果第一行包含"省"或"市"，可能是地址
            if '省' in first_line or '市' in first_line:
                return first_line

        # 如果没有找到明确的地址，尝试从收件人之后的内容中提取
        for i, line in enumerate(lines):
            if any(keyword in line for keyword in ['收件人', '收货人', '姓名', '电话', '手机', '联系电话', '所在地区']):
                # 检查下一行是否为地址
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if len(next_line) > 5 and not any(keyword in next_line.lower() for keyword in ['电话', '手机', '商品', '购买']):
                        return next_line

        return None

    @staticmethod
    def _extract_recipient(message: str) -> Optional[str]:
        """提取收件人"""
        patterns = [
            r'收件人[:：]\s*([^\n]+)',
            r'收货人[:：]\s*([^\n]+)',
            r'姓名[:：]\s*([^\n]+)',
            r'联系人[:：]\s*([^\n]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                return re.split(r'\s*(?:手机号码|手机号|电话|所在地区|详细地址|地址|商品)\s*[:：]', match.group(1))[0].strip()

        # 尝试从"姓名 电话"格式的行中提取
        # 例如: "essa 17757748821" 或 "张三 13800138000"
        lines = message.split('\n')
        for line in lines:
            line = line.strip()
            # 匹配：非数字开头 + 空格 + 手机号
            match = re.match(r'^([a-zA-Z\u4e00-\u9fa5]+)\s+(\d{11})', line)
            if match:
                return match.group(1).strip()
            # 匹配：姓名 + 空格/冒号 + 电话（非手机号格式）
            match = re.match(r'^([a-zA-Z\u4e00-\u9fa5]{2,12})[\s:：]+[\d\-+]+', line)
            if match:
                name = match.group(1).strip()
                # 排除一些商品名称
                if name not in [
                    '商品', '购买', '下单', '地址', '收货地址', '配送地址',
                    '电话', '手机', '联系电话', '联系方式', '号码',
                ]:
                    return name

        return None

    @staticmethod
    def _extract_phone(message: str) -> Optional[str]:
        """提取电话号码"""
        # 匹配模式：电话、手机、联系方式后接号码
        patterns = [
            r'(?:电话|手机号|手机号码|手机|联系电话|联系方式|号码)[:：]?\s*([\d\s\-+]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                phone = match.group(1).strip()
                # 清理电话号码
                phone = re.sub(r'[^\d\-+]', '', phone)
                if len(phone) >= 7:  # 最小电话号码长度
                    return phone

        # 如果没有明确的电话标记，尝试直接匹配数字
        phone_pattern = r'(?<!\d)1[3-9]\d{9}(?!\d)'  # 不从超长数字中截取有效手机号
        match = re.search(phone_pattern, message)
        if match:
            return match.group(0)

        # 匹配固话
        landline_pattern = r'(?<!\d)0\d{2,3}[-]?\d{7,8}(?!\d)'
        match = re.search(landline_pattern, message)
        if match:
            return match.group(0)

        return None

    @staticmethod
    def validate_order(order_info: Dict) -> Tuple[bool, str]:
        """验证订单信息完整性"""
        from contact_fields import with_generated_name
        order_info.update(with_generated_name(order_info))
        if order_info.get("multiple_orders_detected"):
            return False, (
                "⚠️ 检测到多个收货手机号，当前消息包含多条订单。\n\n"
                "请按“一个收货地址 + 一个收件人 + 一组商品”分开发送，"
                "机器人会分别生成确认单。"
            )

        if order_info.get("requires_item_confirmation"):
            return False, (
                "⚠️ 检测到“均配随机”或“每个口味”这类商品说明。\n\n"
                "请改为每个口味 + 数量，例如：\n"
                "西瓜冰20\n桃子冰20\n青柠冰20"
            )

        quantity_errors = [str(error) for error in order_info.get("quantity_scope_errors") or [] if str(error)]
        if quantity_errors:
            details=[str(name)+' ×'+str(quantity) for name,quantity in (order_info.get('items') or [])[:20]]
            if details:quantity_errors.append('识别明细（尚未录单）：'+'；'.join(details))
            return False, "⚠️ 订单数量需要确认：\n\n" + "\n".join(f"• {error}" for error in quantity_errors)

        if not order_info.get("items"):
            return False, "❌ 未找到商品信息\n\n请确保您的订单包含商品信息，例如：\n商品: 苹果 x2, 香蕉 x1\n或\n苹果 x2, 香蕉 x1"

        # 仅做基础结构校验；商品是否真实存在交给后端预览/匹配
        for product_name, quantity in order_info["items"]:
            if not product_name or not str(product_name).strip():
                return False, "❌ 商品名称无效\n\n请检查商品行是否为空"

            if quantity <= 0:
                return False, f"❌ 商品 '{product_name}' 数量无效\n\n数量必须大于0"

        diagnostics = diagnose_contact(order_info)
        if diagnostics['missing'] or diagnostics['invalid']:
            return False, contact_prompt(diagnostics)

        return True, "验证通过"


# 测试代码
if __name__ == "__main__":
    test_messages = [
        # 无仓库信息
        """
商品: 苹果 x2, 香蕉 x1
地址: 北京市朝阳区建国路88号
收件人: 张三
电话: 13800138000
        """,
        # 按仓库分组
        """
北京仓: 苹果 x2, 香蕉 x1
上海仓: 葡萄 x3
地址: 上海市浦东新区世纪大道100号
收件人: 李四
手机: 13912345678
        """,
        # 每个商品指定仓库
        """
苹果 x2 (北京仓), 香蕉 x1 (北京仓), 葡萄 x3 (上海仓)
地址: 广州市天河区xxx大厦
收件人: 王五
联系方式: 020-12345678
        """
    ]

    parser = OrderParser()
    for i, message in enumerate(test_messages, 1):
        print("=" * 50)
        print(f"测试用例 {i}:")
        print(f"内容: {message.strip()}")
        order_info = parser.extract_order_info(message)

        if order_info:
            print("✅ 解析成功:")
            print(f"  商品: {order_info['items']}")
            print(f"  地址: {order_info['address']}")
            print(f"  收件人: {order_info['recipient']}")
            print(f"  电话: {order_info['phone']}")

            valid, reason = parser.validate_order(order_info)
            print(f"  验证: {'✅ 通过' if valid else '❌ 失败'} - {reason}")
        else:
            print("❌ 解析失败")
        print()
