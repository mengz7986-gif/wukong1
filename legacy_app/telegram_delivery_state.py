"""Project-local cross-process send pacing, without messages or credentials."""
import fcntl
import json
import os
import time
import math
from pathlib import Path

DEFAULT_PATH=Path(__file__).resolve().parent/'cache'/'.telegram-delivery-state.json'


def group_due(state,chat,now):
    """20 group sends per rolling minute, shared by every local sender.

    Legacy files retained only the next 3.1s slot. Seed a conservative full
    history from that slot rather than treating an upgrade as unused quota.
    """
    if not (chat.startswith('-') or chat.startswith('@')):return 0
    histories=state.setdefault('group_sends',{})
    if not isinstance(histories,dict):raise ValueError('DELIVERY_GROUP_HISTORY_INVALID')
    if chat not in histories:
        last=state.get('chats',{}).get(chat,0)-3.1
        histories[chat]=([last-i*3.1 for i in reversed(range(20)) if last-i*3.1>now-60.05]
                         if chat in state.get('chats',{}) else [])
    rows=histories[chat]
    if (not isinstance(rows,list) or len(rows)>20
            or any(type(t) not in (int,float) or not math.isfinite(t) for t in rows)):
        raise ValueError('DELIVERY_GROUP_HISTORY_INVALID')
    rows=sorted(t for t in rows if t>now-60.05)
    histories[chat]=rows
    return rows[0]+60.05 if len(rows)>=20 else 0


def record_group(state,chat,now):
    if not (chat.startswith('-') or chat.startswith('@')):return
    histories=state['group_sends'];histories[chat].append(now)
    if len(histories)>1024:
        for key in list(histories):
            if not histories[key] or histories[key][-1]<=now-60.05:del histories[key]


class DeliveryState:
    def __init__(self,path=DEFAULT_PATH):
        self.path=Path(path)

    def _update(self,callback):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('a+') as file:
            os.fchmod(file.fileno(),0o600)
            fcntl.flock(file,fcntl.LOCK_EX)
            file.seek(0); raw=file.read()
            # Fail closed on corrupt state; do not burst by silently resetting it.
            state=json.loads(raw) if raw else {'global_next':0,'cooldown':0,'chats':{}}
            result=callback(state)
            file.seek(0);file.truncate();json.dump(state,file);file.flush()
            return result

    def reserve(self,chat,global_interval,chat_interval):
        now=time.time()
        def update(state):
            due=max(state.get('cooldown',0),state.get('global_next',0),
                    state['chats'].get(chat,0) if chat else 0,group_due(state,chat,now))
            if due>now:
                return due-now
            state['global_next']=now+global_interval
            if chat:
                state['chats'][chat]=now+chat_interval
            record_group(state,chat,now)
            if len(state['chats'])>1024:
                state['chats']={k:v for k,v in state['chats'].items() if v>now}
            return 0
        return self._update(update)

    def fence_legacy_rollback(self):
        """After all senders stop, retain pacing and fence the old limiter.

        An old binary ignores rolling histories. Do not restore an old state
        snapshot: it would forget sends already accepted after deployment.
        """
        def update(state):
            latest=max((t for rows in state.get('group_sends',{}).values()
                        for t in rows),default=0)
            state['cooldown']=max(state.get('cooldown',0),latest+60.05)
            return state['cooldown']
        return self._update(update)

    def cooldown(self,seconds):
        def update(state):
            state['cooldown']=max(state.get('cooldown',0),time.time()+seconds)
        self._update(update)
