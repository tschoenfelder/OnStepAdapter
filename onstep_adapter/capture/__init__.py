"""Passive capture of the traffic between an INDI client, indiserver and OnStepX.

Two ways to record, both write the trace format of ``indi_trace``:

* ``CaptureProxy`` - a transparent TCP pass-through the client connects to
  instead of indiserver (or that ``indi_lx200_OnStep`` connects to instead of
  the OnStepX TCP port). Bytes are forwarded unchanged; nothing is injected.
* ``import_pcap`` - convert a ``tcpdump`` recording; nothing in the path.

``analysis.check_assumptions`` tests a capture against the behavior the
adapter, ``docs/MOCK.md`` and ``onstep_mock`` assume; ``onstep_mock.validate``
replays a capture against the mock and reports where the two differ.
"""

from .analysis import Finding, calibrate, check_assumptions, segment_operations
from .pcap import import_pcap, read_pcap
from .proxy import CaptureProxy
from .stream import CaptureWriter, open_capture

__all__ = [
    "CaptureProxy",
    "CaptureWriter",
    "Finding",
    "calibrate",
    "check_assumptions",
    "import_pcap",
    "open_capture",
    "read_pcap",
    "segment_operations",
]
