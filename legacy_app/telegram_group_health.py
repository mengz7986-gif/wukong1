"""Read-only Telegram membership checks; never expose credential URLs/errors."""
import requests


def probe(chat_id,token,post=None):
    post=post or requests.post
    def call(method,data=None):
        response=post('https://api.telegram.org/bot'+token+'/'+method,
                      data=data or {},timeout=8)
        value=response.json()
        if response.status_code!=200 or not value.get('ok'):
            raise ValueError('telegram_check_failed')
        return value['result']
    try:
        bot=call('getMe')
        chat=call('getChat',{'chat_id':str(chat_id)})
        if str(chat.get('id'))!=str(chat_id) or chat.get('type') not in ('group','supergroup'):
            return dict(status='not_a_group',ordinary_messages=False)
        member=call('getChatMember',{'chat_id':str(chat_id),'user_id':bot['id']})
        present=member.get('status') in ('member','administrator','creator') or (
            member.get('status')=='restricted' and member.get('is_member'))
        ordinary=bool(present and (bot.get('can_read_all_group_messages') or member.get('status') in ('administrator','creator')))
        return dict(status='ready' if ordinary else 'privacy_blocked' if present else 'not_in_group',
                    ordinary_messages=ordinary,title=chat.get('title',''))
    except Exception:
        return dict(status='check_failed',ordinary_messages=False)
