import asyncio
import anyio

async def run_sync(function, *args):
    """Drain a worker before propagating cancellation, so cleanup cannot race its writes."""
    task = asyncio.create_task(anyio.to_thread.run_sync(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
