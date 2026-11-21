"""The v0.1 analysis pipeline: lines -> records -> clusters."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Union

from .assembler import assemble
from .formats import ALL_FORMATS
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
    formats: frozenset = ALL_FORMATS,
) -> tuple[list[Cluster], Stats]:
    stats = Stats()

    def counted() -> Iterable[Union[RawLine, str]]:
        for line in lines:
            stats.lines += 1
            yield line

    threshold = level_rank(min_level)
    clusterer = Clusterer(max_samples=max_samples)
    recent: deque[str] = deque(maxlen=2)  # the records just before the current one
    for record in assemble(counted(), source=source, formats=formats):
        stats.records += 1
        if level_rank(record.level) >= threshold:
            clusterer.add(record, recent)
        else:
            stats.skipped += 1
        recent.append(record.message)

    clusters = clusterer.clusters()
    stats.clusters = len(clusters)
    return clusters, stats
