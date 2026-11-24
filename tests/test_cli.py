from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from arenaclub_optimizer.cli import app

runner = CliRunner()


def test_version_prints_package_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == version("arenaclub-optimizer") + "\n"


@pytest.mark.parametrize("command", ["rank", "watch"])
def test_stub_command_exits_non_zero_with_not_implemented(command):
    result = runner.invoke(app, [command])

    assert result.exit_code != 0
    assert result.stderr == f"arena {command}: not implemented yet\n"
