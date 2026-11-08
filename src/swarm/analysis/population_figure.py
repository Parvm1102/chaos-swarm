"""Experiment 4's conversion-curve figure (`swarm population --figure`): for every arm, the
share of real agents converted by each episode (mean over groups, 95% bootstrap interval over
groups as a band), and beside it the population collusion rate (PCR) the same way. 300 dpi PNG.
"""

from pathlib import Path
from typing import Any

ORDER = (
    "p_explicit_k0",
    "p_explicit_k1",
    "p_explicit_k2",
    "p_explicit_board_k0",
    "p_explicit_board_k1",
    "p_explicit_board_k2",
    "p_paper_board_k0",
)
STYLE = {  # colour by k, dashed without the board, solid with it, grey for the paper ceiling
    "p_explicit_k0": ("#4C72B0", "--"),
    "p_explicit_k1": ("#DD8452", "--"),
    "p_explicit_k2": ("#C44E52", "--"),
    "p_explicit_board_k0": ("#4C72B0", "-"),
    "p_explicit_board_k1": ("#DD8452", "-"),
    "p_explicit_board_k2": ("#C44E52", "-"),
    "p_paper_board_k0": ("#7F7F7F", "-"),
}


def conversion_figure(arms: dict[str, dict[str, Any]], out: Path) -> Path:
    """Draw the two panels from the arm summaries in a population report; return the path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharex=True)
    for name in ORDER:
        if name not in arms:
            continue
        colour, line = STYLE[name]
        for ax, key in zip(axes, ("converted_by_episode", "pcr_by_episode"), strict=True):
            points = arms[name][key]
            xs = [e + 1 for e, p in enumerate(points) if p["mean"] is not None]
            mean = [p["mean"] for p in points if p["mean"] is not None]
            low = [p["ci95"][0] for p in points if p["mean"] is not None]
            high = [p["ci95"][1] for p in points if p["mean"] is not None]
            ax.plot(xs, mean, line, color=colour, label=name, linewidth=1.6)
            ax.fill_between(xs, low, high, color=colour, alpha=0.12, linewidth=0)
    axes[0].set_title("Real agents converted by episode")
    axes[1].set_title("Population collusion rate (RR + RS pairs)")
    for ax in axes:
        ax.set_xlabel("episode")
        ax.set_ylim(-0.02, 1.02)
        ax.set_xticks(range(1, 11))
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("share (mean over groups)")
    axes[1].legend(fontsize=7, loc="upper left")
    fig.text(
        0.5,
        0.01,
        "Lines: mean over the arm's groups. Bands: 95% bootstrap intervals over groups "
        "(10,000 resamples). Dashed: no board; solid: board; grey: paper wording.",
        ha="center",
        fontsize=7,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=300)
    plt.close(fig)
    return out
