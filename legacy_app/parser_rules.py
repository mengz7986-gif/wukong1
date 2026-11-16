"""Synchronizable, business-neutral order parsing rules.

This module intentionally has no database, Telegram, token, inventory, or
catalog imports.  Wholesale remains the rule reference, while each robot owns
its runtime adapters and business data.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Iterable, Optional, Sequence


SHARED_PARSER_RULESET_VERSION = "shared-parser-v1.2"
# Backwards-compatible name used by the dropshipping adapter.
PARSER_RULESET_VERSION = SHARED_PARSER_RULESET_VERSION

QUANTITY_UNITS = r"(?:根|跟|个|支|条|颗|盒|件)?"
ITEM_QUANTITY = "ITEM_QUANTITY"
SECTION_SUBTOTAL = "SECTION_SUBTOTAL"
ORDER_TOTAL = "ORDER_TOTAL"

_LIST_MARKER_RE = re.compile(r"^\s*(?:[•·●○▪▫◦‣⁃*]+|[-—–－]+)\s*")
_ORDER_TOTAL_RE = re.compile(
    rf"^(?:整单|一共|总共|总计|订单合计|订单总数|总数|总|数量)\s*[:：]?\s*(\d+)\s*{QUANTITY_UNITS}$"
)
_SCOPED_TOTAL_RE = re.compile(
    rf"^(?:小计|共|共计|合计)\s*[:：]?\s*(\d+)\s*{QUANTITY_UNITS}$"
)
_BARE_TOTAL_RE = re.compile(rf"^(\d+)\s*{QUANTITY_UNITS}$")
_ITEM_RE = re.compile(
    rf"^(.+?)(?:\s*[:：]\s*|\s*[xX×*]\s*|\s+)?(\d+)\s*{QUANTITY_UNITS}$"
)
_DELIVERY_HINT_RE = re.compile(
    r"(?:1[3-9]\d{9}|姓名|收件人|联系人|电话|手机|地址|省|市|区|县|街|路|号|栋|幢|单元|楼|室)"
)


@dataclass(frozen=True)
class QuantityFact:
    kind: str
    quantity: int
    line_number: int
    text: str
    section: Optional[str] = None
    item: Optional[str] = None
    section_index: Optional[int] = None


@dataclass(frozen=True)
class QuantityScopeResult:
    facts: tuple[QuantityFact, ...]
    declared_total: Optional[int]
    computed_total: int
    section_subtotals: tuple[tuple[str, int], ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "facts": [asdict(fact) for fact in self.facts],
            "declared_total": self.declared_total,
            "computed_total": self.computed_total,
            "section_subtotals": [
                {"section": section, "quantity": quantity}
                for section, quantity in self.section_subtotals
            ],
            "errors": list(self.errors),
        }


def normalize_order_text(value: object) -> str:
    """Apply the shared, conservative text normalization layer."""
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character for character in text
        if character in "\n\t" or unicodedata.category(character) not in {"Cf", "Cc"}
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_item_candidate(value: object) -> str:
    text = normalize_order_text(value)
    text = _LIST_MARKER_RE.sub("", text)
    text = text.strip(" \t,，、;；。.:：")
    return re.sub(r"\s+", " ", text).strip()


def _item_quantity(line: str) -> Optional[tuple[str, int]]:
    if re.fullmatch(r"\d+", line):
        return None
    match = _ITEM_RE.fullmatch(line)
    if not match:
        return None
    item = normalize_item_candidate(match.group(1))
    quantity = int(match.group(2))
    # A phone may share one line with a recipient name.  The numeric suffix is
    # still contact data, never an item quantity.
    if re.fullmatch(r"1[3-9]\d{9}", match.group(2)):
        return None
    if not item or quantity <= 0 or _DELIVERY_HINT_RE.search(item):
        return None
    # A total keyword is never an item name.
    if item in {"小计", "整单", "共", "共计", "一共", "合计", "总计", "总共", "总数", "数量"}:
        return None
    return item, quantity


def quantity_declaration(line: str, section: Optional[str] = None) -> Optional[tuple[str, int]]:
    """Classify declarations once, using the adapter's actual section context."""
    line = normalize_item_candidate(line)
    line = re.sub(r"备注.*$", "", line).strip()
    if re.fullmatch(r"1[3-9]\d{9}", line):
        return None
    match = _ORDER_TOTAL_RE.fullmatch(line)
    if match:
        return ORDER_TOTAL, int(match.group(1))
    match = _SCOPED_TOTAL_RE.fullmatch(line) or _BARE_TOTAL_RE.fullmatch(line)
    if match:
        return (SECTION_SUBTOTAL if section else ORDER_TOTAL), int(match.group(1))
    return None


def validate_quantity_facts(
    facts: Iterable[QuantityFact],
    parsed_items: Optional[Iterable[Sequence[object]]] = None,
) -> QuantityScopeResult:
    """The only quantity validator, shared by structured and text adapters.

    Structured adapters supply catalog-resolved item quantities, contact-free
    declarations and unique section indexes. No product matching happens here.
    """
    facts = tuple(facts)
    errors = []
    totals = [fact.quantity for fact in facts if fact.kind == ORDER_TOTAL]
    section_totals = {}
    subtotals = []
    # A subtotal may be declared in a heading before its item details. Sum
    # the entire section first; never count rows or only preceding rows.
    for fact in facts:
        key = (fact.section_index, fact.section)
        if fact.kind == ITEM_QUANTITY:
            section_totals[key] = section_totals.get(key, 0) + fact.quantity
    for fact in facts:
        key = (fact.section_index, fact.section)
        if fact.kind == SECTION_SUBTOTAL:
            subtotals.append((fact.section or "未分类", fact.quantity))
            actual = section_totals.get(key, 0)
            if actual != fact.quantity:
                errors.append(f"分段小计不一致：{fact.section or '未分类'}填写 {fact.quantity}，商品实际合计 {actual}。")
    if len(set(totals)) > 1:
        errors.append("订单总数存在多个不同填写值。")
    declared = totals[0] if totals else None
    computed = (sum(int(item[1]) for item in parsed_items if len(item) >= 2)
                if parsed_items is not None else sum(f.quantity for f in facts if f.kind == ITEM_QUANTITY))
    # Validate the final merchandise result, not just the source facts against
    # one another. When every explicit source section has one matching
    # subtotal, losing an item downstream must not produce a partial sale.
    subtotal_keys=[(f.section_index,f.section) for f in facts if f.kind==SECTION_SUBTOTAL]
    item_keys={(f.section_index,f.section) for f in facts if f.kind==ITEM_QUANTITY}
    if (parsed_items is not None and item_keys and item_keys==set(subtotal_keys)
            and len(subtotal_keys)==len(set(subtotal_keys))
            and all(section_totals.get((f.section_index,f.section),0)==f.quantity
                    for f in facts if f.kind==SECTION_SUBTOTAL)):
        expected=sum(section_totals.values())
        if expected!=computed:
            errors.append(f"商品明细解析数量不一致：原文合计 {expected}，商品实际合计 {computed}。")
    if declared is not None and declared != computed:
        errors.append(f"订单总数不一致：客户填写 {declared}，商品实际合计 {computed}。")
    return QuantityScopeResult(facts, declared, computed, tuple(subtotals), tuple(dict.fromkeys(errors)))


def _section_heading(line: str, next_line: str) -> bool:
    if not line or _DELIVERY_HINT_RE.search(line):
        return False
    if _ORDER_TOTAL_RE.fullmatch(line) or _SCOPED_TOTAL_RE.fullmatch(line):
        return False
    if _item_quantity(line):
        return False
    if _item_quantity(next_line):
        return True
    # The structure adapter can conserve a comma-separated flavour list as
    # one line (``青提1支,荔枝1支(``).  It is still evidence that the
    # preceding opaque token is a section heading; requiring the entire next
    # line to be one item caused the previous section's ``一共两支`` to be
    # misclassified as the whole-order total.
    if _DELIVERY_HINT_RE.search(next_line) or not re.search(r'[,，]',next_line):
        return False
    return len(re.findall(r'[^,，\s]+?\s*(?:[xX×*]\s*)?\d+\s*(?:枝|只|支|根|个|件|盒)?(?=$|[,，])', next_line)) >= 1


def analyze_quantity_scopes(
    text: object,
    parsed_items: Optional[Iterable[Sequence[object]]] = None,
    *,
    ignore_leading_bare_capacity: bool = False,
) -> QuantityScopeResult:
    """Classify item quantities, section subtotals, and the order total.

    ``共X``/``合计X`` inside a product section is a section subtotal.  Only
    explicit global wording such as ``一共X`` or ``总计X`` claims the order total.
    This prevents multiple section totals from occupying ``declared_total``.
    """
    lines = [normalize_item_candidate(line) for line in normalize_order_text(text).splitlines()]
    lines = [line for line in lines if line]
    facts: list[QuantityFact] = []
    section = ""
    section_index = None

    for index, line in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        after_next = lines[index + 2] if index + 2 < len(lines) else ""
        explicit = re.fullmatch(r'(?:品类|型号)[:：]\s*(.+)',line)
        if explicit:
            section=explicit.group(1)
            section_index=index+1
            continue
        if _section_heading(line, next_line):
            section = line.rstrip(":： ")
            section_index = index + 1
            continue
        declaration = quantity_declaration(line, section or None)
        if declaration:
            kind, quantity = declaration
            # Ambiguous global wording immediately following the current
            # section is a subtotal when another catalog-style section follows.
            # Explicit order labels remain global, and a terminal total after
            # all sections remains ORDER_TOTAL. The validator stays unchanged.
            if (kind == ORDER_TOTAL and section
                    and re.match(r'^(?:一共|总共|总计)\s*[:：]?\s*\d',line)
                    and _section_heading(next_line,after_next)):
                kind=SECTION_SUBTOTAL
            if (kind == SECTION_SUBTOTAL and index == len(lines)-1
                    and len({f.section_index for f in facts if f.kind == ITEM_QUANTITY}) > 1
                    and quantity == sum(f.quantity for f in facts if f.kind == ITEM_QUANTITY)):
                kind=ORDER_TOTAL
            # Dropshipping legacy messages may place model capacity (110跟)
            # before the first flavor. This adapter policy is opt-in; the
            # structured wholesale validator never applies this heuristic.
            if (ignore_leading_bare_capacity and section and _BARE_TOTAL_RE.fullmatch(line)
                    and not any(f.kind == ITEM_QUANTITY and f.section_index == section_index for f in facts)):
                continue
            facts.append(QuantityFact(kind, quantity, index + 1, line,
                                      section=section if kind == SECTION_SUBTOTAL else None,
                                      section_index=section_index if kind == SECTION_SUBTOTAL else None))
            continue
        item = _item_quantity(line)
        if item:
            name, quantity = item
            facts.append(QuantityFact(ITEM_QUANTITY, quantity, index + 1, line,
                                      section=section or None, item=name, section_index=section_index))
            continue
        # The structural adapter may keep several explicit flavours on one
        # comma-separated line.  Conserve each quantity fact instead of
        # dropping the entire line from source-total validation.
        if not _DELIVERY_HINT_RE.search(line) and re.search(r'[,，]', line):
            candidates=[]
            for part in re.split(r'[,，]', line):
                parsed=_item_quantity(normalize_item_candidate(part).rstrip('()（）'))
                if parsed:
                    candidates.append(parsed)
            if len(candidates) >= 2:
                for name,quantity in candidates:
                    facts.append(QuantityFact(ITEM_QUANTITY,quantity,index+1,line,
                                              section=section or None,item=name,section_index=section_index))
    return validate_quantity_facts(facts, parsed_items)
