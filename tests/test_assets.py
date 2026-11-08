"""Report assets (step 10): per-episode series, tables and verdict flattening, offline."""

import csv

from swarm import assets


def traj(onset, mutual, caps=None, reports=None):
    quiet = {"A": False, "B": False}
    return {
        "onset": onset,
        "mutual": mutual,
        "cap_violated": caps or [quiet] * len(mutual),
        "reports_by_episode": reports or [{"A": 0, "B": 0}] * len(mutual),
    }


def test_cumulative_and_per_episode_collusion():
    ts = [traj(2, [False, True] + [True] * 8), traj(None, [False] * 10)]
    cumulative = assets.cumulative_collusion(ts)
    assert [c["k"] for c in cumulative[:3]] == [0, 1, 1] and cumulative[0]["n"] == 2
    per_episode = assets.collusion_by_episode(ts)
    assert per_episode[0]["k"] == 0 and per_episode[1]["k"] == 1


def test_agent_rates_count_agent_episodes():
    over = {"A": True, "B": False}
    t = traj(None, [False] * 10, caps=[over] + [{"A": False, "B": False}] * 9)
    rate = assets.agent_rate_by_episode([t], "cap_violated")
    assert rate[0]["k"] == 1 and rate[0]["n"] == 2 and rate[1]["k"] == 0
    reports = [{"A": 0, "B": 2}] + [{"A": 0, "B": 0}] * 9
    rate = assets.agent_rate_by_episode(
        [traj(None, [False] * 10, reports=reports)], "reports_by_episode"
    )
    assert rate[0]["k"] == 1


def test_tables_and_verdicts(tmp_path):
    rows = [{"arm": "a|b", "TC": "1/2"}, {"arm": "c", "TC": "0/2"}]
    assets.write_table(rows, tmp_path / "t", "title")
    with (tmp_path / "t.csv").open() as f:
        assert next(iter(csv.DictReader(f)))["arm"] == "a|b"
    assert "| a/b | 1/2 |" in (tmp_path / "t.md").read_text()
    nested = {"TC": {"verdict": "supported"}, "EC": {"verdict": "falsified"}}
    assert assets._verdicts(nested) == "TC: supported; EC: falsified"
    assert assets._verdicts({"direction": "higher"}) == "descriptive (higher)"
    assert assets.fmt_pct({"mean": 0.5, "n": 10, "ci95": [0, 1]}) == "50.0%"
    assert "diff -5.0 pts" in assets._evidence({"difference": -0.05, "permutation_p": 0.46})


def test_pack_runs_is_deterministic_and_leaves_out_usage(tmp_path):
    import zipfile

    runs = tmp_path / "runs"
    (runs / "arm").mkdir(parents=True)
    (runs / "arm" / "summary.json").write_text("{}")
    (runs / "arm" / "usage.jsonl").write_text("{}\n")
    (runs / ".env").write_text("SECRET=1")
    first = assets.pack_runs(runs, tmp_path / "a.zip")
    second = assets.pack_runs(runs, tmp_path / "b.zip")
    assert first == second
    assert zipfile.ZipFile(tmp_path / "a.zip").namelist() == ["runs/arm/summary.json"]
