"""``Plan.ride_boundary_distance`` (the app's "Ride Edge" toggle) reaches ``send_schedule``'s wire message."""

from __future__ import annotations

import betterproto2

from pymammotion.data.model.hash_list import Plan
from pymammotion.mammotion.commands.mammotion_command import MammotionCommand
from pymammotion.proto import LubaMsg, NavPlanJobSet


def _plan_job_set(payload: bytes) -> NavPlanJobSet:
    msg = LubaMsg().parse(payload)
    name, value = betterproto2.which_one_of(msg.nav, "SubNavMsg")
    assert name == "todev_planjob_set"
    return value


def test_send_schedule_carries_ride_boundary_distance() -> None:
    """The app's "Ride Edge" toggle reaches the device on the send-schedule command."""
    command = MammotionCommand("Luba-VA6ABCDE", 1)
    plan = Plan(ride_boundary_distance=0.5)
    assert _plan_job_set(command.send_schedule(plan)).ride_boundary_distance == 0.5


def test_send_schedule_omits_ride_boundary_distance_when_off() -> None:
    """Off is the proto3 default, so a device that never heard of the field sees nothing."""
    command = MammotionCommand("Luba-VS6ABCDE", 1)
    plan = Plan()
    assert _plan_job_set(command.send_schedule(plan)).ride_boundary_distance == 0.0
