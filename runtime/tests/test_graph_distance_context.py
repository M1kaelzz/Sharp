"""P2 graph-distance context compression tests (batch P2).

Locks the focus-aware, graph-distance truncation in
``summarize_graph_yaml_by_distance``:

- **focus distance BFS**: facts close to the focus node (measured by walking
  intents backwards: an intent maps its ``from`` facts to its ``to`` fact, so
  ``to -> from`` is one hop toward the evidence that produced it) stay full
  length; facts farther than ``keep_hops`` are truncated with ``[truncated]``.
- **focus parametrization**: ``focus_ids`` names the node(s) distance is
  measured from (reason: ``goal``; explore: the intent's ``from`` ids) — this
  is the batch's mechanism win over the legacy time-based truncation.
- **origin/goal always kept** at full length regardless of distance.
- **graceful degradation**: unparseable / non-graph YAML and empty focus return
  the input unchanged (identical to the legacy fallback).
- **mutant gate**: dropping the ``dist > keep_hops`` truncation condition must
  fail the far-fact test.

Pure function test; no docker daemon, no live server.
"""

from __future__ import annotations

import yaml

from sharp.dispatcher.tasks.common import summarize_graph_yaml_by_distance


LONG = "x" * 500  # longer than the 300-char truncation threshold


def _build_graph(*, facts: list[tuple[str, str]], intents: list[dict]) -> str:
    """Build the same shape as export.py's _export_yaml output.

    facts: [(id, description), ...]; intents: {"from": [...], "to": "..."}.
    """
    data = {
        "project": {"title": "t", "origin": "o", "goal": "g"},
        "facts": [{"id": fid, "description": desc} for fid, desc in facts],
        "intents": intents,
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _descriptions(graph_yaml: str) -> dict[str, str]:
    data = yaml.safe_load(graph_yaml)
    return {f["id"]: f["description"] for f in data["facts"]}


def test_far_from_focus_is_truncated_near_kept():
    """A fact one hop from the focus stays full; facts far from it are truncated."""
    # graph: f3 --(i3)--> f2 --(i2)--> f1 --(i1)--> goal
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("f1", LONG), ("f2", LONG), ("f3", LONG)],
        intents=[
            {"from": ["f1"], "to": "goal"},
            {"from": ["f2"], "to": "f1"},
            {"from": ["f3"], "to": "f2"},
        ],
    )
    out = summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("goal",), keep_hops=1)
    desc = _descriptions(out)
    # f1 is distance 1 (<= keep_hops): kept full.
    assert desc["f1"] == LONG
    # f2 is distance 2, f3 distance 3: truncated.
    assert desc["f2"].endswith("[truncated]")
    assert desc["f3"].endswith("[truncated]")


def test_explore_focus_from_sources():
    """With focus=the intent's from facts, that chain stays full while facts off
    the chain (even near the goal) are truncated."""
    # goal chain: g1 --(ig)--> goal ; explore chain: e2 --> e1 --> goal
    # focus = ["e1"] => e1 (dist 0) and e2 (dist 1) stay full;
    # g1 (dist 1 from goal but not on the focus chain) is truncated.
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("g1", LONG), ("e1", LONG), ("e2", LONG)],
        intents=[
            {"from": ["g1"], "to": "goal"},
            {"from": ["e1"], "to": "goal"},
            {"from": ["e2"], "to": "e1"},
        ],
    )
    out = summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("e1",), keep_hops=2)
    desc = _descriptions(out)
    assert desc["e1"] == LONG  # distance 0
    assert desc["e2"] == LONG  # distance 1 (<= keep_hops 2)
    assert desc["g1"].endswith("[truncated]")  # distance 1 from goal but off focus chain
    assert desc["goal"] == "G"  # special node always kept
    assert desc["origin"] == "O"  # special node always kept


def test_origin_goal_always_kept_even_when_far():
    """origin/goal survive truncation regardless of distance/focus."""
    graph = _build_graph(
        facts=[("origin", LONG), ("goal", LONG), ("f1", LONG), ("f2", LONG)],
        intents=[
            {"from": ["f1"], "to": "goal"},
            {"from": ["f2"], "to": "f1"},
        ],
    )
    out = summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("f2",), keep_hops=0)
    desc = _descriptions(out)
    assert desc["origin"] == LONG
    assert desc["goal"] == LONG
    assert desc["f1"].endswith("[truncated]")  # distance 1 > keep_hops 0
    assert desc["f2"] == LONG  # distance 0


def test_below_hops_nothing_truncated():
    """If all facts are within keep_hops, no description is truncated."""
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("f1", LONG)],
        intents=[{"from": ["f1"], "to": "goal"}],
    )
    out = summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("goal",), keep_hops=5)
    assert _descriptions(out)["f1"] == LONG


def test_short_far_descriptions_not_truncated():
    """A far fact whose description is already short stays as-is (no marker)."""
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("f1", "short"), ("f2", "short")],
        intents=[
            {"from": ["f1"], "to": "goal"},
            {"from": ["f2"], "to": "f1"},
        ],
    )
    desc = _descriptions(
        summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("goal",), keep_hops=0)
    )
    assert desc["f1"] == "short"
    assert desc["f2"] == "short"


def test_malformed_yaml_returns_input():
    assert summarize_graph_yaml_by_distance(graph_yaml="not: :yaml[", focus_ids=("goal",)) == "not: :yaml["


def test_non_graph_yaml_returns_input():
    """A dict without facts/intents lists falls back to the input unchanged."""
    plain = yaml.dump({"project": {"title": "t"}}, allow_unicode=True)
    assert summarize_graph_yaml_by_distance(graph_yaml=plain, focus_ids=("goal",)) == plain


def test_empty_focus_returns_input():
    graph = _build_graph(
        facts=[("f1", LONG)],
        intents=[{"from": ["f1"], "to": "goal"}],
    )
    assert summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=()) == graph


def test_fact_with_no_path_truncated():
    """A fact with no path to the focus node is treated as infinitely far and
    truncated, while the focus chain survives."""
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("f1", LONG), ("isolated", LONG)],
        intents=[{"from": ["f1"], "to": "goal"}],
    )
    desc = _descriptions(
        summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("goal",), keep_hops=1)
    )
    assert desc["f1"] == LONG  # distance 1
    assert desc["isolated"].endswith("[truncated]")  # no path => truncated


def test_multiple_focus_ids_union():
    """Focus may be a set of ids (e.g. an intent's multiple from facts): all of
    them plus their upstream chains are kept."""
    graph = _build_graph(
        facts=[("origin", "O"), ("goal", "G"), ("a", LONG), ("b", LONG), ("ca", LONG)],
        intents=[
            {"from": ["a", "b"], "to": "goal"},
            {"from": ["ca"], "to": "a"},
        ],
    )
    out = summarize_graph_yaml_by_distance(graph_yaml=graph, focus_ids=("a", "b"), keep_hops=1)
    desc = _descriptions(out)
    assert desc["a"] == LONG  # distance 0
    assert desc["b"] == LONG  # distance 0
    assert desc["ca"] == LONG  # distance 1