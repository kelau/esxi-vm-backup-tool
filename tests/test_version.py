from typer.testing import CliRunner

from esxi_backup import __version__
from esxi_backup.cli import app


def test_version_is_exposed_by_cli():
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__ == "0.7.3"
