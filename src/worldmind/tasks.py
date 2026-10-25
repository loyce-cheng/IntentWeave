"""Cancellation-safe execution of bounded synchronous work."""
from __future__ import annotations

import asyncio


async def computation(fn, *args, on_complete=None):
    """Join the worker before releasing its lock, even after repeated cancellation.

    A successful write's synchronous commit callback runs on the event loop
    before cancellation propagates, keeping revisions and receipts consistent.
    """
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation must never propagate into the thread wrapper.
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if on_complete is not None:
        result = on_complete(result)
    if cancelled:
        raise asyncio.CancelledError
    return result

