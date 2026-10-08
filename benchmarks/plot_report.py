#!/usr/bin/env python3
"""Static scientific plots derived only from the published CSV performance tables."""

import csv
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/report"
plt.rcParams.update(
    {
        "font.size": 11,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "savefig.dpi": 160,
        "svg.hashsalt": "retention-report",
    }
)
COLORS = ["#245A81", "#138580", "#BA6C21", "#7C589B"]


def main():
    rows = list(csv.DictReader((OUT / "retirement-summary.csv").open()))
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.7), sharey=True)
    configurations = [
        ("postgres:17", 4096, "Local PG 17 · 4,096 parents"),
        ("postgres:17", 32768, "Local PG 17 · 32,768 parents"),
        ("aurora", 4096, "Aurora 17.9 · 4,096 parents"),
    ]
    modes = ["delete", "partition", "split_daily", "copy"]
    labels = ["DELETE", "Partition", "Payload\nsplit", "Copy\nkeepers"]
    for ax, (target, count, title) in zip(axes, configurations):
        selected = {
            r["mode"]: r
            for r in rows
            if r["target"] == target
            and int(r["parent_rows"]) == count
            and r["profile"] == "typical"
            and r["batch_size"] == "256"
            and r["fk_indexes"] == "True"
        }
        if not set(modes) <= selected.keys():
            raise ValueError(
                "Missing final daily comparison; finish experiments before plotting"
            )
        values = [float(selected[m]["server_seconds_median"]) for m in modes]
        lower = [
            v - float(selected[m]["server_seconds_min"]) for v, m in zip(values, modes)
        ]
        upper = [
            float(selected[m]["server_seconds_max"]) - v for v, m in zip(values, modes)
        ]
        ax.bar(
            labels,
            values,
            color=COLORS,
            alpha=0.9,
            yerr=[lower, upper],
            capsize=4,
            width=0.62,
        )
        ax.set_title(title, fontsize=12, pad=14)
        ax.set_yscale("log")
        ax.set_ylim(0.012, 2.0)
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)
        for i, v in enumerate(values):
            ax.text(
                i,
                v * 0.73,
                f"{v:.3f}",
                ha="center",
                va="top",
                fontsize=10,
                bbox={
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.85,
                    "pad": 1,
                },
            )
    axes[0].set_ylabel("Server retirement seconds · logarithmic scale")
    fig.suptitle(
        "Retirement cost changes with scale and partition layout", fontsize=16, y=0.99
    )
    fig.text(
        0.5,
        0.01,
        "Medians and full observed range, 3 trials. Typical profile; 80% expired. Archive, setup and commit time excluded.\nDaily payload split uses 45 expired leaves; composite design retires 135 leaves. Hardware differs across local/Aurora.",
        ha="center",
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0.12, 1, 0.94))
    fig.savefig(OUT / "retirement-comparison.png")
    fig.savefig(OUT / "retirement-comparison.svg", metadata={"Date": None})
    plt.close(fig)
    rows = list(csv.DictReader((OUT / "query-matrix.csv").open()))
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)
    shapes = [
        ("id", "ID only"),
        ("id_timestamp", "ID + timestamp constant"),
        ("id_timestamp_expression", "ID + runtime timestamp"),
        ("external_indexed", "UUID indexed"),
    ]
    for ax, target in zip(axes, ["postgres:16", "postgres:17"]):
        for (shape, label), color in zip(shapes, COLORS):
            selected = sorted(
                [r for r in rows if r["target"] == target and r["shape"] == shape],
                key=lambda r: int(r["partitions"]),
            )
            ax.plot(
                [int(r["partitions"]) for r in selected],
                [float(r["p95_ms"]) for r in selected],
                label=label,
                color=color,
                marker="o",
                linewidth=2,
            )
        ax.set_title("Local PostgreSQL " + target.split(":")[1])
        ax.set_xlabel("Partitions (0 = unpartitioned)")
        ax.set_yscale("log")
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("Client lookup p95 ms · logarithmic scale")
    axes[1].legend(fontsize=9)
    fig.suptitle(
        "Partition predicates matter for narrow-key lookups", fontsize=16, y=0.99
    )
    fig.text(
        0.5,
        0.015,
        "2,000,000 narrow ID/timestamp/UUID rows. Five warmups, 30 measurements per shape.\nThese are not full HTML reads or production request-latency forecasts.",
        ha="center",
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0.12, 1, 0.93))
    fig.savefig(OUT / "query-sensitivity.png")
    fig.savefig(OUT / "query-sensitivity.svg", metadata={"Date": None})
    plt.close(fig)

    for name in ("retirement-comparison.svg", "query-sensitivity.svg"):
        path = OUT / name
        path.write_text(
            "\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n"
        )


if __name__ == "__main__":
    main()
