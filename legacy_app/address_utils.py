# -*- coding: utf-8 -*-
"""中文收货地址标准化工具。

目标是推送第三方物流前，尽量把用户输入的半结构化地址拆成：
省 / 市 / 区县 / 镇街 / 详细地址。

说明：
- 不依赖外部接口，避免推单时因第三方地理服务不可用而失败。
- 有把握时自动补齐省市；无法判断时保留原地址并给出 warning，不瞎填。
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple


MUNICIPALITIES = {
    "北京": "北京市",
    "北京市": "北京市",
    "天津": "天津市",
    "天津市": "天津市",
    "上海": "上海市",
    "上海市": "上海市",
    "重庆": "重庆市",
    "重庆市": "重庆市",
}


PROVINCE_ALIASES = {
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省", "黑龙江": "黑龙江省",
    "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省", "福建": "福建省", "江西": "江西省",
    "山东": "山东省", "河南": "河南省", "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省",
    "海南": "海南省", "四川": "四川省", "贵州": "贵州省", "云南": "云南省", "陕西": "陕西省",
    "甘肃": "甘肃省", "青海": "青海省", "台湾": "台湾省",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区", "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区", "澳门": "澳门特别行政区",
}


# 轻量城市 -> 省份映射。覆盖常见发货/收货城市；未覆盖时仍按地址原文保留并提示。
PROVINCE_CITIES = {
    "广东省": "广州 深圳 佛山 东莞 惠州 珠海 中山 江门 汕头 潮州 揭阳 汕尾 湛江 茂名 阳江 肇庆 清远 韶关 河源 梅州 云浮",
    "广西壮族自治区": "南宁 柳州 桂林 梧州 北海 防城港 钦州 贵港 玉林 百色 贺州 河池 来宾 崇左",
    "海南省": "海口 三亚 三沙 儋州",
    "福建省": "福州 厦门 泉州 漳州 莆田 三明 南平 龙岩 宁德",
    "浙江省": "杭州 宁波 温州 嘉兴 湖州 绍兴 金华 衢州 舟山 台州 丽水",
    "江苏省": "南京 无锡 徐州 常州 苏州 南通 连云港 淮安 盐城 扬州 镇江 泰州 宿迁",
    "山东省": "济南 青岛 淄博 枣庄 东营 烟台 潍坊 济宁 泰安 威海 日照 临沂 德州 聊城 滨州 菏泽",
    "河南省": "郑州 开封 洛阳 平顶山 安阳 鹤壁 新乡 焦作 濮阳 许昌 漯河 三门峡 南阳 商丘 信阳 周口 驻马店 济源",
    "河北省": "石家庄 唐山 秦皇岛 邯郸 邢台 保定 张家口 承德 沧州 廊坊 衡水",
    "湖北省": "武汉 黄石 十堰 宜昌 襄阳 鄂州 荆门 孝感 荆州 黄冈 咸宁 随州 恩施",
    "湖南省": "长沙 株洲 湘潭 衡阳 邵阳 岳阳 常德 张家界 益阳 郴州 永州 怀化 娄底 湘西",
    "江西省": "南昌 景德镇 萍乡 九江 新余 鹰潭 赣州 吉安 宜春 抚州 上饶",
    "安徽省": "合肥 芜湖 蚌埠 淮南 马鞍山 淮北 铜陵 安庆 黄山 滁州 阜阳 宿州 六安 亳州 池州 宣城",
    "四川省": "成都 自贡 攀枝花 泸州 德阳 绵阳 广元 遂宁 内江 乐山 南充 眉山 宜宾 广安 达州 雅安 巴中 资阳 阿坝 甘孜 凉山",
    "重庆市": "重庆",
    "贵州省": "贵阳 六盘水 遵义 安顺 毕节 铜仁 黔西南 黔东南 黔南",
    "云南省": "昆明 曲靖 玉溪 保山 昭通 丽江 普洱 临沧 楚雄 红河 文山 西双版纳 大理 德宏 怒江 迪庆",
    "陕西省": "西安 铜川 宝鸡 咸阳 渭南 延安 汉中 榆林 安康 商洛",
    "山西省": "太原 大同 阳泉 长治 晋城 朔州 晋中 运城 忻州 临汾 吕梁",
    "辽宁省": "沈阳 大连 鞍山 抚顺 本溪 丹东 锦州 营口 阜新 辽阳 盘锦 铁岭 朝阳 葫芦岛",
    "吉林省": "长春 吉林 四平 辽源 通化 白山 松原 白城 延边",
    "黑龙江省": "哈尔滨 齐齐哈尔 鸡西 鹤岗 双鸭山 大庆 伊春 佳木斯 七台河 牡丹江 黑河 绥化 大兴安岭",
    "内蒙古自治区": "呼和浩特 包头 乌海 赤峰 通辽 鄂尔多斯 呼伦贝尔 巴彦淖尔 乌兰察布 兴安 锡林郭勒 阿拉善",
    "甘肃省": "兰州 嘉峪关 金昌 白银 天水 武威 张掖 平凉 酒泉 庆阳 定西 陇南 临夏 甘南",
    "青海省": "西宁 海东 海北 黄南 海南 果洛 玉树 海西",
    "宁夏回族自治区": "银川 石嘴山 吴忠 固原 中卫",
    "新疆维吾尔自治区": "乌鲁木齐 克拉玛依 吐鲁番 哈密 昌吉 博尔塔拉 巴音郭楞 阿克苏 克孜勒苏 喀什 和田 伊犁 塔城 阿勒泰",
}


CITY_TO_PROVINCE = {}
for province, cities_text in PROVINCE_CITIES.items():
    for city_name in cities_text.split():
        CITY_TO_PROVINCE[city_name] = province
        CITY_TO_PROVINCE[f"{city_name}市"] = province
for municipality in ("北京", "天津", "上海", "重庆"):
    CITY_TO_PROVINCE[municipality] = f"{municipality}市"
    CITY_TO_PROVINCE[f"{municipality}市"] = f"{municipality}市"


COMMON_DISTRICT_TO_CITY = {
    # 广州
    "越秀区": ("广东省", "广州市"), "海珠区": ("广东省", "广州市"), "荔湾区": ("广东省", "广州市"),
    "天河区": ("广东省", "广州市"), "白云区": ("广东省", "广州市"), "黄埔区": ("广东省", "广州市"),
    "番禺区": ("广东省", "广州市"), "花都区": ("广东省", "广州市"), "南沙区": ("广东省", "广州市"),
    "从化区": ("广东省", "广州市"), "增城区": ("广东省", "广州市"),
    # 深圳
    "福田区": ("广东省", "深圳市"), "罗湖区": ("广东省", "深圳市"), "南山区": ("广东省", "深圳市"),
    "宝安区": ("广东省", "深圳市"), "龙岗区": ("广东省", "深圳市"), "盐田区": ("广东省", "深圳市"),
    "龙华区": ("广东省", "深圳市"), "坪山区": ("广东省", "深圳市"), "光明区": ("广东省", "深圳市"),
    # 佛山 / 东莞 / 中山常用
    "禅城区": ("广东省", "佛山市"), "南海区": ("广东省", "佛山市"), "顺德区": ("广东省", "佛山市"),
    "三水区": ("广东省", "佛山市"), "高明区": ("广东省", "佛山市"),
    "东莞市": ("广东省", "东莞市"), "中山市": ("广东省", "中山市"),
}


KNOWN_STREETS = {
    "永平街道": ("广东省", "广州市", "白云区"),
}


def _clean_address(address: str) -> str:
    address = str(address or "").strip()
    address = re.sub(r"[\s,，;；]+", "", address)
    # Country and labelled postal metadata are not provincial prefixes or
    # delivery detail. Keep the untouched input in normalize_cn_address.raw.
    address = re.sub(r"^(?:中国大陆|中国内地|中华人民共和国)", "", address)
    address = re.sub(r"(?:邮政编码|邮编)[:：]?\d{6}(?!\d)", "", address)
    return address


def _take_prefix_by_candidates(text: str, candidates: List[str]) -> Tuple[str, str]:
    for candidate in sorted(candidates, key=len, reverse=True):
        if text.startswith(candidate):
            return candidate, text[len(candidate):]
    return "", text


def _extract_town(text: str) -> Tuple[str, str]:
    if not text:
        return "", text
    for street_name in sorted(KNOWN_STREETS.keys(), key=len, reverse=True):
        pos = text.find(street_name)
        if pos >= 0:
            return street_name, f"{text[:pos]}{text[pos + len(street_name):]}".strip()
    match = re.search(r"([\u4e00-\u9fa5]{2,12}(?:街道|镇|乡|民族乡|苏木|街))", text)
    if not match:
        return "", text
    town = match.group(1)
    start, end = match.span(1)
    return town, f"{text[:start]}{text[end:]}".strip()


def _address_memo_key(address):
    if not isinstance(address, str) or len(address) > 4096:
        return None
    try:
        from region_index import get_region_index
        # A changed index must never reuse an old administrative decision.
        return (get_region_index(), address)
    except Exception:
        return None


from request_memo import request_memoized


@request_memoized(_address_memo_key)
def normalize_cn_address(address: str) -> Dict[str, object]:
    raw = str(address or "").strip()
    text = _clean_address(raw)
    # Reorder an explicit county-before-province prefix only when the exact
    # full chain exists in the offline tree. Never discard an unknown prefix.
    administrative_migration = None
    try:
        from region_index import get_region_index
        index = get_region_index()
        if index:
            for district_name in sorted(index.region_by_name, key=len, reverse=True):
                if not text.startswith(district_name):continue
                following = text[len(district_name):]
                reordered=False
                for parent in index.district_parents(district_name):
                    prefix = parent[0] + parent[1]
                    if following.startswith(prefix):
                        text = prefix + district_name + following[len(prefix):]
                        reordered=True
                        break
                if reordered:break
            for municipality in ('北京','天津','上海','重庆'):
                text = re.sub(r'^(?:'+municipality+r'市?){2,}', municipality+'市', text)
            for row in index.admin_migrations:
                prefix = row['province'] + row['old_district'] + (row.get('town') or row.get('anchor') or '\0')
                if text.startswith(prefix):
                    text = row['province'] + row['district'] + text[len(row['province'] + row['old_district']):]
                    administrative_migration = row
                    break
    except Exception:
        # An unavailable snapshot cannot authorize a reordered/migrated chain.
        pass
    warnings: List[str] = []
    province = city = district = town = ""
    direct_admin_area = False
    rest = text

    if not text:
        return {
            "raw": raw, "province": "", "city": "", "district": "", "town": "",
            "detail": "", "normalized_address": "", "confidence": 0.0,
            "warnings": ["地址为空"],
        }

    # 1) 先识别省/自治区/直辖市。
    province_candidates = list(PROVINCE_ALIASES.values()) + list(PROVINCE_ALIASES.keys()) + list(MUNICIPALITIES.keys())
    province_hit, rest_after_province = _take_prefix_by_candidates(rest, province_candidates)
    # A city may share its stem with its province. The explicit city suffix
    # is stronger evidence than the shorter province abbreviation.
    explicit_city, _ = _take_prefix_by_candidates(rest, [c for c in CITY_TO_PROVINCE if c.endswith('市')])
    if explicit_city and province_hit and len(explicit_city) > len(province_hit):
        province_hit = ''
    if province_hit:
        province = MUNICIPALITIES.get(province_hit) or PROVINCE_ALIASES.get(province_hit) or province_hit
        # In a public place name such as ``北京大学``, the municipality
        # stem is both useful regional evidence and part of the POI name.  Keep
        # the full place span so optional map corroboration never queries the
        # unsafe generic word ``大学``.
        municipality_poi = (province_hit in MUNICIPALITIES and not province_hit.endswith('市')
                            and re.match(r'^(?:大学|学院|学校|医院|小区|花园|公寓|苑|酒店|宾馆)', rest_after_province))
        rest = text if municipality_poi else rest_after_province
        # Customers may append the generic "省" to a known autonomous-region
        # abbreviation. Consume that same-level suffix rather than retaining
        # it as delivery detail and producing "自治区省". Region identity is
        # unchanged; no city/district is inferred or overwritten here.
        if (province_hit in PROVINCE_ALIASES
                and province.endswith(('自治区', '特别行政区')) and rest.startswith('省')):
            rest = rest[1:]
        if province in ("北京市", "天津市", "上海市", "重庆市"):
            city = "市辖区"
            # Repeated municipality levels are syntax, not a district.
            while rest.startswith(province):
                rest = rest[len(province):]

    # 2) 识别城市，支持“广州市...”或“广州...”这种省份缺失的地址。
    if not city:
        city_candidates = list(CITY_TO_PROVINCE.keys())
        city_hit, rest_after_city = _take_prefix_by_candidates(rest, city_candidates)
        # A city stem inside a fully written county is not city evidence:
        # 朝阳区 must not become 辽宁省朝阳市 + 区....
        if city_hit and not city_hit.endswith(('市','县','旗','州','地区','盟')) and re.match(r'区|县|旗',rest_after_city):
            city_hit=''
        try:
            from region_index import get_region_index
            index=get_region_index()
            exact=index.explicit_city_prefix(province,rest) if index else ''
            if exact and len(exact)>len(city_hit or ''):
                city_hit,rest_after_city=exact,rest[len(exact):]
        except Exception:
            pass
        if city_hit and not city_hit.endswith(('市','县','旗','林区','州','地区','盟')) and re.match(r'路|街(?!道)|巷|弄|大道|公路',rest_after_city):
            city_hit=''
        # A complete prefecture name outranks a shorter city-stem alias.
        # Otherwise an autonomous prefecture may be rewritten as a city and
        # its remaining ethnic/suffix text incorrectly consumed as a district.
        explicit_prefecture = re.match(r'(.{2,12}?(?:自治州|州|地区|盟))', rest)
        if (explicit_prefecture and not (city_hit and city_hit.endswith('市'))
                and (not city_hit or len(explicit_prefecture.group(1)) > len(city_hit))):
            city = explicit_prefecture.group(1)
            rest = rest[explicit_prefecture.end():]
            province = province or CITY_TO_PROVINCE.get(city_hit, '')
        elif city_hit:
            # A county-level city may be pasted directly after the province
            # (for example ``吉林省延吉市`` or ``江苏省昆山市``).  It is the
            # district field, not a prefecture-level city.  Resolve this only
            # through the checked-in administrative tree; never guess it from
            # the suffix alone.
            county_chain = None
            try:
                from region_index import get_region_index
                index = get_region_index()
                nodes = index.region_by_name.get(city_hit, ()) if index else ()
                chains = [index._chains[node.id] for node in nodes
                          if index._same(province, index._chains[node.id].province)
                          and index._chains[node.id].district == city_hit]
                if len(set(chains)) == 1:
                    county_chain = chains[0]
            except Exception:
                county_chain = None
            if county_chain and county_chain.city and county_chain.city != city_hit:
                province = province or county_chain.province
                city = county_chain.city
                district = city_hit
                rest = rest_after_city
            else:
                city = city_hit if city_hit.endswith(("市", "州", "区", "盟", "县", "旗", "林区")) else f"{city_hit}市"
                province = province or CITY_TO_PROVINCE.get(city_hit, "")
                # ``武汉大学`` uses the city stem as part of a named POI.
                # Preserve that span while still using it as city evidence.
                if (not city_hit.endswith(("市", "州", "区", "盟"))
                        and re.match(r'^(?:大学|学院|学校|医院|小区|花园|公寓|苑|酒店|宾馆)', rest_after_city)):
                    rest = text
                else:
                    rest = rest_after_city
        else:
            # A name or stray punctuation is sometimes glued before the city
            # (for example ``江苏省李南京市...``).  Accept a known city within
            # the first two characters and discard only that prefix noise.
            for candidate in sorted(city_candidates, key=len, reverse=True):
                position = rest.find(candidate)
                if position not in (1, 2):
                    continue
                if not candidate.endswith(('市','州','区','盟')) and re.match(r'区|县|旗',rest[position+len(candidate):]):continue
                city = candidate if candidate.endswith(("市", "州", "区", "盟")) else f"{candidate}市"
                province = province or CITY_TO_PROVINCE.get(candidate, "")
                rest = rest[position + len(candidate):]
                break
    try:
        from region_index import get_region_index
        index=get_region_index()
        if index and city:
            province=province or index.city_scope_province(city)
            if index.is_direct_county_city_view(province,city):district=city
    except Exception:
        pass
    # Dongguan and Zhongshan directly administer towns/streets without a
    # county/district layer. The checked-in compatibility map already records
    # that relationship; use it as deterministic evidence when the broader
    # region snapshot does not expose the special administrative shape.
    if not district and city and COMMON_DISTRICT_TO_CITY.get(city)==(province,city):
        district=city
    if not city and province:
        city_match = re.match(r"(.{2,12}?(?:市|自治州|地区|盟))", rest)
        if city_match:
            city = city_match.group(1)
            rest = rest[city_match.end():]

    # 3) 识别区县。若用户只写了“白云区...”，对常用区县反推省市。
    district_match = None if district else re.match(r"(.{1,12}?(?:区|县|旗|市))", rest)
    if district_match and district_match.group(1).endswith('市'):
        candidate = district_match.group(1)
        try:
            from region_index import get_region_index
            index = get_region_index()
            nodes = index.region_by_name.get(candidate, ()) if index else ()
            proven = [index._chains[node.id] for node in nodes
                      if index._same(province, index._chains[node.id].province)
                      and index._same(city, index._chains[node.id].city)
                      and index._chains[node.id].district == candidate]
            if len(set(proven)) != 1:
                district_match = None
        except Exception:
            district_match = None
    if district_match:
        district = district_match.group(1)
        rest = rest[district_match.end():]
    elif not district:
        for district_name, (known_province, known_city) in sorted(COMMON_DISTRICT_TO_CITY.items(), key=lambda x: len(x[0]), reverse=True):
            pos = rest.find(district_name)
            if pos == 0:
                from region_index import get_region_index
                index=get_region_index()
                parents=index.district_parents(district_name,province,city) if index else []
                if len(parents)!=1:continue
                district = district_name
                province = province or parents[0][0]
                city = city or parents[0][1]
                rest = rest[len(district_name):]
                break

    # 4) 通过已知街道补齐上级省市区。
    for street_name, (known_province, known_city, known_district) in KNOWN_STREETS.items():
        if street_name in text:
            from region_index import get_region_index
            index=get_region_index()
            proven=index.resolve(text,province,city,district,street_name) if index else None
            if proven and not proven.note and proven.province and proven.city and proven.district:
                province=province or proven.province
                city=city or proven.city
                district=district or proven.district
            break

    # Remove only a repeated, already identified complete region prefix.
    # Never delete neighborhood/road/building text or shorten a town name.
    region_prefix = province + (city if city != '市辖区' else '') + district
    while region_prefix and rest.startswith(region_prefix):
        rest = rest[len(region_prefix):]
    # Prefer the offline tree's complete township name over a greedy regex.
    verified_town = ''
    try:
        from region_index import get_region_index
        index = get_region_index()
        if index:
            city=index.canonical_city_scope(province,city)
            match = index.resolve(text, province=province,
                                  city=province if city == '市辖区' else city,
                                  district=district)
            if match.street and match.street in rest:
                verified_town = match.street
    except Exception:
        pass
    if verified_town:
        town = verified_town
        detail = rest.replace(town, '', 1)
    else:
        town, detail = _extract_town(rest)
    detail = detail or rest

    # Complete a missing district from the checked-in administrative tree.
    # Ambiguous matches remain unresolved and are surfaced as a warning.
    try:
        from region_index import get_region_index

        region_index = get_region_index()
        if region_index is not None:
            resolution = region_index.resolve(
                text,
                province=province,
                city=province if city == '市辖区' else city,
                district=district,
                street=town,
            )
            province = (MUNICIPALITIES.get(resolution.province) or PROVINCE_ALIASES.get(resolution.province)
                        or resolution.province or province)
            city = resolution.city or city
            district = resolution.district or district
            town = resolution.street or town
            if resolution.district:
                for alias in (resolution.district, *region_index._short_region_names(resolution.district)):
                    if detail.startswith(alias):
                        detail = detail[len(alias):].strip()
                        break
            # Dongguan, Zhongshan and a few other prefecture-level cities
            # directly administer towns/streets. Their valid administrative
            # chain has no district/county node, but Fenghuodi still needs a
            # non-empty receiverArea. Use the verified direct-admin town as
            # that field and leave receiverTown empty to avoid duplication.
            if (
                not district
                and resolution.street
                and region_index.is_direct_admin_city(city)
            ):
                district = resolution.street
                town = ""
                direct_admin_area = True
            if resolution.street and resolution.street in detail:
                detail = detail.replace(resolution.street, "", 1).strip()
            elif resolution.street:
                # A unique suffixless township/street alias is source text,
                # not delivery detail. Remove it only after the offline tree
                # has resolved one unambiguous administrative chain.
                for alias in region_index._short_region_names(resolution.street):
                    if detail.startswith(alias):
                        detail = detail[len(alias):].strip()
                        break
            # The customer may omit 区/县. Remove that exact leading short
            # region token only after the offline tree uniquely resolved it.
            if resolution.district and province in MUNICIPALITIES.values():
                for alias in (resolution.district, *region_index._short_region_names(resolution.district)):
                    if detail.startswith(alias):
                        detail=detail[len(alias):]
                        break
            if resolution.note:
                warnings.append(resolution.note)
    except Exception:
        # Keep the lightweight parser usable if region data is unavailable.
        pass

    if city and not province:
        province = CITY_TO_PROVINCE.get(city) or CITY_TO_PROVINCE.get(city.rstrip("市"), "")
    if district and not (province and city):
        # Full offline evidence outranks the legacy common-district shortcut.
        # 白云区 has more than one parent and must remain unresolved.
        from region_index import get_region_index
        index=get_region_index()
        parents=index.district_parents(district,province,city) if index else []
        if len(parents)==1:
            province=province or parents[0][0]
            city=city or parents[0][1]

    if not province:
        warnings.append("未能自动识别省份")
    if not city:
        warnings.append("未能自动识别城市")
    if not district:
        warnings.append("未能自动识别区县")
    if not town and not direct_admin_area:
        warnings.append("未能自动识别镇/街道")

    # The town/street extractor removes the matched town from ``rest``.  Do
    # not append it again when the region index supplied the same value.
    # Provincial direct-admin counties use a city presentation view in the
    # existing adapter. Keep fields compatible, but never duplicate the same
    # county name in the delivery string.
    normalized = "".join([province, city if city not in ("市辖区",province) else "", district if district!=city else "", town, detail])
    score = 0.2 + (0.2 if province else 0) + (0.2 if city else 0) + (0.2 if district else 0) + (0.1 if town else 0) + (0.1 if detail else 0)
    legacy_conflict=False
    unverified_city_scope=False
    named_parent_conflict=False
    try:
        from region_index import get_region_index
        index=get_region_index()
        legacy_conflict=bool(index and index.has_legacy_region_conflict(province,city,district))
        unverified_city_scope=bool(index and index.strict_snapshot and province and city and not index.city_scope_known(province,city))
        named_parent_conflict=bool(index and index.named_parent_conflict(province,city,district,raw))
    except Exception:
        pass
    if legacy_conflict:
        warnings.append('原区县名称与当前行政区快照不一致，请核实当前区县；未自动替换旧地名')
    if unverified_city_scope:
        warnings.append('明确城市尚未在当前地址快照中完成核验，未使用同名街道改写城市')
    if named_parent_conflict:
        warnings.append('所填省市区与已核实政府公告的上级关系冲突，未自动覆盖原文')
    return {
        "raw": raw,
        "province": province,
        "city": city,
        "district": district,
        "town": town,
        "detail": detail,
        "normalized_address": normalized or raw,
        "confidence": round(min(score, 1.0), 2),
        "warnings": warnings,
        "legacy_region_conflict": legacy_conflict,
        "unverified_city_scope": unverified_city_scope,
        "named_parent_conflict": named_parent_conflict,
        "snapshot_id": getattr(locals().get('index'),'snapshot_id',''),
        "administrative_migration": administrative_migration,
    }


def split_cn_address(address: str) -> Dict[str, str]:
    parsed = normalize_cn_address(address)
    return {
        "province": str(parsed.get("province") or ""),
        "city": str(parsed.get("city") or ""),
        "district": str(parsed.get("district") or ""),
        "town": str(parsed.get("town") or ""),
        "detail": str(parsed.get("detail") or parsed.get("raw") or ""),
    }
