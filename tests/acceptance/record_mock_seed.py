"""Regenerate the mock seed recording (not a test; run by hand, about 80 s).

    python3 tests/acceptance/record_mock_seed.py \\
        tests/acceptance/recordings/mock-seed-unpark-axis-abort-home-park.jsonl

The unchanged adapter drives the mock at real speed through the same
CaptureProxy used on the real rig: UNPARK, a finite RA-axis move to arrival,
22 s stationary (drift), a DEC-axis move stopped mid-slew, GO HOME, PARK.
"""

import sys
import time

from onstep_adapter import OnStepClient
from onstep_adapter.capture import CaptureProxy, open_capture
from onstep_mock import SimConfig, SimulatedOnStepServer
from onstep_mock.testing import runtime_config, wait_for_live_status


def main(out: str) -> None:
    with SimulatedOnStepServer(SimConfig(speed=1.0)) as sim:
        writer = open_capture(out, label="mock seed: unpark, axis move, abort, home, park")
        with CaptureProxy(writer, upstream=(sim.host, sim.port), listen=("127.0.0.1", 0)) as proxy:
            with OnStepClient(config=runtime_config(sim, port=proxy.port)) as client:
                wait_for_live_status(client)
                client.mount.unpark()
                time.sleep(1)
                client.mount.move_ra_axis_deg(5).wait(timeout=60)
                time.sleep(22)
                move = client.mount.move_dec_axis_deg(-8)
                sim.wait_until(lambda m: m.motion == "SLEWING", timeout=5)
                time.sleep(0.3)
                move.stop()
                time.sleep(1)
                client.mount.go_home().wait(timeout=60)
                time.sleep(1)
                client.mount.park().wait(timeout=60)
                time.sleep(1)
        sim.assert_no_forbidden()


if __name__ == "__main__":
    main(sys.argv[1])
