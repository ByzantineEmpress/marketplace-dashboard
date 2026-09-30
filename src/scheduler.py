"""Periodic marketplace syncing.

Listings and sales only change when the marketplaces say they do, and inventory
read by hand once is stale within the hour: items sell, prices move, and a sale
that is never pulled is revenue missing from every total. So the app syncs on a
timer as well as on demand.

Deliberately a plain asyncio task rather than a scheduling dependency. The work is
"every N minutes, one run at a time", which is a sleep loop; a cron-style library
would add a dependency, a second place for the schedule to live, and a way for two
copies of the job to overlap if the container is ever scaled beyond one.

Off unless SCHEDULED_SYNC_ENABLED is set. A background job that reaches out to
live marketplaces must never start by accident in a test run or on a developer's
machine, where it would spend API quota and write to the database unattended.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.config import config

# Populated while the loop is running, for tests and for the health endpoint.
LAST_RUN: dict = {"at": None, "results": [], "error": None}


def _log(message: str) -> None:
    """One line per event, prefixed so it can be found in a container's log."""
    print(f"[scheduler] {message}", flush=True)


def run_sync_once() -> list:
    """Run one full pass. Blocking, so call it off the event loop."""
    from src.marketplace_sync import sync_every_account

    return sync_every_account()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait, or return early if shutdown was signalled.

    Returns True when it is time to stop. Cancelling a plain ``asyncio.sleep``
    would also work, but a task cancelled mid-HTTP-request leaves the request
    half-finished; an Event lets the current pass finish first.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _loop(stop: asyncio.Event, interval_seconds: int,
                startup_delay_seconds: int) -> None:
    _log(f"enabled: every {interval_seconds // 60} min, "
         f"first run in {startup_delay_seconds}s")

    # A deploy should not be followed immediately by a burst of API calls, and a
    # container restart loop must not turn into a request loop against eBay.
    if await _sleep_or_stop(stop, startup_delay_seconds):
        return

    while not stop.is_set():
        import datetime as _dt

        try:
            # Blocking HTTP, so it must not run on the event loop.
            results = await asyncio.to_thread(run_sync_once)
            LAST_RUN["at"] = _dt.datetime.utcnow().isoformat(timespec="seconds")
            LAST_RUN["results"] = results
            LAST_RUN["error"] = None
            if results:
                failed = [r for r in results if not r.get("ok")]
                _log(f"synced {len(results)} connection(s), "
                     f"{len(failed)} with errors")
                for item in failed:
                    _log(f"  {item.get('platform')} for user "
                         f"{item.get('user_id')}: {item.get('error')}")
            else:
                _log("nothing connected yet, nothing to sync")
        except Exception as exc:  # noqa: BLE001
            # The loop must survive anything a sync can throw, or one bad night
            # silently ends scheduled syncing until the next deploy.
            LAST_RUN["error"] = str(exc)
            _log(f"run failed: {exc}")

        if await _sleep_or_stop(stop, interval_seconds):
            return

    _log("stopped")


def start(app) -> Optional[asyncio.Task]:
    """Start the timer if it is switched on. Returns the task, or None."""
    if not config.SCHEDULED_SYNC_ENABLED:
        _log("disabled (set SCHEDULED_SYNC_ENABLED=true to turn it on)")
        return None

    interval = max(1, int(config.SCHEDULED_SYNC_INTERVAL_MINUTES)) * 60
    delay = max(0, int(config.SCHEDULED_SYNC_STARTUP_DELAY_SECONDS))
    stop = asyncio.Event()

    # Held on the app so shutdown can reach it.
    app.state.sync_stop = stop
    task = asyncio.create_task(_loop(stop, interval, delay))
    app.state.sync_task = task
    return task


async def shutdown(app) -> None:
    """Ask the loop to finish its current pass and stop."""
    stop = getattr(app.state, "sync_stop", None)
    task = getattr(app.state, "sync_task", None)
    if stop is not None:
        stop.set()
    if task is not None and not task.done():
        try:
            # Bounded: a pass mid-request should be allowed to finish, but
            # shutdown must not hang on it forever.
            await asyncio.wait_for(task, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
