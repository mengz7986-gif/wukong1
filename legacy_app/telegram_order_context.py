"""Group account identity and per-draft context, without modifying Telegram IDs."""
from types import SimpleNamespace
import logging


class DraftMessage:
    def __init__(self,original,payload):
        self.original=original
        self.payload=payload

    def __getattr__(self,name):
        return getattr(self.original,name)

    async def reply_text(self,*args,**kwargs):
        from failure_analysis import note_reply
        note_reply(args[0] if args else kwargs.get('text',''))
        receipt_id=self.payload.get('_group_receipt_message_id')
        if receipt_id and not self.payload.get('_group_receipt_closed'):
            edit_kwargs={key:value for key,value in kwargs.items()
                if key in ('parse_mode','entities','reply_markup','link_preview_options')}
            edit_kwargs.setdefault('reply_markup',None)
            try:
                message=await self.original.get_bot().edit_message_text(
                    text=args[0] if args else kwargs.get('text',''),
                    chat_id=self.payload.get('_group_receipt_chat_id') or self.original.chat_id,
                    message_id=receipt_id,**edit_kwargs)
            except Exception as error:
                from telegram.error import BadRequest, Forbidden
                if isinstance(error,BadRequest) and 'message is not modified' in str(error).lower():
                    message=SimpleNamespace(message_id=receipt_id)
                elif isinstance(error,(BadRequest,Forbidden)):
                    message=await self.original.reply_text(*args,**kwargs)
                else:
                    raise
            self.payload['_group_receipt_closed']=True
        else:
            message=await self.original.reply_text(*args,**kwargs)
        logging.getLogger(__name__).info('REPLY chat=%s source_message=%s',getattr(self.original,'chat_id',None),getattr(self.original,'message_id',None))
        message_id=getattr(message,'message_id',None)
        if isinstance(message_id,int) and not isinstance(message_id,bool):
            ids=self.payload.setdefault('_reply_message_ids',[])
            if message_id not in ids:ids.append(message_id)
            from draft_button_evidence import record
            record(self.payload,kwargs.get('reply_markup'),message_id)
        return message


class AccountUpdate:
    def __init__(self,original,account,draft=None):
        self.original=original
        self._rainbow_account=account
        self.actual_sender_id=original.effective_user.id
        self._rainbow_source_message_id=draft['message_id'] if draft else getattr(original.effective_message,'message_id',None)
        if draft and getattr(original,'message',None):
            self.message=DraftMessage(original.message,draft['payload'])
            self.effective_message=self.message
        self.effective_user=SimpleNamespace(id=int(account['telegram_id']),username=account.get('username'),
                                           is_bot=False,first_name='',last_name='',full_name='')

    def __getattr__(self,name):
        return getattr(self.original,name)


class DraftContext:
    def __init__(self,original,draft):
        self.original=original
        self.user_data=draft['payload']
        self.user_data['_group_draft_token']=draft['token']

    def __getattr__(self,name):
        return getattr(self.original,name)
