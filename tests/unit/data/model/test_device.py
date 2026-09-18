"""Tests for MowingDevice JSON serialization with int-keyed HashList fields."""
from __future__ import annotations

import json
import math

import pytest

from pymammotion.data.model.device import MowingDevice
from pymammotion.data.model.hash_list import FrameList, HashList, MowPath, NavGetCommData
from pymammotion.proto import MqttRtkConnect, ReportInfoData, RptDevLocation, RptRtk


def _make_hash_list_with_int_keys() -> HashList:
    hl = HashList()
    hl.area[12345] = FrameList(total_frame=2, sub_cmd=0, data=[NavGetCommData(hash=12345)])
    hl.obstacle[99999] = FrameList(total_frame=1, sub_cmd=1)
    hl.path[77777] = FrameList(total_frame=3, sub_cmd=2)
    hl.current_mow_path[1] = {0: MowPath(area=12345, total_frame=1)}
    return hl


def test_mowing_device_to_json_with_int_keys() -> None:
    """MowingDevice.to_json() must not raise when HashList has int-keyed dicts."""
    device = MowingDevice(name="test-device")
    device.map = _make_hash_list_with_int_keys()

    json_str = device.to_json()
    assert isinstance(json_str, str)

    data = json.loads(json_str)
    # orjson serialises int keys as strings in JSON (the JSON spec requires string keys)
    assert "12345" in data["map"]["area"]
    assert "99999" in data["map"]["obstacle"]
    assert "77777" in data["map"]["path"]
    assert "1" in data["map"]["current_mow_path"]


def test_mowing_device_to_jsonb_with_int_keys() -> None:
    """MowingDevice.to_jsonb() must not raise and return bytes."""
    device = MowingDevice(name="test-device")
    device.map = _make_hash_list_with_int_keys()

    raw = device.to_jsonb()
    assert isinstance(raw, bytes)
    assert b"12345" in raw


def test_empty_mowing_device_roundtrip() -> None:
    """Empty MowingDevice serialises and deserialises cleanly."""
    device = MowingDevice(name="empty")
    json_str = device.to_json()
    assert json_str
    data = json.loads(json_str)
    assert data["name"] == "empty"


# ===========================================================================
# The OTA check (CheckDeviceVersion.current_version) is the cloud's view of the
# ===========================================================================
from pymammotion.data.model.device import Device, MowerDevice, RTKBaseStationDevice, create_device
from pymammotion.http.model.http import CheckDeviceVersion


def _check(version: str, *, device_id: str = "iot-1") -> CheckDeviceVersion:
    return CheckDeviceVersion(current_version=version, device_id=device_id)


def test_mower_seeds_device_version() -> None:
    device = MowerDevice(name="Luba-VS123")
    check = _check("1.12.0.466")
    device.apply_version_check(check)
    assert device.update_check is check
    assert device.device_firmwares.device_version == "1.12.0.466"


def test_rtk_seeds_device_version() -> None:
    device = RTKBaseStationDevice(name="RTK-abc")
    device.apply_version_check(_check("3.0.1"))
    assert device.device_firmwares.device_version == "3.0.1"


def test_empty_current_version_does_not_overwrite() -> None:
    device = MowerDevice(name="Luba-VS123")
    device.device_firmwares.device_version = "1.12.0.466"
    device.apply_version_check(_check(""))  # empty cloud value
    assert device.device_firmwares.device_version == "1.12.0.466"  # preserved


def test_base_device_without_firmware_field_is_safe() -> None:
    # Base Device has update_check but no device_firmwares — must not raise.
    device = Device(name="x")
    device.apply_version_check(_check("9.9.9"))
    assert device.update_check.current_version == "9.9.9"


def test_seeds_version_feeds_detection_gate() -> None:
    # End-to-end: OTA version flows into the firmware-gated obstacle options.
    from pymammotion.data.model.mowing_modes import DetectionStrategy

    device = create_device("Luba-VS123", "a1pvCnb3PPu")
    device.apply_version_check(_check("1.11.0"))  # below the 1.12.0 threshold
    options = DetectionStrategy.for_device(device.name, device.device_firmwares.device_version)
    assert DetectionStrategy.slow_touch in options  # old-firmware option set


# ---------------------------------------------------------------------------
# MowingDevice.update_report_data — the RTK origin behind every position
# ---------------------------------------------------------------------------

# One report frame's shape: the mower ~30 m from its origin, plus the device's own absolute
# reading of that same point. The two are related by exactly the ENU offset, which is what shows
# `mqtt_rtk_info` to be a position rather than an origin.
#
# The origin is synthetic — round degrees converted to the radians the wire carries. Tests here
# must never carry a real deployment's coordinates.
_ORIGIN = (math.radians(50.0), math.radians(10.0))
_ENU_OFFSET = (-298_000, 61_900)  # real_pos_x / real_pos_y, 1e-4 m — 29.8 m west, 6.19 m north


def _expected_position() -> tuple[float, float]:
    """Where `_ENU_OFFSET` from `_ORIGIN` lands, in degrees, by flat-earth arithmetic.

    Deliberately not the library's own ECEF conversion: an expectation computed the same way as
    the thing under test restates it instead of checking it. Over tens of metres the two agree far
    inside the tolerance the assertions use.
    """
    lat0, lon0 = math.degrees(_ORIGIN[0]), math.degrees(_ORIGIN[1])
    metres_per_degree_lat = 111_320.0
    east, north = _ENU_OFFSET[0] / 1e4, _ENU_OFFSET[1] / 1e4
    return (
        lat0 + north / metres_per_degree_lat,
        lon0 + east / (metres_per_degree_lat * math.cos(math.radians(lat0))),
    )


def _report() -> ReportInfoData:
    reported_lat, reported_lon = _expected_position()
    return ReportInfoData(
        rtk=RptRtk(mqtt_rtk_info=MqttRtkConnect(latitude=reported_lat, longitude=reported_lon)),
        locations=[RptDevLocation(real_pos_x=_ENU_OFFSET[0], real_pos_y=_ENU_OFFSET[1], pos_type=4)],
    )


def test_no_position_until_an_origin_has_been_seen() -> None:
    """`mqtt_rtk_info` is the device's own position, not its origin, so it cannot stand in for one:
    seeding the origin with it offsets every position computed afterwards by however far the mower
    stood from the real origin — tens of metres while mowing — and produces a well-formed
    coordinate nothing downstream can tell from a correct one. With no origin the position stays
    at the collapsed (0, 0) anchor, which every consumer already rejects as the no-fix sentinel."""
    device = MowingDevice()

    device.update_report_data(_report())

    assert abs(device.location.device.latitude) < 1.0
    assert abs(device.location.device.longitude) < 1.0
    assert device.location.RTK.latitude == 0.0


def test_the_origin_from_the_buffer_puts_the_position_where_the_device_says_it_is() -> None:
    """With the real origin in hand the projection reproduces the device's own absolute reading of
    the same point — which is the whole reason that reading can be used to check the origin."""
    device = MowingDevice()
    device.location.RTK.latitude, device.location.RTK.longitude = _ORIGIN

    device.update_report_data(_report())

    expected_lat, expected_lon = _expected_position()
    assert device.location.device.latitude == pytest.approx(expected_lat, abs=1e-5)
    assert device.location.device.longitude == pytest.approx(expected_lon, abs=1e-5)
