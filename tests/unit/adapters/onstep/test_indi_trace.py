import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from onstep_adapter.indi_client import OnStepIndiClient
from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.indi_mount import IndiMount
from onstep_adapter.indi_trace import TraceRecorder
from onstep_adapter.indi_transport import IndiTransport
from onstep_adapter.tools import trace_compare

DEVICE = "LX200 OnStep"
PROPERTY = (
    b'<defSwitchVector device="LX200 OnStep" name="TELESCOPE_ABORT_MOTION" '
    b'perm="rw" state="Idle"><defSwitch name="ABORT">Off</defSwitch></defSwitchVector>'
)
ABORT_OK = (
    b'<setSwitchVector device="LX200 OnStep" name="TELESCOPE_ABORT_MOTION" state="Ok">'
    b'<oneSwitch name="ABORT">Off</oneSwitch></setSwitchVector>'
)


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class TraceRecorderTests(unittest.TestCase):
    def test_records_session_send_and_decoded_status(self):
        stream = io.StringIO()
        trace = TraceRecorder(stream, source="sim", label="unit")
        trace.sent(
            '<newSwitchVector device="LX200 OnStep" name="TELESCOPE_PARK">'
            '<oneSwitch name="UNPARK">On</oneSwitch></newSwitchVector>'
        )
        trace.received(
            device=DEVICE, name="OnStep Status", kind="Text", state="Ok",
            changed={":GU# return": "nNpEW060"}, values={":GU# return": "nNpEW060"},
            revision=7, tag="setTextVector",
        )
        session, send, recv = lines(stream)
        self.assertEqual(session["type"], "session")
        self.assertEqual(session["source"], "sim")
        self.assertEqual([session["seq"], send["seq"], recv["seq"]], [1, 2, 3])
        self.assertEqual(send["property"], "TELESCOPE_PARK")
        self.assertEqual(send["values"], {"UNPARK": "On"})
        self.assertEqual(recv["gu"]["motion_state"], "unparked")
        self.assertFalse(recv["gu"]["tracking"])
        self.assertEqual(recv["gu"]["pier_side"], "west")

    def test_write_failure_never_raises(self):
        class Broken(io.StringIO):
            def write(self, _):
                raise OSError("disk full")

        trace = TraceRecorder(Broken())
        trace.record("send", property="TELESCOPE_ABORT_MOTION")
        self.assertEqual(trace.write_failures, 2)


class TransportTraceTests(unittest.TestCase):
    def test_send_is_logged_before_its_reply(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(PROPERTY)
                connection.recv(4096)
                connection.sendall(ABORT_OK)
                connection.recv(4096)

        server = threading.Thread(target=serve)
        server.start()
        stream = io.StringIO()
        transport = IndiTransport(port=listener.getsockname()[1], trace=TraceRecorder(stream))
        try:
            transport.connect()
            transport.request_switch(DEVICE, "TELESCOPE_ABORT_MOTION", "ABORT", timeout=3)
        finally:
            transport.close()
            server.join(timeout=3)
            listener.close()
        records = lines(stream)
        kinds = [(r["type"], r.get("property")) for r in records]
        self.assertEqual(kinds[:6], [
            ("session", None),
            ("connect", None),
            ("send", None),
            ("recv", "TELESCOPE_ABORT_MOTION"),
            ("send", "TELESCOPE_ABORT_MOTION"),
            ("recv", "TELESCOPE_ABORT_MOTION"),
        ])
        self.assertEqual(records[5]["state"], "Ok")
        self.assertEqual(records[-1]["type"], "close")


class MountTraceTests(unittest.TestCase):
    def test_public_calls_are_framed_with_result_or_error(self):
        stream = io.StringIO()

        class Client:
            trace = TraceRecorder(stream)

            def emergency_stop(self):
                return {"stopped_confirmed": True}

            def go_home(self):
                raise RuntimeError("HOME disabled")

        mount = IndiMount(Client())
        mount.stop()
        with self.assertRaises(RuntimeError):
            mount.go_home()
        records = lines(stream)[1:]
        self.assertEqual([(r["type"], r["op"]) for r in records], [
            ("call", "stop"), ("return", "stop"), ("call", "go_home"), ("error", "go_home"),
        ])
        self.assertEqual(records[1]["result"], {"stopped_confirmed": True})
        self.assertEqual(records[3]["error_type"], "RuntimeError")

    def test_mount_without_trace_is_unchanged(self):
        class Client:
            def emergency_stop(self):
                return "stopped"

        self.assertEqual(IndiMount(Client()).stop(), "stopped")


class ConfigTraceTests(unittest.TestCase):
    def test_trace_path_from_config_creates_utc_named_file(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "config.toml"
            example = Path("config.indi.example.toml").read_text(encoding="utf-8")
            config_path.write_text(
                example.replace(
                    '# path = "~/.local/state/onstep_adapter/trace-{utc}.jsonl"',
                    f'path = "{folder}/trace-{{utc}}.jsonl"',
                ),
                encoding="utf-8",
            )
            config = load_indi_config(config_path)
            client = OnStepIndiClient(config=config, transport=IndiTransport())
            try:
                self.assertIsNotNone(client.trace)
                self.assertIs(client._transport.trace, client.trace)
                self.assertRegex(client.trace.path.name, r"^trace-\d{8}T\d{6}Z\.jsonl$")
            finally:
                client.trace.close()

    def test_example_config_has_tracing_off(self):
        self.assertIsNone(load_indi_config("config.indi.example.toml").trace_path)


def rec(seq, t, kind, **fields):
    return {"seq": seq, "t": t, "thread": "main", "type": kind, **fields}


def status(seq, t, raw):
    flags = set(raw)
    return rec(seq, t, "recv", property="OnStep Status", state="Ok", values={":GU# return": raw},
               gu={"motion_state": "slewing" if "N" not in flags else (
                   "tracking" if "n" not in flags else "unparked"),
                   "tracking": "n" not in flags, "slewing": "N" not in flags})


def go_home_trace(slew_seconds, *, tracking_after=False):
    end = 1.0 + slew_seconds
    final = "NpEW060" if tracking_after else "nNpEW060"
    return [
        rec(1, 0.0, "session"),
        status(2, 0.1, "nNpEW060"),
        rec(3, 0.5, "call", call_id=1, op="go_home", args=[], kwargs={}),
        rec(4, 0.6, "send", tag="newSwitchVector", property="TELESCOPE_HOME", values={"GO": "On"}),
        status(5, 1.0, "npEW060"),
        rec(6, 1.1, "recv", property="EQUATORIAL_EOD_COORD", state="Busy", values={"RA": "3", "DEC": "50"}),
        status(7, end, final),
        rec(8, end + 0.1, "recv", property="EQUATORIAL_EOD_COORD", state="Idle", values={"RA": "20", "DEC": "90"}),
        rec(9, end + 0.2, "return", call_id=1, op="go_home", duration_s=end - 0.3,
            result={"reached": True, "error": None}),
        rec(10, end + 0.5, "call", call_id=2, op="stop", args=[], kwargs={}),
        rec(11, end + 0.6, "return", call_id=2, op="stop", duration_s=0.1,
            result={"stopped_confirmed": True}),
    ]


class TraceCompareTests(unittest.TestCase):
    def test_handle_move_takes_outcome_from_its_motion_end_record(self):
        records = [
            rec(1, 0.0, "session"),
            rec(2, 0.5, "call", call_id=1, op="go_home", args=[], kwargs={}),
            rec(3, 0.51, "motion_start", kind="home"),
            rec(4, 0.52, "return", call_id=1, op="go_home", duration_s=0.02,
                result="MotionHandle(kind='home', state='running')"),
            rec(5, 1.5, "call", call_id=2, op="stop", args=[], kwargs={}),
            rec(6, 1.6, "motion_end", kind="home", state="stopped", duration_s=1.09,
                error="home move was stopped before it finished", result=None),
            rec(7, 2.0, "return", call_id=2, op="stop", duration_s=0.5,
                result={"stopped_confirmed": True}),
        ]
        home, stop = trace_compare.build_operations(records)
        self.assertEqual((home.outcome, home.duration_s), ("motion:stopped", 1.09))
        self.assertEqual(stop.outcome, "return")
        self.assertEqual(stop.result_flags, {"stopped_confirmed": True})

    def test_operations_capture_writes_states_and_timing(self):
        home, stop = trace_compare.build_operations(go_home_trace(4.0))
        self.assertEqual(home.op, "go_home")
        self.assertEqual(home.sends, ["TELESCOPE_HOME:GO=On"])
        self.assertEqual(home.motion_states, ["unparked", "slewing", "unparked"])
        self.assertEqual(home.coord_states, ["Busy", "Idle"])
        self.assertEqual(home.result_flags, {"reached": True})
        self.assertAlmostEqual(home.motion_start_s, 0.5)
        self.assertAlmostEqual(home.motion_end_s, 4.5)
        self.assertEqual(home.final_dec_deg, 90.0)
        self.assertEqual(stop.result_flags, {"stopped_confirmed": True})

    def test_same_behavior_slower_mount_is_a_timing_warning_only(self):
        report = trace_compare.compare(
            trace_compare.build_operations(go_home_trace(4.0)),
            trace_compare.build_operations(go_home_trace(12.0)),
        )
        self.assertTrue(report["match"])
        self.assertTrue(any("motion_end_s" in line for line in report["warnings"]))

    def test_tracking_left_on_is_a_behavior_mismatch(self):
        report = trace_compare.compare(
            trace_compare.build_operations(go_home_trace(4.0)),
            trace_compare.build_operations(go_home_trace(4.0, tracking_after=True)),
        )
        self.assertFalse(report["match"])
        self.assertTrue(any(
            "motion_states" in line and "tracking" in line for line in report["mismatches"]
        ))
        self.assertGreater(report["observed"][1]["tracking_on_s"], 0.0)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for name, records in (("a", go_home_trace(4.0)), ("b", go_home_trace(4.0, tracking_after=True))):
                path = Path(folder) / f"{name}.jsonl"
                path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
                paths.append(str(path))
            self.assertEqual(trace_compare.main(["summary", paths[0]]), 0)
            self.assertEqual(trace_compare.main(["diff", paths[0], paths[0]]), 0)
            self.assertEqual(trace_compare.main(["diff", paths[0], paths[1]]), 1)


if __name__ == "__main__":
    unittest.main()
