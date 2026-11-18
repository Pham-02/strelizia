from strelizia.clusterer import Clusterer
from strelizia.models import LogRecord


def rec(msg, level="ERROR"):
    return LogRecord(message=msg, level=level)


def test_thousands_of_similar_lines_collapse_to_one_cluster():
    c = Clusterer()
    for i in range(1000):
        c.add(rec(f"ERROR connect ECONNREFUSED 10.0.{i % 250}.{i % 200}:5432"))
    clusters = c.clusters()
    assert len(clusters) == 1
    assert clusters[0].count == 1000
    assert "<IP>" in clusters[0].template


def test_different_problems_stay_separate():
    c = Clusterer()
    for i in range(50):
        c.add(rec(f"ERROR connect ECONNREFUSED 10.0.0.{i}:5432"))
        c.add(rec(f"ERROR disk quota exceeded for user {i}"))
    assert len(c.clusters()) == 2


def test_worst_level_sorts_first_even_if_less_frequent():
    c = Clusterer()
    for i in range(100):
        c.add(rec(f"WARN slow query took {i}ms", "WARN"))
    for i in range(3):
        c.add(rec(f"FATAL out of memory killing process {i}", "FATAL"))
    top = c.clusters()[0]
    assert top.level == "FATAL"


def test_levels_never_merge():
    c = Clusterer()
    for i in range(20):
        c.add(rec(f'1.2.3.4 - - [x] "GET /a/{i} HTTP/1.1" 502 100', "ERROR"))
        c.add(rec(f'1.2.3.4 - - [x] "GET /a/{i} HTTP/1.1" 404 100', "WARN"))
    levels = sorted(cl.level for cl in c.clusters())
    assert levels == ["ERROR", "WARN"]


def test_http_status_is_preserved_in_template():
    c = Clusterer()
    for i in range(20):
        c.add(rec(f'1.2.3.4 - - [x] "GET /a/{i} HTTP/1.1" 502 100', "ERROR"))
    assert " 502 " in c.clusters()[0].template


def test_samples_are_unique_and_capped():
    c = Clusterer(max_samples=2)
    for i in range(10):
        c.add(rec(f"ERROR job {i} failed"))  # ten different messages, one pattern
        c.add(rec("ERROR job 0 failed"))  # plus exact duplicates
    (cluster,) = c.clusters()
    assert cluster.count == 20
    assert len(cluster.samples) == 2
    assert len(set(cluster.samples)) == 2


def test_empty_signature_is_ignored():
    c = Clusterer()
    assert c.add(rec("")) is None
    assert c.clusters() == []
