"""M3 — extended-scenario tests for phionyx-openai-agents.

These cover production-shaped failure modes that the M2 baseline tests
do not exercise:
- Multi-span trees with parent_id chains (the realistic SDK shape).
- Error-bearing spans (span.error attribute set).
- Long chains (100+ envelopes) — hash integrity at scale.
- Lifecycle: trace_end after shutdown still produces a valid envelope.
- Concurrent callbacks from multiple threads (cross-thread safety).
- ``_events`` and ``envelopes`` lists stay aligned 1:1.
- JSONL round-trip preserves the chain bit-exactly.

The real OpenAI Agents SDK is not installed locally; Trace/Span are
mocked via plain Python classes matching the documented attribute set.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest


def _make_processor(tmp_path: Path, trace_id: str | None = None):
    from phionyx_openai_agents import FilesystemEnvelopeStore, PhionyxTracingProcessor

    return PhionyxTracingProcessor(
        trace_id=trace_id, store=FilesystemEnvelopeStore(root=tmp_path)
    )


class _FakeTrace:
    def __init__(self, trace_id: str, workflow_name: str = "demo") -> None:
        self.trace_id = trace_id
        self.name = "trace"
        self.workflow_name = workflow_name
        self.group_id = None
        self.metadata = {}


class _FakeSpan:
    def __init__(
        self,
        span_id: str,
        trace_id: str,
        parent_id: str | None = None,
        name: str = "span",
        error: BaseException | None = None,
    ) -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.name = name
        self.started_at = "2026-05-23T00:00:00Z"
        self.ended_at = "2026-05-23T00:00:01Z"
        self.error = error
        self.span_data = None


# ---------------------------------------------------------------------------
# Multi-span tree — realistic Agents SDK shape
# ---------------------------------------------------------------------------


def test_multi_span_tree_preserves_parent_id_in_payload(tmp_path: Path) -> None:
    """A tree of root → child → grandchild spans preserves parent_id linkage in the envelope payloads."""
    p = _make_processor(tmp_path)
    trace = _FakeTrace("tr-tree")
    root = _FakeSpan("sp-root", "tr-tree", parent_id=None)
    child = _FakeSpan("sp-child", "tr-tree", parent_id="sp-root")
    grand = _FakeSpan("sp-grand", "tr-tree", parent_id="sp-child")

    p.on_trace_start(trace)
    p.on_span_start(root)
    p.on_span_start(child)
    p.on_span_start(grand)
    p.on_span_end(grand)
    p.on_span_end(child)
    p.on_span_end(root)
    p.on_trace_end(trace)

    # Each span_start envelope payload carries the parent_id we set.
    span_starts = [e for e in p.envelopes if e["subject"]["event_type"] == "span_start"]
    assert len(span_starts) == 3
    parent_ids = [e["message"]["payload"]["data"]["parent_id"] for e in span_starts]
    assert parent_ids == [None, "sp-root", "sp-child"]

    # The chain still verifies end-to-end.
    assert p.verify_chain()["ok"] is True


def test_sibling_spans_share_parent_id(tmp_path: Path) -> None:
    """Sibling spans (two children of the same parent) both carry the parent's id."""
    p = _make_processor(tmp_path)
    trace = _FakeTrace("tr-siblings")
    root = _FakeSpan("sp-root", "tr-siblings", parent_id=None)
    sib1 = _FakeSpan("sp-sib1", "tr-siblings", parent_id="sp-root")
    sib2 = _FakeSpan("sp-sib2", "tr-siblings", parent_id="sp-root")

    p.on_trace_start(trace)
    p.on_span_start(root)
    p.on_span_start(sib1)
    p.on_span_end(sib1)
    p.on_span_start(sib2)
    p.on_span_end(sib2)
    p.on_span_end(root)
    p.on_trace_end(trace)

    span_starts = [e for e in p.envelopes if e["subject"]["event_type"] == "span_start"]
    sibling_parents = [
        e["message"]["payload"]["data"]["parent_id"]
        for e in span_starts
        if e["message"]["payload"]["data"]["span_id"] in {"sp-sib1", "sp-sib2"}
    ]
    assert sibling_parents == ["sp-root", "sp-root"]


# ---------------------------------------------------------------------------
# Error-bearing spans
# ---------------------------------------------------------------------------


def test_error_bearing_span_records_error_string_in_payload(tmp_path: Path) -> None:
    """span.error is serialized to a string in the envelope payload."""
    p = _make_processor(tmp_path)
    err = RuntimeError("tool dispatch failed")
    span = _FakeSpan("sp-err", "tr-1", error=err)

    p.on_span_end(span)

    env = p.envelopes[0]
    assert env["message"]["payload"]["data"]["error"] == "tool dispatch failed"


def test_no_error_serializes_as_none(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    span = _FakeSpan("sp-ok", "tr-1", error=None)

    p.on_span_end(span)

    env = p.envelopes[0]
    assert env["message"]["payload"]["data"]["error"] is None


# ---------------------------------------------------------------------------
# Long chain — hash integrity at scale
# ---------------------------------------------------------------------------


def test_hundred_event_chain_verifies(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-long"))
    for i in range(98):
        p.on_span_start(_FakeSpan(f"sp-{i}", "tr-long"))
    p.on_trace_end(_FakeTrace("tr-long"))

    assert len(p.envelopes) == 100
    report = p.verify_chain()
    assert report["ok"] is True
    assert report["envelope_count"] == 100
    assert report["errors"] == []
    # Last envelope's turn_index is 99 (0-based).
    assert p.envelopes[-1]["subject"]["turn_index"] == 99


# ---------------------------------------------------------------------------
# Lifecycle markers — shutdown then trace_end
# ---------------------------------------------------------------------------


def test_trace_end_after_shutdown_still_produces_valid_envelope(tmp_path: Path) -> None:
    """The SDK may call trace_end after shutdown in cleanup. Chain remains valid."""
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-1"))
    p.shutdown()
    # Post-shutdown event — we accept it (no defensive gate; emission stays
    # faithful to whatever the SDK sends). The chain stays linked.
    p.on_trace_end(_FakeTrace("tr-1"))

    types = [e["subject"]["event_type"] for e in p.envelopes]
    assert types == ["trace_start", "processor_shutdown", "trace_end"]
    assert p.verify_chain()["ok"] is True


def test_force_flush_does_not_break_chain(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-1"))
    p.force_flush()
    p.on_span_start(_FakeSpan("sp-1", "tr-1"))
    p.force_flush()
    p.on_span_end(_FakeSpan("sp-1", "tr-1"))
    p.force_flush()
    p.on_trace_end(_FakeTrace("tr-1"))

    assert p.verify_chain()["ok"] is True
    # 4 real events + 3 force_flush markers = 7 envelopes.
    assert len(p.envelopes) == 7


# ---------------------------------------------------------------------------
# Concurrent callbacks — cross-thread safety
# ---------------------------------------------------------------------------


def test_concurrent_callbacks_produce_consistent_chain(tmp_path: Path) -> None:
    """Multiple threads firing callbacks simultaneously must produce a valid chain.

    The processor's emission lock serializes envelope creation. We do
    NOT assert ordering across threads (a chain produced by concurrent
    threads is non-deterministically interleaved) — only that every
    callback resulted in an envelope and the chain still verifies.
    """
    p = _make_processor(tmp_path)

    def worker(start_idx: int, n: int) -> None:
        for i in range(start_idx, start_idx + n):
            p.on_span_start(_FakeSpan(f"sp-{i}", "tr-concurrent"))

    threads = [threading.Thread(target=worker, args=(i * 20, 20)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(p.envelopes) == 100
    report = p.verify_chain()
    assert report["ok"] is True
    assert report["envelope_count"] == 100
    # Turn indices are dense [0..99] regardless of thread interleaving.
    turn_indices = sorted(e["subject"]["turn_index"] for e in p.envelopes)
    assert turn_indices == list(range(100))


# ---------------------------------------------------------------------------
# _events and envelopes consistency — both lists track the same emissions
# ---------------------------------------------------------------------------


def test_events_and_envelopes_lists_stay_aligned(tmp_path: Path) -> None:
    """Each emission appends to both lists; counts and event_types match."""
    p = _make_processor(tmp_path)

    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_span_start(_FakeSpan("sp-1", "tr-1"))
    p.on_span_end(_FakeSpan("sp-1", "tr-1"))
    p.shutdown()
    p.on_trace_end(_FakeTrace("tr-1"))

    assert len(p.events) == len(p.envelopes) == 5
    # The event_type recorded in _PendingEvent matches the envelope subject.
    pairs = list(zip(p.events, p.envelopes))
    for evt, env in pairs:
        assert evt.event_type == env["subject"]["event_type"]


# ---------------------------------------------------------------------------
# JSONL round-trip — exported file reloads bit-exactly + verifies
# ---------------------------------------------------------------------------


def test_jsonl_round_trip_preserves_chain_byte_exact(tmp_path: Path) -> None:
    """Exporting → reloading from JSONL must produce envelopes that
    re-verify under the module-level helper."""
    from phionyx_openai_agents import verify_chain as module_verify_chain

    p = _make_processor(tmp_path, trace_id="roundtrip-test")
    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_span_start(_FakeSpan("sp-1", "tr-1"))
    p.on_span_end(_FakeSpan("sp-1", "tr-1"))
    p.on_trace_end(_FakeTrace("tr-1"))

    out = tmp_path / "chain.jsonl"
    n = p.export_envelopes(out)
    assert n == 4

    lines = out.read_text(encoding="utf-8").strip().split("\n")
    reloaded = [json.loads(line) for line in lines]
    report = module_verify_chain(reloaded)
    assert report["ok"] is True
    assert report["envelope_count"] == 4

    # In-memory envelopes match the reloaded list (after JSON normalization).
    for in_mem, reloaded_env in zip(p.envelopes, reloaded):
        assert in_mem["integrity"]["current"] == reloaded_env["integrity"]["current"]
        assert in_mem["subject"]["event_type"] == reloaded_env["subject"]["event_type"]


# ---------------------------------------------------------------------------
# Defensive: unknown / weirdly-typed attributes still serialize cleanly
# ---------------------------------------------------------------------------


def test_processor_tolerates_span_with_unhashable_metadata(tmp_path: Path) -> None:
    """A Span whose internal attrs are exotic types still serializes safely."""
    p = _make_processor(tmp_path)

    class WeirdSpan:
        span_id = "sp-w"
        trace_id = "tr-w"
        parent_id = None
        name = "weird"
        started_at = "2026-05-23T00:00:00Z"
        ended_at = "2026-05-23T00:00:01Z"
        error = None
        span_data = object()  # untyped opaque

    p.on_span_end(WeirdSpan())

    env = p.envelopes[0]
    assert env["message"]["payload"]["data"]["span_data_type"] == "object"
    assert p.verify_chain()["ok"] is True
