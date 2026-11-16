"""Bound local CPU work without occupying HTTP or Telegram concurrency slots."""
import asyncio
import weakref

_slots=weakref.WeakKeyDictionary()
_light_slots=weakref.WeakKeyDictionary()


async def run(function,*args,**kwargs):
    return await _run(function,args,kwargs,_slots,2,'LOCAL_CPU')

async def run_light(function,*args,**kwargs):
    """One reserved slot for pure structure/typo checks, never business I/O."""
    return await _run(function,args,kwargs,_light_slots,1,'LOCAL_LIGHT')

async def _run(function,args,kwargs,slot_store,capacity,label):
    loop=asyncio.get_running_loop()
    reference=slot_store.get(loop)
    slots=reference() if reference is not None else None
    if slots is None:
        slots=asyncio.Semaphore(capacity)
        slot_store[loop]=weakref.ref(slots)
    from order_trace import stage
    with stage(label+'_WAIT'):
        await slots.acquire()
    try:
        # Keep the permit until the actual thread stops, even if its owner is
        # cancelled. This is pure parsing/validation, never business writes.
        task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
        try:
            with stage(label+'_WORK'):
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done,permit=slots: permit.release())
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            slots=None
            raise
    finally:
        if slots is not None:slots.release()
