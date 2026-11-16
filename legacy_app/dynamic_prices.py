"""Catalog-scoped dynamic offsets; persisted values remain unadjusted bases."""
from decimal import Decimal
SPECIAL_FLAVORS=frozenset(('蓝莓覆盆子','香芋冰淇淋','杨梅绿妍','伯牙绝弦','鳄梨哈密瓜','芦荟甘泉','酸梅芭乐','柠檬养乐多','桃子草莓'))
def offset(product):
    parent=getattr(product,'parent',None)
    return 2 if parent is not None and parent.name=='高维' and parent.is_parent and product.name in SPECIAL_FLAVORS and not product.is_parent else 0
def amount(base,extra):
    return float(Decimal(str(base or 0))+Decimal(str(extra)))
def effective(product,field):
    extra=offset(product)
    base=getattr(product.parent if extra else product,'_'+field)
    return amount(base,extra)
def custom(product,base):return amount(base,offset(product))
def effective_state(product):
    source=product.parent if offset(product) else product
    return source._reference_price_state
def price_property(field):
    from sqlalchemy.ext.hybrid import hybrid_property
    def read(self):return effective(self,field)
    def write(self,value):setattr(self,'_'+field,value)
    def expression(cls):return getattr(cls,'_'+field)
    return hybrid_property(read,write).expression(expression)
