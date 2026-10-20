import socket
import threading
import time
import unittest

from onstep_adapter.indi_transport import IndiTransport


PROPERTY = (
    b'<defSwitchVector device="LX200 OnStep" name="SAFE_MERIDIAN_FLIP" '
    b'perm="rw" state="Idle">'
    b'<defSwitch name="OFF">On</defSwitch>'
    b'<defSwitch name="ON">Off</defSwitch>'
    b'</defSwitchVector>'
)


class IndiTransportTests(unittest.TestCase):
    def test_unexpected_reader_exception_marks_transport_failed_and_wakes_waiter(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        class FailingTransport(IndiTransport):
            def _record(self, node):
                raise RuntimeError("unexpected reader failure")

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(PROPERTY)
                time.sleep(0.5)

        server = threading.Thread(target=serve)
        server.start()
        transport = FailingTransport(port=listener.getsockname()[1])
        started = time.monotonic()
        try:
            transport.connect()
            with self.assertRaises(ConnectionError) as caught:
                transport.wait_property(
                    "LX200 OnStep", "NEVER_ARRIVES", timeout=5.0
                )
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(str(caught.exception.__cause__), "unexpected reader failure")
            self.assertFalse(transport.is_open)
            with self.assertRaises(ConnectionError) as send_error:
                transport._send('<getProperties version="1.7"/>')
            self.assertIs(send_error.exception.__cause__, caught.exception.__cause__)
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()

    def test_reader_failure_can_close_and_reconnect_without_device_disconnect(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        commands = []

        class FailOnceTransport(IndiTransport):
            fail_next_record = True

            def _record(self, node):
                if self.fail_next_record:
                    self.fail_next_record = False
                    raise RuntimeError("first reader failed")
                super()._record(node)

        def serve():
            first, _ = listener.accept()
            with first:
                commands.append(first.recv(4096).decode())
                first.sendall(PROPERTY)
                time.sleep(0.2)
            second, _ = listener.accept()
            with second:
                commands.append(second.recv(4096).decode())
                second.sendall(PROPERTY)
                time.sleep(0.2)

        server = threading.Thread(target=serve)
        server.start()
        transport = FailOnceTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            with self.assertRaises(ConnectionError):
                transport.wait_property(
                    "LX200 OnStep", "SAFE_MERIDIAN_FLIP", timeout=2.0
                )
            self.assertFalse(transport.is_open)
            with self.assertRaisesRegex(ConnectionError, "close the transport"):
                transport.connect()

            transport.close()
            transport.connect()
            prop = transport.wait_property(
                "LX200 OnStep", "SAFE_MERIDIAN_FLIP", timeout=2.0
            )
            self.assertEqual(prop.values["OFF"], "On")
            self.assertTrue(transport.is_open)
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()

        self.assertEqual(len(commands), 2)
        self.assertTrue(all("getProperties" in command for command in commands))
        self.assertFalse(any("DISCONNECT" in command for command in commands))

    def test_set_switch_is_idempotent_when_fresh_vector_is_already_target_ok(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        commands = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<defSwitchVector device="LX200 OnStep" name="TELESCOPE_TRACK_STATE" '
                    b'perm="rw" state="Ok"><defSwitch name="TRACK_ON">Off</defSwitch>'
                    b'<defSwitch name="TRACK_OFF">On</defSwitch></defSwitchVector>'
                )
                connection.settimeout(0.3)
                try:
                    commands.append(connection.recv(4096).decode())
                except socket.timeout:
                    pass

        server = threading.Thread(target=serve)
        server.start()
        transport = IndiTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            result = transport.set_switch(
                "LX200 OnStep", "TELESCOPE_TRACK_STATE", "TRACK_OFF"
            )
            self.assertEqual(result.values["TRACK_OFF"], "On")
            self.assertEqual(result.state, "Ok")
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()

        self.assertNotIn("newSwitchVector", "".join(commands))

    def test_finite_target_sends_ra_and_dec_atomically(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        commands = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<defNumberVector device="LX200 OnStep" name="EQUATORIAL_EOD_COORD" '
                    b'perm="rw" state="Ok"><defNumber name="RA">1</defNumber>'
                    b'<defNumber name="DEC">20</defNumber></defNumberVector>'
                )
                commands.append(connection.recv(4096).decode())

        server = threading.Thread(target=serve)
        server.start()
        transport = IndiTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            transport.issue_numbers(
                "LX200 OnStep", "EQUATORIAL_EOD_COORD", {"RA": 5.0, "DEC": 20.0}
            )
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()
        self.assertEqual(commands[1].count("newNumberVector"), 2)
        self.assertIn('<oneNumber name="RA">5</oneNumber>', commands[1])
        self.assertIn('<oneNumber name="DEC">20</oneNumber>', commands[1])

    def test_momentary_abort_accepts_ok_even_when_switch_resets_off(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        commands = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<defSwitchVector device="LX200 OnStep" name="TELESCOPE_ABORT_MOTION" '
                    b'perm="rw" state="Idle"><defSwitch name="ABORT">Off</defSwitch>'
                    b'</defSwitchVector>'
                )
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<setSwitchVector device="LX200 OnStep" name="TELESCOPE_ABORT_MOTION" '
                    b'state="Ok"><oneSwitch name="ABORT">Off</oneSwitch>'
                    b'</setSwitchVector>'
                )

        server = threading.Thread(target=serve)
        server.start()
        transport = IndiTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            result = transport.request_switch(
                "LX200 OnStep", "TELESCOPE_ABORT_MOTION", "ABORT"
            )
            self.assertEqual(result.state, "Ok")
            self.assertEqual(result.values["ABORT"], "Off")
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()
        self.assertIn("<oneSwitch name=\"ABORT\">On</oneSwitch>", commands[1])

    def test_text_and_number_writes_require_matching_ok_updates(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        commands = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<defTextVector device="LX200 OnStep" name="TIME_UTC" perm="rw" state="Idle">'
                    b'<defText name="UTC">old</defText><defText name="OFFSET">+2.00</defText>'
                    b'</defTextVector>'
                    b'<defNumberVector device="LX200 OnStep" name="GEOGRAPHIC_COORD" perm="rw" state="Idle">'
                    b'<defNumber name="LAT">0</defNumber><defNumber name="LONG">0</defNumber>'
                    b'<defNumber name="ELEV">0</defNumber></defNumberVector>'
                )
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<setTextVector device="LX200 OnStep" name="TIME_UTC" state="Ok">'
                    b'<oneText name="UTC">2026-09-22T10:00:00</oneText>'
                    b'<oneText name="OFFSET">+2.00</oneText></setTextVector>'
                )
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<setNumberVector device="LX200 OnStep" name="GEOGRAPHIC_COORD" state="Ok">'
                    b'<oneNumber name="LAT">50.336</oneNumber>'
                    b'<oneNumber name="LONG">8.533</oneNumber>'
                    b'<oneNumber name="ELEV">304</oneNumber></setNumberVector>'
                )

        server = threading.Thread(target=serve)
        server.start()
        transport = IndiTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            time_result = transport.set_text(
                "LX200 OnStep", "TIME_UTC",
                {"UTC": "2026-09-22T10:00:00", "OFFSET": "+2.00"},
            )
            site_result = transport.set_number(
                "LX200 OnStep", "GEOGRAPHIC_COORD",
                {"LAT": 50.336, "LONG": 8.533, "ELEV": 304.0},
            )
            self.assertEqual(time_result.state, "Ok")
            self.assertEqual(site_result.values["LONG"], "8.533")
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()
        self.assertFalse(server.is_alive())
        self.assertIn("newTextVector", commands[1])
        self.assertIn("newNumberVector", commands[2])
        self.assertFalse(any("DISCONNECT" in command for command in commands))

    def test_enables_switch_without_disconnect_or_motion_request(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        commands = []

        def serve():
            connection, _ = listener.accept()
            with connection:
                commands.append(connection.recv(4096).decode())
                connection.sendall(PROPERTY)
                commands.append(connection.recv(4096).decode())
                connection.sendall(
                    b'<setSwitchVector device="LX200 OnStep" name="SAFE_MERIDIAN_FLIP" state="Ok">'
                    b'<oneSwitch name="OFF">Off</oneSwitch>'
                    b'<oneSwitch name="ON">On</oneSwitch>'
                    b'</setSwitchVector>'
                )
                connection.settimeout(1)
                try:
                    commands.append(connection.recv(4096).decode())
                except socket.timeout:
                    pass

        server = threading.Thread(target=serve)
        server.start()
        transport = IndiTransport(port=listener.getsockname()[1])
        try:
            transport.connect()
            result = transport.set_switch("LX200 OnStep", "SAFE_MERIDIAN_FLIP", "ON")
            self.assertEqual(result.values["ON"], "On")
            self.assertEqual(result.state, "Ok")
        finally:
            transport.close()
            transport.close()
            server.join(timeout=3)
            listener.close()

        self.assertFalse(server.is_alive())
        self.assertIn("getProperties", commands[0])
        self.assertIn("SAFE_MERIDIAN_FLIP", commands[1])
        self.assertFalse(any("CONNECTION" in command or "TELESCOPE" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
