"""Exact bundle sale tokens precede ordinary merchandise parsing."""
import re
import unicodedata


def extract(text, catalog):
    """Returns sale requests and untouched remaining lines; no quantity guessing.

    Custom selection blocks are returned as original text for the existing
    category-scoped Alias/AI resolver, not interpreted as global merchandise.
    """
    names=[]
    for bundle in catalog:
        for name in [bundle['name']]+bundle.get('aliases',[]):
            names.append((unicodedata.normalize('NFKC',name),bundle))
    names.sort(key=lambda pair:len(pair[0]),reverse=True)
    requests=[];remaining=[];custom=None
    for original in str(text).splitlines():
        line=unicodedata.normalize('NFKC',original).strip()
        hit=None
        for name,bundle in names:
            match=re.fullmatch(re.escape(name)+r'\s*(?:(?:[×xX*]\s*)?(\d+)\s*(?:份|个|袋|包)?)?\s*[:：]?',line,re.I)
            if match:
                hit=(bundle,int(match.group(1) or 1));break
        if hit:
            bundle,n=hit
            if n<=0:raise ValueError('福袋份数必须大于0')
            request=dict(bundle_id=bundle['id'],quantity=n,selection_text=[])
            requests.append(request)
            custom=request if bundle['config']['mode']=='custom' else None
            continue
        if custom is not None and re.search(r'(?:[*×xX]\s*\d+|\d+\s*(?:支|根)?\s*$)',line):
            # Contact/address and independent amount lines can never be choices.
            if not re.search(r'地址|手机|电话|联系人|收件|\d{7}|[号栋楼室区市省]|^\d+$',line):
                custom['selection_text'].append(original);continue
        custom=None
        remaining.append(original)
    return requests,'\n'.join(remaining)
