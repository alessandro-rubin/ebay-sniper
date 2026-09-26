import pytest

from ebay_sniper.cli import build_parser, main


def test_version_flag_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])
    assert exc_info.value.code == 0


def test_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("command", ["run-once", "watch", "check-config"])
def test_unimplemented_commands_return_error(command: str) -> None:
    assert main([command]) == 1
