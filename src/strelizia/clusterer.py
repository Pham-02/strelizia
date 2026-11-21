"""Group log records into clusters (probable root causes) using the Drain algorithm.

Grouping is local and deterministic: no network, no LLM. Variable parts of a
message (IPs, ids, numbers, timestamps) are masked so that
"connect ECONNREFUSED 10.0.0.5:5432" repeated 38,000 times is one cluster.

Each severity level, and each group an adapter names (nginx uses the HTTP status), gets
its own Drain tree, so a 404 can never be merged into a 502, nor a 502 into a 504, just
because the lines look alike.
"""

from __future__ import annotations

from typing import Iterable, Optional

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from .models import Cluster, LogRecord, level_rank

# Order matters: more specific patterns first.
_MASKS = [
    (r"\[\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]", "TS"),  # nginx access
    (r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?", "TS"),  # ISO-ish
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "UUID"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "IP"),
    (r"\b0x[0-9a-fA-F]+\b", "HEX"),
    (r"\b[0-9a-f]{12,}\b", "ID"),
    # Numbers, but keep an HTTP status code (right after a closing quote, as in a raw access
    # line, or after the words "HTTP " as in the nginx adapter's key) so 502 and 504 stay apart.
    (r'(?<![\w.])(?<!" )(?<!HTTP )-?\d+(?:\.\d+)?(?![\w.])', "NUM"),
]


def _make_config(sim_th: float, max_clusters: int) -> TemplateMinerConfig:
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = sim_th
    cfg.drain_depth = 4
    cfg.drain_max_children = 100
    cfg.drain_max_clusters = max_clusters  # bounded memory
    cfg.mask_prefix = "<"
    cfg.mask_suffix = ">"
    cfg.masking_instructions = [MaskingInstruction(p, name) for p, name in _MASKS]
    cfg.profiling_enabled = False
    return cfg


class Clusterer:
    def __init__(self, max_samples: int = 2, sim_th: float = 0.4, max_clusters: int = 5000):
        self._sim_th = sim_th
        self._max_clusters = max_clusters
        self._max_samples = max_samples
        self._miners: dict[tuple[str, str], TemplateMiner] = {}  # one Drain tree per (level, group)
        self._clusters: dict[tuple[str, str, int], Cluster] = {}
        self._next_id = 1
        # Exact-duplicate cache: an identical line always lands in the same cluster, so we
        # can skip masking and tree search. Capped so unique-heavy logs cannot grow it forever.
        self._seen: dict[tuple[str, str, str], Cluster] = {}
        self._seen_cap = 20_000

    def _miner_for(self, level: str, group: str) -> TemplateMiner:
        miner = self._miners.get((level, group))
        if miner is None:
            miner = TemplateMiner(None, _make_config(self._sim_th, self._max_clusters))
            self._miners[(level, group)] = miner
        return miner

    def add(self, record: LogRecord, context: Optional[Iterable[str]] = None) -> Optional[int]:
        sig = record.signature
        if not sig:
            return None
        level, group = record.level, record.group
        cached = self._seen.get((level, group, sig))
        if cached is not None:
            cached.observe(record, self._max_samples)
            return cached.id
        result = self._miner_for(level, group).add_log_message(sig)
        key = (level, group, result["cluster_id"])
        cluster = self._clusters.get(key)
        if cluster is None:
            cluster = Cluster(id=self._next_id, template=result["template_mined"], level=level)
            if context:
                cluster.context_before = [c[:600] for c in context]
            self._next_id += 1
            self._clusters[key] = cluster
        cluster.observe(record, self._max_samples)
        if len(self._seen) < self._seen_cap:
            self._seen[(level, group, sig)] = cluster
        return cluster.id

    def clusters(self) -> list[Cluster]:
        """All clusters, most important first: worst level, then most frequent."""
        for (level, group, cid), cluster in self._clusters.items():
            live = self._miners[(level, group)].drain.id_to_cluster.get(cid)
            if live is not None:  # templates get refined as more lines arrive
                cluster.template = live.get_template()
        return sorted(
            self._clusters.values(),
            key=lambda c: (level_rank(c.level), c.count),
            reverse=True,
        )
