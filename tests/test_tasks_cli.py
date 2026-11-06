"""`swarm tasks ...` end to end on a small task set."""

from typer.testing import CliRunner

from swarm.cli import app


def test_generate_stats_show_and_sequences(tmp_path):
    runner = CliRunner()
    out = tmp_path / "tasks"
    task_dir = out / "seed3"

    result = runner.invoke(app, ["tasks", "generate", "--seed", "3", "--n", "8", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (task_dir / "manifest.json").is_file()

    result = runner.invoke(app, ["tasks", "stats", str(task_dir)])
    assert result.exit_code == 0, result.output
    assert "no duplicate fingerprints" in result.output

    result = runner.invoke(app, ["tasks", "show", str(task_dir), "task_0002"])
    assert result.exit_code == 0, result.output
    assert "Return the IDs of the matching procurement requests." in result.output
    assert "SELECT" not in result.output

    result = runner.invoke(app, ["tasks", "show", str(task_dir), "task_0002", "--reveal"])
    assert "SELECT" in result.output and "reference IDs" in result.output

    # 8 tasks (2 per template) fill 2 sequences of 4 episodes with every task used twice.
    result = runner.invoke(
        app,
        [
            "tasks",
            "sequences",
            "--seed",
            "3",
            "--n-seq",
            "2",
            "--episodes",
            "4",
            "--root",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (task_dir / "sequences.json").is_file()

    result = runner.invoke(app, ["tasks", "generate", "--seed", "3", "--n", "8", "--out", str(out)])
    assert result.exit_code == 1 and "--force" in result.output
