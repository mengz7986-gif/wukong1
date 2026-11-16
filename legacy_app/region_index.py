"""Offline administrative-region index used for address completion."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass,replace
from functools import lru_cache
from threading import Lock
from pathlib import Path
from typing import Iterable
from request_memo import request_memoized


STREET_SUFFIXES = ("街道", "地区", "镇", "乡", "苏木")
IGNORED_NAMES = {"其它区", "其他区", "海外"}
SNAPSHOT_FILES=['province.json','city.json','region.json','pcas.json','legacy_regions.json','verified_name_supplements.json',
                'protected_custom/province.json','protected_custom/city.json','protected_custom/region.json','protected_custom/pcas.json',
                'verified_delivery_zones.json','verified_admin_migrations.json']


@dataclass(frozen=True)
class RegionNode:
    id: str
    name: str
    pid: str
    zipcode: str = ""


@dataclass(frozen=True)
class RegionResolution:
    province: str = ""
    city: str = ""
    district: str = ""
    street: str = ""
    note: str = ""


class RegionIndex:
    """Name and parent indexes loaded from the checked-in JSON data."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir) if data_dir else Path(__file__).with_name("data")
        self.data_root=root
        source_files=SNAPSHOT_FILES
        self.snapshot_id=hashlib.sha256(b''.join(name.encode()+b'\0'+(root/name).read_bytes()
            for name in source_files if (root/name).exists())).hexdigest()
        zones=root/'verified_delivery_zones.json'
        self.delivery_zones=json.loads(zones.read_text()) if zones.exists() else []
        migrations=root/'verified_admin_migrations.json'
        self.admin_migrations=json.loads(migrations.read_text()) if migrations.exists() else []

        self.provinces = self._load(root / "province.json")
        self.cities = self._load(root / "city.json")
        self.regions = self._load(root / "region.json")
        legacy_path=root/'legacy_regions.json'
        legacy=json.loads(legacy_path.read_text()) if legacy_path.exists() else []
        self.legacy_region_conflicts=frozenset((str(row['province']).rstrip('省市'),str(row['city']).rstrip('市'),str(row['district'])) for row in legacy)
        self.strict_snapshot=legacy_path.exists()
        named_path=root/'verified_name_supplements.json'
        self.verified_named_regions=json.loads(named_path.read_text()) if named_path.exists() else []
        protected=root/'protected_custom'
        self.protected_custom_index=RegionIndex(protected) if protected.is_dir() else None
        self.province_by_name = self._by_name(self.provinces.values())
        self.city_by_name = self._by_name(self.cities.values())
        self.region_by_name = self._by_name(self.regions.values())
        self.city_ids = set(self.cities)
        self.region_ids = set(self.regions)
        self._chains = {node.id: self._make_chain(node) for node in self.regions.values()}
        self.street_by_name = self._load_street_tree(root / "pcas.json")
        # Some prefecture-level cities (for example Dongguan and Zhongshan)
        # administer towns/streets directly and have no district/county node.
        # Keep this derived from the checked-in tree so the address parser can
        # distinguish a valid direct-admin chain from an unresolved address.
        self.direct_admin_cities = frozenset(
            chain.city
            for chains in self.street_by_name.values()
            for chain in chains
            if chain.city and not chain.district
        )
        self._name_trie = self._build_name_trie(set(self.region_by_name) | set(self.street_by_name))
        # Administrative data is immutable for this index instance. Build the
        # suffixless lookup once, instead of scanning every region per address.
        self._alias_names: dict[str, list[str]] = {}
        for name in self.region_by_name:
            for alias in self._short_region_names(name):
                self._alias_names.setdefault(alias, []).append(name)
        self._alias_trie = self._build_name_trie(set(self._alias_names))
        manifest=root/'offline_manifest.json'
        if data_dir is None and not manifest.exists():raise ValueError('OFFLINE_REGION_MANIFEST_MISSING')
        if manifest.exists():
            expected=json.loads(manifest.read_text(encoding='utf-8'))
            actual=self.inventory()
            if expected.get('format')!=1 or any(expected.get(k)!=v for k,v in actual.items()):
                raise ValueError('OFFLINE_REGION_SNAPSHOT_MISMATCH')
            if actual['broken_city_parents'] or actual['broken_region_parents']:
                raise ValueError('OFFLINE_REGION_PARENT_INTEGRITY_FAILED')

    @staticmethod
    def _load(path: Path) -> dict[str, RegionNode]:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        result: dict[str, RegionNode] = {}
        for key, value in raw.items():
            node_id = str(value.get("id", key))
            result[node_id] = RegionNode(
                id=node_id,
                name=str(value.get("name", "")),
                pid=str(value.get("pid", "0")),
                zipcode=str(value.get("zipcode", "")),
            )
        return result

    def has_legacy_region_conflict(self,province,city,district):
        return (str(province or '').rstrip('省市'),str(city or '').rstrip('市'),str(district or '')) in self.legacy_region_conflicts

    def district_parents(self,district,province='',city=''):
        """Every exact county parent, not a ranked popular-location guess."""
        return sorted({(chain.province,chain.city,chain.district)
            for node in self.region_by_name.get(district,()) for chain in [self._chains[node.id]]
            if chain.district==district and self._same(province,chain.province)
            and self._same(city,chain.city)})

    def inventory(self):
        return dict(snapshot_id=self.snapshot_id,provinces=len(self.provinces),cities=len(self.cities),
                    regions=len(self.regions),street_names=len(self.street_by_name),
                    broken_city_parents=sum(n.pid not in self.provinces for n in self.cities.values()),
                    broken_region_parents=sum(n.pid not in self.cities and n.pid not in self.regions for n in self.regions.values()))

    def canonical_city_scope(self,province,city):
        """Expand a uniquely scoped prefecture abbreviation; preserve unknown cities."""
        if not city:return ''
        if city in self.city_by_name:return city
        if city.endswith('市'):
            chains={self._chains[node.id] for node in self.region_by_name.get(city,())
                    if self._chains[node.id].district==city
                    and self._same(province,self._chains[node.id].province)}
            if len(chains)==1:return next(iter(chains)).city
        if city.endswith('州') and not city.endswith('自治州') and len(city[:-1])>=2:
            matches={node.name for node in self.cities.values()
                     if node.name.startswith(city[:-1]) and node.name.endswith('自治州')
                     and self._same(province,self.provinces[node.pid].name)}
            if len(matches)==1:return next(iter(matches))
        return city

    def city_scope_known(self,province,city):
        if any(row.get('direct_county_city_view') and row['city']==city and self._same(province,row['province']) for row in self.verified_named_regions):return True
        if city in self.city_by_name:
            return any(self._same(province,self.provinces[node.pid].name) for node in self.city_by_name[city])
        if self.protected_custom_index and province:
            if any(self._same(province,name) for name in self.protected_custom_index.province_by_name):
                return self.protected_custom_index.city_scope_known(province,city)
        return False

    def explicit_city_prefix(self,province,text):
        names={node.name for node in self.cities.values() if self._same(province,self.provinces[node.pid].name)}
        names.update(row['city'] for row in self.verified_named_regions if row.get('direct_county_city_view') and self._same(province,row['province']))
        if self.protected_custom_index:
            names.update(node.name for node in self.protected_custom_index.cities.values() if self._same(province,self.protected_custom_index.provinces[node.pid].name))
        return next((name for name in sorted(names,key=len,reverse=True) if text.startswith(name)),'')

    def city_scope_province(self,city):
        values={self.provinces[node.pid].name for node in self.city_by_name.get(city,())}
        values.update(row['province'] for row in self.verified_named_regions if row.get('direct_county_city_view') and row['city']==city)
        if self.protected_custom_index:
            values.update(self.protected_custom_index.provinces[node.pid].name for node in self.protected_custom_index.city_by_name.get(city,()))
        return next(iter(values)) if len(values)==1 else ''

    def is_direct_county_city_view(self,province,city):
        if any(row.get('direct_county_city_view') and row['city']==city and self._same(province,row['province']) for row in self.verified_named_regions):return True
        return any(node.id.isdigit() and not node.id.endswith('00') and self._same(province,self.provinces[node.pid].name) for node in self.city_by_name.get(city,()))

    def named_parent_conflict(self,province,city,district,text=''):
        rows=[row for row in self.verified_named_regions
              if row['district']==district or (row['name'] and row['name'] in str(text or ''))]
        return bool(rows and not any(self._same(province,row['province']) and self._same(city,row['city']) for row in rows))

    @staticmethod
    def _by_name(nodes: Iterable[RegionNode]) -> dict[str, list[RegionNode]]:
        result: dict[str, list[RegionNode]] = {}
        for node in nodes:
            if node.name and node.name not in IGNORED_NAMES:
                result.setdefault(node.name, []).append(node)
        return result

    @staticmethod
    def _load_street_tree(path: Path) -> dict[str, list[RegionResolution]]:
        """Load the complete township/street tree when ``pcas.json`` exists."""
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        result: dict[str, list[RegionResolution]] = {}
        for province, cities in raw.items():
            for city, districts in cities.items():
                # The source uses "市辖区" below municipalities; expose the
                # actual municipality name to callers instead.
                normalized_city = province if city == "市辖区" and province.endswith("市") else city
                for district, streets in districts.items():
                    normalized_district = "" if district == normalized_city else district
                    for street in streets:
                        if not street or street in IGNORED_NAMES:
                            continue
                        chain = RegionResolution(
                            province=str(province),
                            city=str(normalized_city),
                            district=str(normalized_district),
                            street=str(street),
                        )
                        result.setdefault(street, []).append(chain)
                        # Customers commonly omit the administrative suffix
                        # (for example "来广营" for "来广营地区"). Keep the
                        # alias tied to the same checked-in parent chain;
                        # resolve() still refuses ambiguous aliases.
                        if street.endswith("地区"):
                            for alias in RegionIndex._short_region_names(street):
                                result.setdefault(alias, []).append(chain)
        return result

    @staticmethod
    def _build_name_trie(names: set[str]) -> dict:
        root: dict = {}
        for name in names:
            if len(name) < 2:
                continue
            node = root
            for char in name:
                node = node.setdefault(char, {})
            node.setdefault("\0", []).append(name)
        return root

    def _matched_names(self, text: str) -> list[str]:
        return self._matched_in_trie(text,self._name_trie)

    @staticmethod
    def _matched_in_trie(text: str, trie: dict) -> list[str]:
        matches: set[str] = set()
        for start in range(len(text)):
            node = trie
            for char in text[start:]:
                next_node = node.get(char)
                if next_node is None:
                    break
                node = next_node
                matches.update(node.get("\0", ()))
        return sorted(matches, key=len, reverse=True)

    @staticmethod
    def _short_region_names(name: str) -> tuple[str, ...]:
        """Return common suffixless aliases used in pasted addresses."""
        value = str(name or "").strip()
        suffixes = (
            "特别行政区", "自治县", "新区", "开发区", "经济技术开发区",
            "自治区", "地区", "区", "县", "旗", "市",
        )
        aliases = []
        for suffix in suffixes:
            if value.endswith(suffix) and len(value) > len(suffix):
                aliases.append(value[:-len(suffix)])
        return tuple(alias for alias in aliases if len(alias) >= 2)

    def _make_chain(self, node: RegionNode) -> RegionResolution:
        current = node
        district = ""
        street = ""
        # A region whose parent is a city is district-level. Deeper records
        # are towns/streets and their nearest district is found by walking up.
        if current.name.endswith(STREET_SUFFIXES):
            street = current.name
        elif current.pid in self.city_ids:
            district = current.name
        seen: set[str] = set()
        while current.id not in seen:
            seen.add(current.id)
            if current.pid in self.city_ids:
                city_node = self.cities.get(current.pid)
                if city_node:
                    province_node = self.provinces.get(city_node.pid)
                    return RegionResolution(
                        province=province_node.name if province_node else "",
                        city=city_node.name,
                        district=district,
                        street=street,
                    )
                break
            parent = self.regions.get(current.pid)
            if parent is None:
                break
            if not district and parent.pid in self.city_ids:
                district = parent.name
            current = parent
        return RegionResolution(district=district, street=street)

    def _candidate_nodes(self, text: str) -> list[tuple[RegionNode | None, RegionResolution, int]]:
        candidates: list[tuple[RegionNode | None, RegionResolution, int]] = []
        # Long names first avoids giving a short substring more weight than a
        # complete administrative name.
        names = self._matched_names(text)
        for name in names:
            for node in self.region_by_name.get(name, ()):
                chain = self._chains[node.id]
                # Prefer explicit street/town matches over district-only hits.
                level_score = 30 if name.endswith(STREET_SUFFIXES) else 20
                candidates.append((node, chain, len(name) * 10 + level_score))
        # The pcas source contains all 41k township/street records, including
        # names that are absent from the older addr_go region map.
        for name in names:
            for chain in self.street_by_name.get(name, ()):
                candidates.append((None, chain, len(name) * 10 + 40))

        # Customers often omit the 区/县 suffix (for example 闽侯时代广场).
        # Keep these candidates below exact administrative-name matches.
        text = str(text or "")
        exact_names = set(names)
        # A suffixless county/district alias cannot reuse the characters of
        # an already explicit province/city/longer administrative name. For
        # example a city's stem is not a second independently stated county.
        # Keep a separate occurrence outside these spans eligible so valid
        # suffixless districts after an explicit city still work.
        occupied=[]
        for label in set(self.province_by_name) | set(self.city_by_name) | exact_names:
            if label not in text:
                continue
            occupied.extend((m.start(),m.end(),len(label))
                            for m in re.finditer(re.escape(label),text))
        for alias in self._matched_in_trie(text,self._alias_trie):
            if alias in exact_names:
                continue
            occurrences=list(re.finditer(re.escape(alias),text))
            eligible=[m for m in occurrences
                      if not any(start<=m.start() and m.end()<=end and length>len(alias)
                                 for start,end,length in occupied)
                      and not re.match(r'(?:路|街(?!道)|巷|弄|大道|公路|大桥|小区|花园|公寓|庄|村|屯|社区|[东西南北]里)',text[m.end():])]
            if not eligible:
                continue
            for name in self._alias_names[alias]:
                nodes=self.region_by_name[name]
                for node in nodes:
                    candidates.append((node, self._chains[node.id], len(alias) * 10 + 8))
        return candidates

    @staticmethod
    def _same(expected: str, actual: str) -> bool:
        # province.json uses e.g. 北京, while pcas.json uses 北京市.
        # These are formatting aliases of the same administrative node.
        return not expected or not actual or expected == actual or expected.rstrip('省市') == actual.rstrip('省市')

    def is_direct_admin_city(self, city: str) -> bool:
        """Whether ``city`` directly administers the indexed town/street."""
        value = str(city or "").strip()
        if not value:
            return False
        return value in self.direct_admin_cities or f"{value}市" in self.direct_admin_cities

    @request_memoized(lambda self,text,province='',city='',district='',street='':
        (self,text,province,city,district,street) if isinstance(text,str) and len(text)<=4096 else None)
    def resolve(self, text: str, province: str = "", city: str = "", district: str = "", street: str = "") -> RegionResolution:
        named=[row for row in self.verified_named_regions if row['name'] in text and self._same(province,row['province']) and self._same(city,row['city']) and (not district or district==row['district'])]
        if len(named)==1:
            row=named[0]
            return RegionResolution(province=row['province'],city=row['city'],district=row['district'],street=street,
                note='2026年政府公告名称及上级关系已核实；未冒充行政编码或镇街细分已核实')
        if province and self.protected_custom_index:
            known=self.protected_custom_index.province_by_name
            if any(self._same(province,name) for name in known):
                resolution=self.protected_custom_index.resolve(text,province,city,district,street)
                return replace(resolution,note='使用明确地域下的原有港澳台兼容资料；未认证为最新官方数据')
        candidates = self._candidate_nodes(text)
        if not candidates:
            return RegionResolution(province=province, city=city, district=district, street=street)

        filtered: list[tuple[RegionNode, RegionResolution, int]] = []
        for node, chain, score in candidates:
            if not self._same(province, chain.province):
                continue
            if not self._same(city, chain.city):
                continue
            if district and chain.district and district != chain.district:
                continue
            if street and chain.street and street != chain.street:
                continue
            context_hits = sum(bool(value and value in text) for value in (chain.province, chain.city, chain.district, chain.street))
            filtered.append((node, chain, score + context_hits * 100))

        if not filtered:
            return RegionResolution(province=province, city=city, district=district, street=street)

        filtered.sort(key=lambda item: item[2], reverse=True)
        best_score = filtered[0][2]
        best = [item for item in filtered if item[2] == best_score]
        # Deduplicate candidates that describe the same parent chain.
        chains: list[RegionResolution] = []
        for _, chain, _ in best:
            if chain not in chains:
                chains.append(chain)

        if len(chains) > 1:
            labels = sorted({" ".join(value for value in (item.province, item.city, item.district, item.street) if value) for item in chains})
            note = "行政区重名，候选：" + " / ".join(labels[:4])
            return RegionResolution(province=province, city=city, district=district, street=street, note=note)

        selected = best[0][1]
        return RegionResolution(
            province=selected.province or province,
            city=selected.city or city,
            district=selected.district or district,
            street=selected.street or street,
        )


_region_index_lock=Lock()


@lru_cache(maxsize=1)
def _cached_region_index() -> RegionIndex | None:
    try:index = RegionIndex()
    except (OSError,ValueError,KeyError,TypeError):return None
    if not index.provinces or not index.cities or not index.regions:
        return None
    return index


def get_region_index() -> RegionIndex | None:
    # functools.lru_cache may execute a cold factory in multiple threads.
    # Protect the cache lookup as well as construction; only one immutable
    # region snapshot is built per process, including its protected child.
    with _region_index_lock:
        return _cached_region_index()


def _clear_region_index():
    with _region_index_lock:
        _cached_region_index.cache_clear()


# Preserve the original explicit invalidation and cache inspection interface.
get_region_index.cache_clear=_clear_region_index
get_region_index.cache_info=_cached_region_index.cache_info
get_region_index.cache_parameters=_cached_region_index.cache_parameters
get_region_index.__wrapped__=_cached_region_index.__wrapped__
