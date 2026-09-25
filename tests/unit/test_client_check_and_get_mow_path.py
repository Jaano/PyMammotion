"""check_and_get_mow_path — I.75/C.214: query route config first, never invalidate the cache."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pymammotion.client import MammotionClient
from pymammotion.device.handle import DeviceHandle
from tests._helpers import make_mock_handle, make_mock_transport


def _make_device_for_mow_path(
    *, path_hash: int, computed_path_hash: int, bol_hash: int = 999, current_mow_path: Any | None = None
) -> MagicMock:
    """A MowingDevice-shaped mock exercising ``_should_fetch_mow_path``'s gate and
    ``HashList.is_mow_path_current``'s inputs directly, since both live on real (mocked-out)
    methods here rather than being recomputed."""
    device = MagicMock()
    device.report_data.dev.sys_status = "idle"
    device.report_data.work.path_hash = path_hash
    device.report_data.locations = [MagicMock(bol_hash=bol_hash)]
    device.map.computed_bol_hash = bol_hash
    device.map.computed_path_hash = computed_path_hash
    device.map.current_mow_path = current_mow_path if current_mow_path is not None else {1: {}}
    device.map.is_mow_path_current.return_value = False
    return device


async def _make_handle_with_device(device_id: str, device_name: str, device: MagicMock) -> DeviceHandle:
    handle = make_mock_handle(device_id, device_name, device=device)
    await handle.add_transport(make_mock_transport())
    await handle.start()
    return handle


async def test_check_and_get_mow_path_does_not_invalidate_cache_while_idle() -> None:
    """An idle device reports ``work.path_hash == 1`` (820 captured reports, ``0`` never
    observed) — the common case, not evidence of a genuine route change. Wiping the cache here
    blanks the Mow path layer and nothing refetches it, since the gate below declines to fetch
    for ``path_hash <= 1`` regardless (I.75/C.214)."""
    client = MammotionClient()
    device = _make_device_for_mow_path(path_hash=1, computed_path_hash=987654)
    handle = await _make_handle_with_device("dev1", "Luba-Idle", device)
    await client._device_registry.register(handle)
    handle.send_raw = AsyncMock()  # type: ignore[method-assign]

    result = await client.check_and_get_mow_path("Luba-Idle")

    assert result is False
    device.map.invalidate_mow_path.assert_not_called()
    handle.send_raw.assert_not_called()
    await handle.stop()


async def test_check_and_get_mow_path_queries_route_configuration_before_fetching() -> None:
    """A session that has not seen a job start has an empty ``device.work`` — the app's own
    sequence reads the route configuration over the wire first, or there is nothing to build a
    cover-path request from (I.75/C.214)."""
    client = MammotionClient()
    device = _make_device_for_mow_path(path_hash=12345, computed_path_hash=1)
    handle = await _make_handle_with_device("dev1", "Luba-Route", device)
    await client._device_registry.register(handle)

    call_order: list[str] = []
    handle.send_raw = AsyncMock(side_effect=lambda *a, **k: call_order.append("send_raw"))  # type: ignore[method-assign]

    async def _fake_start_saga(*args: Any, **kwargs: Any) -> bool:
        call_order.append("start_saga")
        return True

    client.start_mow_path_saga = AsyncMock(side_effect=_fake_start_saga)  # type: ignore[method-assign]

    result = await client.check_and_get_mow_path("Luba-Route")

    assert result is True
    assert call_order == ["send_raw", "start_saga"]
    device.map.invalidate_mow_path.assert_not_called()
    await handle.stop()
