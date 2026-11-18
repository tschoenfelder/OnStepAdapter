"""The rig model (`SimConfig.rig_2eb2a5cb`) against the field capture it was fitted from.

Field capture 2eb2a5cb (2026-11-18, INDI sniffer, DEC at slew rate 9, mount at rest before
each pass, times from the capture's own UTC stamps):

- pass 2: MOTION_NORTH On 18:15:22.980 at DEC 52.35, Off 18:15:25.981, at rest at 55.81;
- pass 3: MOTION_NORTH On 18:15:29.140 at DEC 55.81, Off 18:15:36.276; reading 67.10 at
  18:15:36.100, peak 77.70 at 18:15:42.487.

The model must reproduce both passes, which is what makes the drive's tests against it
evidence for the rig rather than for the mock.
"""

from __future__ import annotations

import pytest

from onstep_mock.mount import Pose, SimConfig, VirtualMount


TICK_S = 0.005


def _run_pass(start_dec: float, off_after_s: float, *, probe_s: float | None = None):
    config = SimConfig.rig_2eb2a5cb(
        start_pose=Pose(ha_h=0.0, dec_deg=start_dec, pier_side="west"), start_parked=False,
    )
    mount = VirtualMount.create(config)
    mount.slew_rate = 9
    now = 0.0
    assert mount.start_manual("ns", 1, now) is None
    probe = None
    peak = start_dec
    off_sent = False
    while now < off_after_s + 30.0:
        now += TICK_S
        if not off_sent and now >= off_after_s:
            assert mount.start_manual("ns", 0, now) is None
            off_sent = True
        mount.tick(TICK_S, now)
        peak = max(peak, mount.dec_deg)
        if probe_s is not None and probe is None and now >= probe_s:
            probe = mount.dec_deg
        if off_sent and mount.motion == "IDLE":
            break
    return mount, peak, probe


def test_pass_2_of_the_capture_ends_where_the_rig_ended() -> None:
    mount, _, _ = _run_pass(52.35, 25.981 - 22.980)
    assert mount.motion == "IDLE"
    assert mount.dec_deg == pytest.approx(55.81, abs=0.3)


def test_pass_3_of_the_capture_coasts_as_far_as_the_rig_did() -> None:
    mount, peak, probe = _run_pass(55.81, 36.276 - 29.140, probe_s=36.100 - 29.140)
    # A published reading lags the axis by ~0.1 s (0.33 deg at 3.27 deg/s).
    assert probe == pytest.approx(67.10, abs=0.4)
    assert peak == pytest.approx(77.70, abs=0.3)
    assert mount.dec_deg == pytest.approx(peak)


def test_the_rig_flags_no_slewing_during_manual_motion() -> None:
    mount, _, _ = _run_pass(52.35, 3.0)
    mount.start_manual("ns", 1, 0.0)
    mount.tick(1.0, 1.0)
    assert mount.slewing
    assert "N" in mount.raw_gu(1.0)  # :GU# "not slewing", as the rig reported it
