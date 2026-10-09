"""`luna research-v3` CLI surface (fixed --out for controller resumes; never a holdout command)."""

from typer.testing import CliRunner

from luna.cli import app


def test_research_v3_help_lists_the_run_options():
    out = CliRunner().invoke(app, ["research-v3", "--help"], terminal_width=200)
    assert out.exit_code == 0
    for opt in ("--account", "--jobs", "--out", "--resume"):
        assert opt in out.output
