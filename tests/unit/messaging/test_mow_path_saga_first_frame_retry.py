"""A batch request whose first frame never arrives is re-asked, not abandoned.

The device answers ``app_request_cover_paths`` in ~0.5 s when idle, ~5 s when a job is starting,
and occasionally not at all.  One expiry of the between-frames watchdog therefore must not end the
run — the APK re-issues on the same timer (``HashDataManager.handlerType_12333``).
"""

from __future__ import annotations

import asyncio

import pytest

from pymammotion.data.model.hash_list import HashList, NavGetHashListData, RootHashList
from pymammotion.messaging.broker import DeviceMessageBroker
from pymammotion.messaging.mow_path_saga import MowPathSaga
from pymammotion.transport.base import SagaFailedError
from tests.unit.messaging._helpers import make_command_builder as _make_command_builder


def _map_with_line_hashes(hashes: list[int]) -> HashList:
    hash_list = HashList()
    hash_list.root_hash_lists.append(
        RootHashList(total_frame=1, sub_cmd=3, data=[NavGetHashListData(current_frame=1, data_couple=hashes)])
    )
    return hash_list


async def _run_saga(attempts: int) -> tuple[MowPathSaga, object]:
    broker = DeviceMessageBroker()

    async def send_command(_cmd: bytes) -> None:
        return None

    builder = _make_command_builder()
    saga = MowPathSaga(
        command_builder=builder,
        send_command=send_command,
        get_map=lambda: _map_with_line_hashes([111, 222]),
        zone_hashs=[111],
        skip_planning=True,
        device_name="Luba-Test",
    )
    saga.step_timeout = 0.02
    saga.max_attempts = 1
    saga.total_timeout = 10.0
    saga.first_frame_attempts = attempts
    saga._route_val = object()  # noqa: SLF001 — stands in for a route the device already confirmed

    with pytest.raises(SagaFailedError):
        await saga.execute(broker)
    return saga, builder


async def test_a_silent_batch_is_re_asked_before_the_run_is_given_up() -> None:
    _saga, builder = await _run_saga(attempts=4)

    # One initial request plus three re-asks, each with its own transaction id.
    assert builder.get_line_info_list.call_count == 4
    transaction_ids = {call.args[1] for call in builder.get_line_info_list.call_args_list}
    assert len(transaction_ids) == 4


async def test_the_re_asks_are_bounded() -> None:
    """A device that never answers must not hold the queue open indefinitely."""
    _saga, builder = await _run_saga(attempts=1)

    assert builder.get_line_info_list.call_count == 1


def _unused() -> None:
    asyncio.sleep  # noqa: B018 - keeps the asyncio import honest for the event-loop marker
