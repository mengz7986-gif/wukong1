"""Utilities for extracting Chinese delivery information from messy text.

The parser deliberately uses deterministic rules and a small built-in region
lexicon so it can run offline.  It is intended for spreadsheet cleanup, not
for legal address validation: rows that need review are marked in ``status``.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Iterable, List, Sequence

from region_index import get_region_index


# Longest names must be tried first (for example, "广西壮族自治区" before
# "广西").  Aliases without a suffix cover the abbreviated forms commonly
# found in pasted order data.
PROVINCE_ALIASES = [
    ("内蒙古自治区", "内蒙古自治区"),
    ("广西壮族自治区", "广西壮族自治区"),
    ("宁夏回族自治区", "宁夏回族自治区"),
    ("新疆维吾尔自治区", "新疆维吾尔自治区"),
    ("西藏自治区", "西藏自治区"),
    ("香港特别行政区", "香港特别行政区"),
    ("澳门特别行政区", "澳门特别行政区"),
    ("黑龙江省", "黑龙江省"),
    ("吉林省", "吉林省"),
    ("辽宁省", "辽宁省"),
    ("河北省", "河北省"),
    ("山西省", "山西省"),
    ("山东省", "山东省"),
    ("河南省", "河南省"),
    ("江苏省", "江苏省"),
    ("浙江省", "浙江省"),
    ("安徽省", "安徽省"),
    ("福建省", "福建省"),
    ("江西省", "江西省"),
    ("湖北省", "湖北省"),
    ("湖南省", "湖南省"),
    ("广东省", "广东省"),
    ("海南省", "海南省"),
    ("四川省", "四川省"),
    ("贵州省", "贵州省"),
    ("云南省", "云南省"),
    ("陕西省", "陕西省"),
    ("甘肃省", "甘肃省"),
    ("青海省", "青海省"),
    ("台湾省", "台湾省"),
    ("北京市", "北京市"),
    ("上海市", "上海市"),
    ("天津市", "天津市"),
    ("重庆市", "重庆市"),
    # common short forms
    ("内蒙古", "内蒙古自治区"),
    ("广西", "广西壮族自治区"),
    ("宁夏", "宁夏回族自治区"),
    ("新疆", "新疆维吾尔自治区"),
    ("西藏", "西藏自治区"),
    ("黑龙江", "黑龙江省"),
    ("吉林", "吉林省"),
    ("辽宁", "辽宁省"),
    ("河北", "河北省"),
    ("山西", "山西省"),
    ("山东", "山东省"),
    ("河南", "河南省"),
    ("江苏", "江苏省"),
    ("浙江", "浙江省"),
    ("安徽", "安徽省"),
    ("福建", "福建省"),
    ("江西", "江西省"),
    ("湖北", "湖北省"),
    ("湖南", "湖南省"),
    ("广东", "广东省"),
    ("海南", "海南省"),
    ("四川", "四川省"),
    ("贵州", "贵州省"),
    ("云南", "云南省"),
    ("陕西", "陕西省"),
    ("甘肃", "甘肃省"),
    ("青海", "青海省"),
    ("北京", "北京市"),
    ("上海", "上海市"),
    ("天津", "天津市"),
    ("重庆", "重庆市"),
]
PROVINCE_ALIASES.sort(key=lambda pair: len(pair[0]), reverse=True)
PROVINCE_NAMES = {canonical for _, canonical in PROVINCE_ALIASES}

MUNICIPALITIES = {"北京市", "上海市", "天津市", "重庆市"}

# A compact city lexicon handles addresses where the city suffix was omitted.
# Suffix-based matching below still handles cities not listed here.
CITY_TO_PROVINCE = {
    "广州": "广东省", "深圳": "广东省", "东莞": "广东省", "佛山": "广东省",
    "惠州": "广东省", "珠海": "广东省", "中山": "广东省", "汕头": "广东省",
    "江门": "广东省", "肇庆": "广东省", "湛江": "广东省", "茂名": "广东省",
    "清远": "广东省", "梅州": "广东省", "韶关": "广东省", "潮州": "广东省",
    "揭阳": "广东省", "云浮": "广东省", "石家庄": "河北省", "唐山": "河北省",
    "太原": "山西省", "呼和浩特": "内蒙古自治区", "沈阳": "辽宁省",
    "大连": "辽宁省", "长春": "吉林省", "哈尔滨": "黑龙江省", "南京": "江苏省",
    "苏州": "江苏省", "无锡": "江苏省", "常州": "江苏省", "徐州": "江苏省",
    "杭州": "浙江省", "宁波": "浙江省", "温州": "浙江省", "嘉兴": "浙江省",
    "合肥": "安徽省", "福州": "福建省", "厦门": "福建省", "南昌": "江西省",
    "济南": "山东省", "青岛": "山东省", "郑州": "河南省", "武汉": "湖北省",
    "长沙": "湖南省", "海口": "海南省", "成都": "四川省", "绵阳": "四川省",
    "贵阳": "贵州省", "昆明": "云南省", "西安": "陕西省", "兰州": "甘肃省",
    "西宁": "青海省", "银川": "宁夏回族自治区", "乌鲁木齐": "新疆维吾尔自治区",
    "拉萨": "西藏自治区", "香港": "香港特别行政区", "澳门": "澳门特别行政区",
}
CITY_ALIASES = sorted(CITY_TO_PROVINCE, key=len, reverse=True)

PHONE_RE = re.compile(r"(?<!\d)(1[3-9](?:[\s-]?\d){9})(?!\d)")
HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,20}")
ADDRESS_MARKERS = set("省市区县旗乡镇街道村社区路街巷号栋单元室楼层院组屯队开发园港道里弄")
SEPARATORS_RE = re.compile(r"[\t,，;；|]+")


@dataclass
class AddressRecord:
    raw: str
    name: str = ""
    phone: str = ""
    address: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    street: str = ""
    detail: str = ""
    status: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[ ]{2,}", " ", value)
    return value.strip()


def _strip_labels(value: str) -> str:
    value = re.sub(r"(?:收货地址|地址|收货人|姓名|名字|手机号码|手机号)\s*[:：]?", "", value, flags=re.I)
    value = re.sub(r"^[\s,，;；|/]+|[\s,，;；|/]+$", "", value)
    return value.strip()


def extract_phone(text: str) -> str:
    match = PHONE_RE.search(text)
    return re.sub(r"\D", "", match.group(1)) if match else ""


def _is_name_candidate(token: str) -> bool:
    token = token.strip()
    if not 2 <= len(token) <= 4 or not re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return False
    if any(marker in token for marker in ADDRESS_MARKERS):
        return False
    if token in PROVINCE_NAMES or token in CITY_ALIASES:
        return False
    return True


def extract_name(text: str, phone: str = "") -> str:
    """Find the most likely 2-4 Han-character name near the phone number."""
    without_phone = PHONE_RE.sub(" ", text)
    parts = [p.strip() for p in SEPARATORS_RE.split(without_phone) if p.strip()]
    candidates: list[tuple[int, int, str]] = []
    phone_pos = text.find(phone) if phone else -1

    # The most common glued form is ``姓名手机号地址`` or
    # ``手机号姓名地址``.  Inspect the small window around the phone first and
    # cut it at a known province marker so characters from the address cannot
    # leak into the name.
    if phone_pos >= 0:
        left = text[max(0, phone_pos - 24):phone_pos]
        right = text[phone_pos + len(phone):phone_pos + len(phone) + 24]
        for side, segment in (("left", left), ("right", right)):
            for alias, _ in PROVINCE_ALIASES:
                marker = segment.find(alias)
                if marker >= 0:
                    segment = segment[:marker] if side == "right" else segment[marker + len(alias):]
                    break
            runs = HAN_RUN_RE.findall(segment)
            if not runs:
                continue
            run = runs[-1] if side == "left" else runs[0]
            if _is_name_candidate(run):
                return run
            sizes = (4, 3, 2) if side == "left" else (2, 3, 4)
            for size in sizes:
                if len(run) < size:
                    continue
                token = run[-size:] if side == "left" else run[:size]
                if _is_name_candidate(token):
                    return token

    for part_index, part in enumerate(parts):
        if _is_name_candidate(part):
            distance = abs(text.find(part) - phone_pos) if phone_pos >= 0 else 100
            candidates.append((0 if phone_pos >= 0 and distance <= 12 else 1, distance, part))
        for match in HAN_RUN_RE.finditer(part):
            run = match.group(0)
            # Try short windows because a name can be glued to an address.
            for size in (3, 2, 4):
                for start in range(0, max(0, len(run) - size + 1)):
                    token = run[start:start + size]
                    if not _is_name_candidate(token):
                        continue
                    absolute = text.find(token)
                    distance = abs(absolute - phone_pos) if phone_pos >= 0 and absolute >= 0 else 100
                    # Prefer complete separator fields and strings immediately
                    # before/after a phone over substrings inside an address.
                    boundary_bonus = 0 if run == token else 1
                    candidates.append((boundary_bonus + (0 if distance <= 12 else 1), distance, token))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1], -len(item[2])))
    return candidates[0][2]


def _find_province(address: str) -> tuple[str, str]:
    for alias, canonical in PROVINCE_ALIASES:
        position = address.find(alias)
        if position >= 0 and position <= 3:
            return canonical, address[position + len(alias):]
    # If the province is not at the beginning, keep text before it in the
    # address but still use the match for administrative parsing.
    for alias, canonical in PROVINCE_ALIASES:
        position = address.find(alias)
        if position >= 0:
            return canonical, address[position + len(alias):]
    return "", address


def _find_city(remainder: str, province: str) -> tuple[str, str]:
    if province in MUNICIPALITIES:
        return province, remainder
    match = re.match(r"^(.{2,12}?(?:市|自治州|地区|盟))", remainder)
    if match:
        return match.group(1), remainder[len(match.group(1)):]
    for alias in CITY_ALIASES:
        position = remainder.find(alias)
        if position <= 2 and position >= 0:
            city = alias + ("市" if not alias.endswith(("市", "州")) else "")
            return city, remainder[position + len(alias):]
    return "", remainder


def _find_unit(remainder: str, suffixes: Sequence[str]) -> tuple[str, str]:
    suffix_group = "|".join(re.escape(suffix) for suffix in suffixes)
    match = re.match(rf"^(.{{1,16}}?(?:{suffix_group}))", remainder)
    if not match:
        return "", remainder
    value = match.group(1)
    return value, remainder[len(value):]


def split_address(address: str) -> tuple[str, str, str, str, str]:
    address = _strip_labels(normalize_text(address))
    province, remainder = _find_province(address)
    if not province:
        for alias in CITY_ALIASES:
            position = remainder.find(alias)
            if position in (0, 1, 2):
                province = CITY_TO_PROVINCE[alias]
                break
    city, remainder = _find_city(remainder, province)
    district, remainder = _find_unit(
        remainder,
        ("新区", "自治县", "开发区", "经济技术开发区", "区", "县", "旗"),
    )
    street, remainder = _find_unit(remainder, ("街道", "镇", "乡", "苏木"))
    detail = re.sub(r"^[\s,，;；|]+", "", remainder).strip()
    if not street:
        # A village/community is useful as a street-level fallback.
        street, detail = _find_unit(detail, ("社区", "村"))
    return province, city, district, street, detail


def complete_address_fields(
    address: str,
    province: str,
    city: str,
    district: str,
    street: str,
) -> tuple[str, str, str, str, str]:
    """Complete missing parent levels using the local administrative tree."""
    index = get_region_index()
    if index is None:
        return province, city, district, street, ""
    resolution = index.resolve(address, province, city, district, street)
    return resolution.province, resolution.city, resolution.district, resolution.street, resolution.note


def _address_from_parts(parts: Sequence[str], name: str, phone: str, original: str) -> str:
    useful: list[str] = []
    for part in parts:
        part = part.strip()
        if not part or (phone and phone in re.sub(r"\D", "", part)):
            continue
        if name and part == name:
            continue
        useful.append(part)
    address = "".join(useful) if len(useful) > 1 else (useful[0] if useful else original)
    address = PHONE_RE.sub("", address)
    if name:
        address = address.replace(name, "", 1)
    return _strip_labels(address)


def parse_record(raw: str) -> AddressRecord:
    original = normalize_text(raw)
    if not original:
        return AddressRecord(raw="", status="空行")
    phone = extract_phone(original)
    name = extract_name(original, phone)
    parts = [part.strip() for part in SEPARATORS_RE.split(original) if part.strip()]
    address = _address_from_parts(parts, name, phone, original)
    province, city, district, street, detail = split_address(address)
    province, city, district, street, completion_note = complete_address_fields(
        address, province, city, district, street
    )
    address_parts = [province]
    if city and city != province:
        address_parts.append(city)
    address_parts.extend([district, street, detail])
    address = "".join(part for part in address_parts if part) or address
    missing: list[str] = []
    if not name:
        missing.append("姓名")
    if not phone:
        missing.append("手机号")
    if not address:
        missing.append("地址")
    elif not (province or city or district):
        missing.append("地址层级")
    if completion_note:
        missing.append(completion_note)
    status = "完成" if not missing else "待核对：" + "、".join(missing)
    return AddressRecord(
        raw=original,
        name=name,
        phone=phone,
        address=address,
        province=province,
        city=city,
        district=district,
        street=street,
        detail=detail,
        status=status,
    )


def parse_text(text: str) -> List[AddressRecord]:
    records: list[AddressRecord] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Ignore the common spreadsheet header when it is pasted together
        # with the data.
        if "收货信息" in line and not extract_phone(line):
            continue
        records.append(parse_record(line))
    return records


def records_to_tsv(records: Iterable[AddressRecord]) -> str:
    fields = ["raw", "name", "phone", "address", "province", "city", "district", "street", "detail", "status"]
    rows = [fields]
    rows.extend([asdict(record).get(field, "") for field in fields] for record in records)
    return "\n".join("\t".join(str(cell).replace("\t", " ") for cell in row) for row in rows)


def parse_delimited_rows(rows: Iterable[Sequence[str]]) -> List[AddressRecord]:
    records: list[AddressRecord] = []
    for row in rows:
        values = [normalize_text(value) for value in row if normalize_text(value)]
        if not values:
            continue
        combined = "\t".join(values)
        if "收货信息" in combined and not extract_phone(combined):
            continue
        records.append(parse_record(combined))
    return records


def read_csv_like(path: str, encoding: str = "utf-8-sig") -> List[AddressRecord]:
    with open(path, "r", encoding=encoding, newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|") if sample.strip() else csv.excel
        except csv.Error:
            dialect = csv.excel
        return parse_delimited_rows(csv.reader(handle, dialect))
