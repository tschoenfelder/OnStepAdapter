import socket
import threading
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
