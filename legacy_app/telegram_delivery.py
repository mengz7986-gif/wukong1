"""Bounded network-only Telegram delivery control (no business callbacks)."""
import asyncio
import hashlib
import logging
import time
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from telegram.error import RetryAfter, TimedOut, TelegramError, BadRequest
from telegram.ext import BaseRateLimiter

logger = logging.getLogger(__name__)
_inventory_transaction = ContextVar('inventory_document_transaction', default=None)


@contextmanager
def inventory_document_transaction(transaction):
    """Tag a Message.reply_document call without sending metadata to Telegram."""
    token = _inventory_transaction.set(str(transaction))
    try:
        yield
    finally:
        _inventory_transaction.reset(token)


class DeliveryQueueFull(TelegramError):
    pass


class TelegramDeliveryLimiter(BaseRateLimiter):
    def __init__(self, *, retries=2, group_interval=3.1,
                 private_interval=1.05, global_interval=0.04,
                 max_pending=256, clock=time.monotonic, sleep=asyncio.sleep, state=None):
        self.retries = retries
        self.group_interval = group_interval
        self.private_interval = private_interval
        self.global_interval = global_interval
        self.max_pending = max_pending
        self.clock, self.sleep = clock, sleep
        self.state = state
        self.pending = 0
        self._lock = asyncio.Lock()
        self._chat_next = {}
        self._global_next = 0.0
        self._cooldown = 0.0
        self._error_notice = {}
        self._document_transactions = OrderedDict()
        self._delivery_turns=weakref.WeakValueDictionary()
        self._group_sends={}

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def allow_error_notice(self, chat_id, error):
        # A failed delivery must not cause another delivery to the same limited chat.
        if isinstance(error, (RetryAfter, TimedOut, DeliveryQueueFull)):
            return False
        key = str(chat_id)
        now = self.clock()
        if now < self._error_notice.get(key, 0):
            return False
        self._error_notice[key] = now + 60
        return True

    async def _wait_slot(self, chat, *, order_receipt=False):
        is_group=chat.startswith('-') or chat.startswith('@')
        interval=(1.05 if order_receipt else self.group_interval) if is_group else self.private_interval
        if self.state is not None:
            while True:
                delay = await asyncio.to_thread(self.state.reserve,chat,self.global_interval,interval)
                if delay <= 0:
                    return
                await self.sleep(delay)
        # Recheck cooldown after sleep: another request may have extended it.
        while True:
            async with self._lock:
                now = self.clock()
                from telegram_delivery_state import group_due,record_group
                group_state={'chats':self._chat_next,'group_sends':self._group_sends}
                due = max(self._cooldown, self._global_next,
                          self._chat_next.get(chat, 0) if chat else 0,group_due(group_state,chat,now))
                if due <= now:
                    self._global_next = now + self.global_interval
                    if chat:
                        self._chat_next[chat] = now + interval
                    record_group(group_state,chat,now)
                    # Remove expired idle state without disturbing future reservations.
                    if len(self._chat_next) > 1024:
                        self._chat_next = {k:v for k,v in self._chat_next.items() if v > now}
                    return
                delay = due - now
            await self.sleep(delay)

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        policy = dict(rate_limit_args) if isinstance(rate_limit_args, dict) else {}
        from recovery_timeout_hold import guard_transport as guard_recovery
        guard_recovery(endpoint,data)
        if endpoint=='sendMessage':
            from cancel_notice_hold import guard_transport as guard_cancellation
            guard_cancellation(data)
        from six_unknown_receipt_hold import guard_transport
        guard_transport(data,policy)
        if endpoint in ('sendDocument','sendMediaGroup') and _inventory_transaction.get():
            policy['inventory_transaction'] = _inventory_transaction.get()
        transaction = policy.get('inventory_transaction') if endpoint in ('sendDocument','sendMediaGroup') else None
        if not transaction:
            return await self._dispatch(callback, args, kwargs, endpoint, data, policy)
        key = (str(data.get('chat_id', '')), str(transaction))
        existing = self._document_transactions.get(key)
        if existing is not None:
            ok, result = await asyncio.shield(existing)
            if ok:
                return result
            raise result
        # Same-event callbacks coalesce; a new Telegram update creates a new
        # transaction even if its inventory hash is unchanged. Memory bounded;
        # this is not a claim of Telegram/server-side exactly-once delivery.
        while len(self._document_transactions) >= self.max_pending:
            done = next((k for k, f in self._document_transactions.items() if f.done()), None)
            if done is None:
                raise DeliveryQueueFull('Inventory transaction capacity reached')
            del self._document_transactions[done]
        future = asyncio.get_running_loop().create_future()
        self._document_transactions[key] = future
        try:
            result = await self._dispatch(callback, args, kwargs, endpoint, data, policy)
        except BaseException as exc:
            future.set_result((False, exc))
            raise
        future.set_result((True, result))
        return result

    async def _dispatch(self, callback, args, kwargs, endpoint, data, policy):
        if self.pending >= self.max_pending:
            raise DeliveryQueueFull('Telegram delivery queue capacity reached')
        self.pending += 1
        chat=str(data.get('chat_id',''))
        if endpoint.startswith('editMessage'):chat='edit:'+chat if chat else ''
        elif not endpoint.startswith('send') and endpoint not in ('forwardMessage','copyMessage','copyMessages','forwardMessages'):chat=''
        from order_trace import stage
        try:
            if not chat:
                return await self._dispatch_transport(callback,args,kwargs,endpoint,data,policy)
            # asyncio.Lock is FIFO: later-ready results cannot repeatedly win
            # a pacing slot over an older ready receipt. Only one waiter per
            # chat touches the cross-process pacing file, avoiding wake storms.
            lock=self._delivery_turns.get(chat)
            if lock is None:
                lock=asyncio.Lock();self._delivery_turns[chat]=lock
            with stage('TELEGRAM_TURN_WAIT'):
                await lock.acquire()
            try:
                return await self._dispatch_transport(callback,args,kwargs,endpoint,data,policy)
            finally:lock.release()
        finally:self.pending-=1

    async def _dispatch_transport(self, callback, args, kwargs, endpoint, data, policy):
        chat = str(data.get('chat_id', ''))
        pacing_chat=chat
        if endpoint.startswith('editMessage'):
            pacing_chat='edit:'+chat if chat else ''
        elif not endpoint.startswith('send') and endpoint not in ('forwardMessage','copyMessage','copyMessages','forwardMessages'):
            pacing_chat=''
        fingerprint = hashlib.sha256(chat.encode()).hexdigest()[:12] if chat else 'none'
        from order_trace import stage
        started=self.clock()
        try:
            for attempt in range(self.retries + 1):
                with stage('TELEGRAM_PACING_WAIT'):
                    await self._wait_slot(pacing_chat,order_receipt=policy.get('order_receipt') is True)
                try:
                    # SDK HTTP callback only: never invoke a handler or business pipeline.
                    with stage('TELEGRAM_HTTP'):
                        return await callback(*args, **kwargs)
                except BadRequest as exc:
                    missing=any(phrase in str(exc).casefold() for phrase in (
                        'message to be replied not found','replied message not found','reply message not found'))
                    # Only the SDK's proven transport signature can be adapted.
                    # Rejection is definitive: no message was sent. Retain chat,
                    # thread and body; remove only the unavailable Reply target.
                    if (endpoint!='sendMessage' or not missing or len(args)!=2
                            or args[0]!=endpoint or args[1] is not data
                            or not (data.get('reply_parameters') or data.get('reply_to_message_id'))):
                        raise
                    fallback=dict(data)
                    fallback.pop('reply_parameters',None)
                    fallback.pop('reply_to_message_id',None)
                    logger.warning('telegram_delivery endpoint=%s chat=%s kind=reply_target_missing fallback=same_chat',endpoint,fingerprint)
                    await self._wait_slot(pacing_chat,order_receipt=policy.get('order_receipt') is True)
                    # Exactly one network-only fallback. In particular, never
                    # repeat an uncertain timeout or invoke business handlers.
                    return await callback(endpoint,fallback,**kwargs)
                except RetryAfter as exc:
                    delay = exc.retry_after
                    delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                    if self.state is not None:
                        await asyncio.to_thread(self.state.cooldown,delay+0.1)
                    async with self._lock:
                        self._cooldown = max(self._cooldown, self.clock() + delay + 0.1)
                    logger.warning('telegram_delivery endpoint=%s chat=%s attempt=%d kind=retry_after delay=%.2f',
                                   endpoint, fingerprint, attempt + 1, delay)
                    if attempt == self.retries:
                        raise
                except TimedOut:
                    logger.warning('telegram_delivery endpoint=%s chat=%s attempt=%d kind=timeout',
                                   endpoint, fingerprint, attempt + 1)
                    if endpoint.startswith('send') or endpoint in {'forwardMessage', 'copyMessage'}:
                        # Telegram has no delivery lookup/idempotency key for sends.
                        # Accepted-vs-failed cannot be determined after timeout;
                        # suppress automatic resend in either case, preserve text.
                        raise
                    if attempt == self.retries:
                        raise
                    # Telegram may have accepted a timed-out send; business never repeats.
                    await self.sleep(min(2 ** attempt, 4))
        finally:
            # Includes retry/pacing, excludes FIFO turn waiting. No business
            # callback or send is retried as part of timing collection.
            from order_trace import mark
            mark('TELEGRAM_TRANSPORT_TOTAL',max(0,self.clock()-started)*1000)
