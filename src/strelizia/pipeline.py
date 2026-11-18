"""The v0.1 analysis pipeline: lines -> records -> clusters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Union

from .assembler import assemble
from .clusterer import Clusterer
from .models import Cluster, RawLine, level_rank


@dataclass
class Stats:
    lines: int = 0  # physical lines read
    records: int = 0  # logical records after multi-line assembly
    skipped: int = 0  # records below the minimum level
    clusters: int = 0  # clusters found


def analyze_lines(
    lines: Iterable[Union[RawLine, str]],
    *,
    source: str = "",
    min_level: str = "WARN",
    max_samples: int = 2,
) -> tuple[list[Cluster], Stats]:
    stats = Stats()

    def counted() -> Iterable[Union[RawLine, str]]:
        for line in lines:
            stats.lines += 1
            yield line

    threshold = level_rank(min_level)
    clusterer = Clusterer(max_samples=max_samples)
    for record in assemble(counted(), source=source):
        stats.records += 1
        if level_rank(record.level) < threshold:
            stats.skipped += 1
            continue
        clusterer.add(record)

    clusters = clusterer.clusters()
    stats.clusters = len(clusters)
    return clusters, stats
