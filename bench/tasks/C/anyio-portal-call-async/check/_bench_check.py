import sys
import threading
import anyio
from anyio.from_thread import start_blocking_portal
import bridge

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

cancelled = threading.Event()

async def slow(name):
    try:
        await anyio.sleep(0.4)
        return name.upper()
    except anyio.get_cancelled_exc_class():
        cancelled.set()
        raise

bridge.lookup = slow
ticks = 0

async def ticker():
    global ticks
    while True:
        await anyio.sleep(0.01)
        ticks += 1

async def main(portal):
    global ticks
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        result = await bridge.lookup_in_worker(portal, "bob")
        tg.cancel_scope.cancel()
    if result != "BOB":
        fail(f"wrong result {result!r}")
    if ticks < 10:
        fail(f"the caller's event loop was blocked (only {ticks} ticks in 0.4 s)")
    with anyio.move_on_after(0.1):
        await bridge.lookup_in_worker(portal, "never")
        fail("the call should have been cancelled by the timeout")

with start_blocking_portal() as portal:
    anyio.run(main, portal)
    if not cancelled.wait(2):
        fail("cancelling the caller did not cancel the work in the worker loop")
print("PASS")
