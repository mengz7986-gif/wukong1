"""Renaming is an ID-preserving catalog edit, never a historical fact rewrite."""
import unicodedata


def record_rename(session,product,new_name,actor):
    if (not isinstance(new_name,str) or not 1<=len(new_name.strip())<=100
            or any(unicodedata.category(ch).startswith('C') for ch in new_name)
            or any(ch in '<>' for ch in new_name)):
        raise ValueError('名称须为1至100个可显示字符，不能包含控制字符')
    new_name=new_name.strip()
    if product.name==new_name:return
    from models import CatalogRenameAudit
    session.add(CatalogRenameAudit(product_id=product.id,before_name=product.name,after_name=new_name,actor=str(actor)[:100]))
