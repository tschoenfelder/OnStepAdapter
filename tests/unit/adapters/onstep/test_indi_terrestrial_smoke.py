from unittest.mock import patch

from onstep_adapter.tools.indi_terrestrial_smoke import _confirm


def test_confirmation_rejects_commands_until_empty_enter():
    with patch("builtins.input", side_effect=["w", "p", ""]), patch(
        "builtins.print"
    ) as printed:
        assert _confirm("confirm> ") is True

    assert printed.call_count == 2


def test_confirmation_accepts_typed_escape_without_action():
    with patch("builtins.input", return_value="esc"):
        assert _confirm("confirm> ") is False
