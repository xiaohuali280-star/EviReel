import asyncio
import anyio
from functools import partial

async def run_sync(function, *args, **kwargs):
    """Drain a worker before propagating cancellation, so cleanup cannot race its writes."""
    task = asyncio.create_task(anyio.to_thread.run_sync(partial(function, **kwargs), *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
