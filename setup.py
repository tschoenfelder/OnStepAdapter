"""Selective wheel build for the standalone OnStep adapter."""

from __future__ import annotations

import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class SelectiveBuildPy(build_py):
    _allowed_modules = {
        "onstep_adapter": {"__init__"},
        "onstep_adapter.tools": {
            "__init__",
            "axis_motion_smoke",
            "coordinate_axis_smoke",
        },
        "smart_telescope": {"__init__"},
        "smart_telescope.adapters": {"__init__"},
        "smart_telescope.adapters.onstep": {
            "__init__",
            "client",
            "focuser",
            "firmware_proof",
            "mount",
            "results",
            "safety",
            "serial_bus",
            "state_store",
        },
        "smart_telescope.ports": {"__init__", "focuser", "mount"},
    }

    def run(self) -> None:
        shutil.rmtree(self.build_lib, ignore_errors=True)
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
