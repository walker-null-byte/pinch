"""Bridge between request handlers and a worker that runs its own event loop in another thread."""

import anyio
from anyio.from_thread import BlockingPortal


async def lookup(name: str) -> str:
    """Runs inside the worker's event loop. Slow on purpose."""
    await anyio.sleep(0.05)
    return name.upper()


async def lookup_in_worker(portal: BlockingPortal, name: str) -> str:
    """Called from a request handler (a different event loop than the portal's)."""
    return await portal.call_async(lookup, name)
