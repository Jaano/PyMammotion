"""Tests for the BLE polling loop's report-stream staleness watchdog.

The polling loop sends ``RPT_KEEP`` every 8 s to renew the device-side
subscription, but a renew is blind — if the firmware silently stops sending
report frames, we'd keep renewing forever to nothing.

The APK arms ``MSG_RPT_START_TIME_OUT`` / ``MSG_DATA_TIME_OUT`` to bounce
the subscription after 10-15 s of silence.  Our equivalent: on each loop
tick check ``handle._last_report_at`` and, if no inbound frame has arrived
within ``_BLE_STREAM_STALE_THRESHOLD`` seconds, send ``RPT_STOP`` and clear
``_ble_stream_active`` so the next iteration sends a fresh ``RPT_START``.

To keep the tests real-clock-safe we simulate "stale" / "fresh" by setting
``_last_report_at`` relative to ``time.monotonic()`` rather than freezing
the clock, and patch ``asyncio.sleep`` to a no-op so the test doesn't wait
out the 8-second renew interval at the end of each iteration.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pymammotion.device.ble_loop import (
    _BLE_STREAM_STALE_THRESHOLD,
    ble_polling_loop,
)
from pymammotion.device.modes import _DeviceMode
from pymammotion.proto import RptAct
from pymammotion.transport.base import TransportType


def _make_handle() -> MagicMock:
    """Build a DeviceHandle double rigged to fire the continuous-stream branch."""
    handle = MagicMock()
    handle._stopping = False
    handle.device_name = "Luba-TEST"
    handle.ble_stream_active = False
    handle.last_report_at = 0.0
    handle._rearm_event = MagicMock()

    ble = MagicMock()
    ble.is_connected = True
    handle._transports = {TransportType.BLE: ble}

    # ACTIVE mode → continuous stream (ble_interval = None)
    handle.device_mode = MagicMock(return_value=_DeviceMode.ACTIVE)
    handle.in_no_request_mode = MagicMock(return_value=False)

    # Async hooks the loop calls. RPT_KEEP goes through _enqueue_ble_stream_command (BLE-pinned,
    # send_heartbeat) rather than the MQTT-oriented _send_report_stream_keep — an AsyncMock here
    # accepts any call shape, so it verifies which *branch* fires but not that the real method
    # would accept the call; test_active_stream_keep_call_site_matches_real_handle covers that.
    handle._enqueue_ble_stream_command = AsyncMock()
    handle._send_one_shot_report = AsyncMock()

    return handle


def _calls_for(handle: MagicMock, act: RptAct) -> list:
    return [c for c in handle._enqueue_ble_stream_command.await_args_list if c.args and c.args[0] == act]


async def _run_one_tick(handle: MagicMock) -> None:
    """Drive the polling loop for exactly one iteration's worth of action.

    The real loop sleeps 8 s between iterations.  We replace that sleep with
    one that bumps ``_stopping`` so the loop exits cleanly after a single tick.
    """
    _real_sleep = asyncio.sleep

    async def _short_sleep(_seconds: float) -> None:
        # First call comes from the loop tail — set stop so the loop exits.
        handle._stopping = True
        # Yield once so the loop has a chance to observe the flag.
        await _real_sleep(0)

    with patch("pymammotion.device.ble_loop.asyncio.sleep", _short_sleep):
        await asyncio.wait_for(ble_polling_loop(handle), timeout=2.0)


@pytest.mark.asyncio
async def test_first_iteration_sends_rpt_start() -> None:
    """When the stream isn't active yet, the loop must send RPT_START count=0."""
    handle = _make_handle()

    await _run_one_tick(handle)

    handle._enqueue_ble_stream_command.assert_any_await(RptAct.RPT_START, count=0)
    assert _calls_for(handle, RptAct.RPT_KEEP) == []


@pytest.mark.asyncio
async def test_active_stream_sends_rpt_keep_when_data_flowing() -> None:
    """When the stream is active and frames are arriving, the loop sends RPT_KEEP."""
    handle = _make_handle()
    handle.ble_stream_active = True
    # Pretend a report arrived very recently — well within the stale threshold
    handle.last_report_at = time.monotonic() - 1.0

    await _run_one_tick(handle)

    handle._enqueue_ble_stream_command.assert_any_await(RptAct.RPT_KEEP, count=0)
    assert _calls_for(handle, RptAct.RPT_STOP) == [], "RPT_STOP should not fire on a healthy stream"


@pytest.mark.asyncio
async def test_stale_stream_bounces_with_stop_then_fresh_start() -> None:
    """When _last_report_at is older than _BLE_STREAM_STALE_THRESHOLD, the loop
    must send RPT_STOP and re-issue RPT_START in the same tick.

    Regression for the gap copied from the APK's MSG_RPT_START_TIME_OUT /
    MSG_DATA_TIME_OUT timers.
    """
    handle = _make_handle()
    handle.ble_stream_active = True
    # Last report was well past the threshold ago
    handle.last_report_at = time.monotonic() - (_BLE_STREAM_STALE_THRESHOLD + 5.0)

    await _run_one_tick(handle)

    assert len(_calls_for(handle, RptAct.RPT_STOP)) == 1, "stale stream must trigger exactly one RPT_STOP"
    assert len(_calls_for(handle, RptAct.RPT_START)) == 1, "stale stream must trigger a fresh RPT_START"
    # And critically — no RPT_KEEP fired this tick (we bounced instead)
    assert _calls_for(handle, RptAct.RPT_KEEP) == []


@pytest.mark.asyncio
async def test_stale_check_skipped_before_first_report() -> None:
    """If _last_report_at == 0 (fresh boot, no report yet), the stale check
    must NOT trigger — otherwise the threshold trips immediately on startup.
    """
    handle = _make_handle()
    handle.ble_stream_active = True
    handle.last_report_at = 0.0  # sentinel — never received a report

    await _run_one_tick(handle)

    handle._enqueue_ble_stream_command.assert_any_await(RptAct.RPT_KEEP, count=0)
    assert _calls_for(handle, RptAct.RPT_STOP) == [], "must not bounce on a never-received-report boot"


@pytest.mark.asyncio
async def test_stale_check_tolerates_stop_failure() -> None:
    """If RPT_STOP enqueue raises during the bounce, the loop must still clear
    _ble_stream_active so the next branch sends RPT_START."""
    handle = _make_handle()
    handle.ble_stream_active = True
    handle.last_report_at = time.monotonic() - (_BLE_STREAM_STALE_THRESHOLD + 5.0)
    # First call (RPT_STOP) raises; second call (RPT_START) succeeds
    handle._enqueue_ble_stream_command.side_effect = [
        RuntimeError("transient BLE write failure"),
        None,
    ]

    await _run_one_tick(handle)

    # Even though RPT_STOP raised, the next branch must have sent RPT_START
    assert handle._enqueue_ble_stream_command.await_count >= 2
    second_call = handle._enqueue_ble_stream_command.await_args_list[1]
    assert second_call.args[0] == RptAct.RPT_START


@pytest.mark.asyncio
async def test_active_stream_keep_call_site_matches_real_handle() -> None:
    """Regression: every test above mocks the whole handle, so an AsyncMock() accepts any call
    shape and a call-site/signature mismatch (this file's own bug, once — ble_loop.py called
    ``handle._send_report_stream_keep()`` with no args after that method's signature gained a
    required ``duration_ms``) raises nowhere in this file. Drive the exact call the "stream
    already active" branch makes against a *real* DeviceHandle instead, so a future signature
    change on either side fails here rather than only in production."""
    from pymammotion.device.handle import DeviceHandle

    ble = MagicMock()
    ble.transport_type = TransportType.BLE
    ble.is_connected = True
    ble.send_heartbeat = AsyncMock()
    device = MagicMock()
    device.report_data.dev.sys_status = "idle"
    handle = DeviceHandle(device_id="dev1", device_name="Mower One", initial_device=device, ble_transport=ble)

    async def _run_enqueued_immediately(work: object, **_kwargs: object) -> None:
        await work()  # type: ignore[operator]

    handle.queue.enqueue = _run_enqueued_immediately  # type: ignore[method-assign]

    await handle._enqueue_ble_stream_command(RptAct.RPT_KEEP, count=0)  # noqa: SLF001

    ble.send_heartbeat.assert_awaited_once()
    sent_bytes = ble.send_heartbeat.await_args.args[0]
    from pymammotion.proto import LubaMsg

    cfg = LubaMsg().parse(sent_bytes).sys.todev_report_cfg
    assert cfg.act == RptAct.RPT_KEEP
    assert cfg.count == 0
