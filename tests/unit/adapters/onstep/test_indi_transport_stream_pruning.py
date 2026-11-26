"""The reader must not keep processed stream elements.

Field 2026-11-26: the driver sent hundreds of thousands of <message> elements; only
def*/set*Vector elements were removed from the XML root, so the rest piled up, every
removal got slower, and the reader fell so far behind that commands timed out.
"""

import socket
import threading
import xml.etree.ElementTree as ET

from onstep_adapter import indi_transport
from onstep_adapter.indi_transport import IndiTransport


MESSAGE = b'<message device="LX200 OnStep" message="[SCOPE] CMD &lt;:GU#&gt;"/>'
DELETE = b'<delProperty device="LX200 OnStep" name="OLD"/>'
UPDATE = (
    b'<setNumberVector device="LX200 OnStep" name="EQUATORIAL_EOD_COORD" state="Ok">'
    b'<oneNumber name="RA">2.5</oneNumber>'
    b'</setNumberVector>'
)


def test_reader_drops_every_processed_stream_element(monkeypatch):
    roots = []

    class RecordingParser(ET.XMLPullParser):
        def read_events(self):
            for event, node in super().read_events():
                if event == "start" and node.tag == "stream":
                    roots.append(node)
                yield event, node

    monkeypatch.setattr(indi_transport.ET, "XMLPullParser", RecordingParser)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    done = threading.Event()

    def serve():
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall((MESSAGE * 500) + DELETE + UPDATE)
            done.wait(5)

    server = threading.Thread(target=serve)
    server.start()
    transport = IndiTransport(port=listener.getsockname()[1])
    try:
        transport.connect()
        updated = transport.wait_property(
            "LX200 OnStep", "EQUATORIAL_EOD_COORD", timeout=5.0
        )
        assert updated.values == {"RA": "2.5"}
        transport.close()
        assert len(roots) == 1
        assert list(roots[0]) == []
    finally:
        done.set()
        transport.close()
        server.join(timeout=5)
        listener.close()
