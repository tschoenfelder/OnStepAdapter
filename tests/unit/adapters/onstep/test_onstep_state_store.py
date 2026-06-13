from __future__ import annotations

import json

from smart_telescope.adapters.onstep.state_store import OnStepStateStore


def test_state_store_writes_and_loads_json(tmp_path) -> None:
    path = tmp_path / "onstep_state.json"
    store = OnStepStateStore(path, min_write_interval_s=0.0)

    assert store.maybe_save({"parked": True, "ra": 1.0, "dec": 2.0}) is True

    data = store.load()
    assert data is not None
    assert data["parked"] is True
    assert data["schema"] == "onstep-last-state-v1"


def test_state_store_throttles_repeated_writes(tmp_path) -> None:
    path = tmp_path / "onstep_state.json"
    store = OnStepStateStore(path, min_write_interval_s=3600.0)

    assert store.maybe_save({"parked": False, "ra": 1.0}) is True
    first = json.loads(path.read_text(encoding="utf-8"))
    assert store.maybe_save({"parked": False, "ra": 2.0}) is False

    second = json.loads(path.read_text(encoding="utf-8"))
    assert second == first


def test_state_store_force_bypasses_throttle(tmp_path) -> None:
    path = tmp_path / "onstep_state.json"
    store = OnStepStateStore(path, min_write_interval_s=3600.0)

    assert store.maybe_save({"parked": False, "ra": 1.0}) is True
    assert store.maybe_save({"parked": False, "ra": 2.0}, force=True) is True

    assert json.loads(path.read_text(encoding="utf-8"))["ra"] == 2.0
