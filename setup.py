"""Selective wheel build for the standalone OnStep adapter."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class SelectiveBuildPy(build_py):
    _allowed_modules = {
        "onstep_adapter": {
            "__init__",
            "indi_client",
            "indi_axis_motion",
            "indi_config",
            "indi_gu",
            "indi_transport",
            "indi_status",
            "indi_stop",
            "indi_tracking",
            "indi_home",
            "indi_focuser",
            "indi_guiding",
            "indi_meridian",
            "indi_mount",
            "indi_motion",
            "indi_trace",
            "meridian_policy",
        },
        # Passive traffic capture (CaptureProxy, pcap import, analysis).
        "onstep_adapter.capture": {"__init__", "analysis", "pcap", "proxy", "stream"},
        "onstep_adapter.tools": {
            "__init__",
            "indi_focuser_roundtrip",
            "indi_axis_angle_smoke",
            "indi_capture",
            "trace_compare",
            "trace_consolidate",
        },
        # The virtual OnStepX mount behind a local INDI server, for application tests
        # and an application's offline mode.
        "onstep_mock": {"__init__", "mount", "pytest_plugin", "replay", "server", "testing", "validate"},
    }

    def run(self) -> None:
        root = Path(__file__).resolve().parent
        build_path = Path(self.build_lib).resolve()
        if build_path == root or not build_path.is_relative_to(root):
            raise RuntimeError(f"Refusing to remove build output outside {root}")
        shutil.rmtree(build_path, ignore_errors=True)
        super().run()

    def find_package_modules(
        self,
        package: str,
        package_dir: str,
    ) -> list[tuple[str, str, str]]:
        modules = super().find_package_modules(package, package_dir)
        allowed = self._allowed_modules.get(package, set())
        return [module for module in modules if module[1] in allowed]


setup(cmdclass={"build_py": SelectiveBuildPy})
