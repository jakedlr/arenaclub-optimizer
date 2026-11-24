from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from arenaclub_optimizer.cli import app

runner = CliRunner()


def test_version_prints_package_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert version("arenaclub-optimizer") in result.output


@pytest.mark.parametrize("command", ["rank", "watch"])
def test_stub_command_exits_non_zero_with_not_implemented(command):
    result = runner.invoke(app, [command])

    assert result.exit_code != 0
    assert "not implemented yet" in result.output
