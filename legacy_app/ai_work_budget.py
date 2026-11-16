"""Request-local advisory AI deadline and bounded fair network-only permits."""
import asyncio
import time
import weakref
from collections import deque
from contextvars import ContextVar
from contextlib import contextmanager

_CURRENT=ContextVar('rainbow_ai_work_budget',default=None)
_POOLS=weakref.WeakKeyDictionary()


@contextmanager
def work_budget(seconds=30):
    if _CURRENT.get() is not None:
        yield
        return
    token=_CURRENT.set({'seconds':seconds,'deadline':None})
    try:yield
    finally:_CURRENT.reset(token)


def remaining():
    state=_CURRENT.get()
    if state is None:return None
    if state['deadline'] is None:state['deadline']=time.monotonic()+state['seconds']
    return max(0,state['deadline']-time.monotonic())


def http_timeout(configured):
    left=remaining()
    if left is None:return configured
    if left<=0:raise TimeoutError('AI work budget expired')
    return (min(3,left),min(float(configured),left))


class FairPool:
    def __init__(self,limit=4):
        self.available=limit;self.limit=limit;self.waiters=deque()

    async def acquire(self):
        started=time.monotonic()
        if self.available and not self.waiters:
            self.available-=1
            from order_trace import mark
            mark('AI_PERMIT_WAIT',(time.monotonic()-started)*1000)
            return
        future=asyncio.get_running_loop().create_future();self.waiters.append(future)
        try:
            await asyncio.shield(future)
            from order_trace import mark
            mark('AI_PERMIT_WAIT',(time.monotonic()-started)*1000)
        except BaseException:
            if future.done() and not future.cancelled():self.release()
            else:future.cancel()
            raise

    def release(self):
        while self.waiters:
            future=self.waiters.popleft()
            if not future.done():
                future.set_result(None)
                return
        self.available+=1
        if self.available>self.limit:raise RuntimeError('AI permit accounting mismatch')


def pool():
    loop=asyncio.get_running_loop()
    reference=_POOLS.get(loop);value=reference() if reference else None
    if value is None:
        from config import AI_CONCURRENCY_LIMIT
        value=FairPool(AI_CONCURRENCY_LIMIT);_POOLS[loop]=weakref.ref(value)
    return value
