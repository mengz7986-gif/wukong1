"""Record successful inline confirmation delivery, not inferred receipt IDs."""
from datetime import datetime,timezone


def record(payload, markup, message_id):
    token=payload.get('_group_draft_token')
    if not isinstance(token,str) or not token or type(message_id) is not int or message_id<=0:
        return
    keyboard=markup.get('inline_keyboard',()) if isinstance(markup,dict) else getattr(markup,'inline_keyboard',())
    kinds=set()
    for row in keyboard or ():
        for button in row:
            data=button.get('callback_data') if isinstance(button,dict) else getattr(button,'callback_data',None)
            if not isinstance(data,str):continue
            parts=data.split(':')
            if parts==['name_generate',token]:kinds.add('name')
            elif parts==['address_confirm',token]:kinds.add('address')
            elif parts==['order_confirm',token]:kinds.add('order')
            elif len(parts)==3 and parts[0]=='ai_flavor' and parts[1].partition('~')[0].isdigit() and parts[2]==token:kinds.add('flavor')
    if len(kinds)==1:
        payload['_inline_confirmation']=dict(kind=next(iter(kinds)),token=token,message_id=message_id,
            active=True,delivery_acknowledged=True,updated_at=datetime.now(timezone.utc).isoformat())
    elif (payload.get('_inline_confirmation') or {}).get('message_id')==message_id:
        # Only an acknowledged replacement of this exact prompt can retire
        # its marker; an unrelated reply must not erase another live button.
        payload['_inline_confirmation']=dict(payload['_inline_confirmation'],active=False)


def proven_kind(payload,token):
    marker=payload.get('_inline_confirmation')
    if not isinstance(marker,dict) or marker.get('token')!=token or marker.get('active') is not True or marker.get('delivery_acknowledged') is not True:
        return None
    if type(marker.get('message_id')) is not int or marker['message_id']<=0:return None
    required={'name':'pending_recipient_order','address':'pending_recipient_order','order':'pending_order','flavor':'pending_ai_flavor'}
    kind=marker.get('kind')
    return kind if kind in required and isinstance(payload.get(required[kind]),dict) and payload[required[kind]] else None
