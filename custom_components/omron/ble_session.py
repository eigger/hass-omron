"""Shared BLE poll / pairing session telemetry (connection + duration tickers)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
@asynccontextmanager
async def omron_poll_ble_telemetry(entry_data: dict) -> AsyncIterator[None]:
    """Mark BLE session active, tick duration each second, finalize elapsed time on exit."""
    connection_coordinator = entry_data["connection_coordinator"]
    duration_coordinator = entry_data["duration_coordinator"]
    started = perf_counter()
    ticker_task: asyncio.Task[None] | None = None

    async def _duration_ticker() -> None:
        while True:
            elapsed_tick = round(perf_counter() - started, 3)
            duration_coordinator.async_set_updated_data(elapsed_tick)
            await asyncio.sleep(1)

    connection_coordinator.async_set_updated_data(True)
    duration_coordinator.async_set_updated_data(0.0)
    ticker_task = asyncio.create_task(_duration_ticker())
    try:
        yield
    finally:
        if ticker_task is not None:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass
        elapsed = round(perf_counter() - started, 3)
        duration_coordinator.async_set_updated_data(elapsed)
        connection_coordinator.async_set_updated_data(False)
