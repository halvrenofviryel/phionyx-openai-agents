"""M1 smoke tests for phionyx-openai-agents.

These verify the package-level contract that survives M1 → M2:
- The package is importable without the openai-agents SDK installed.
- PhionyxTracingProcessor exposes the six SDK TracingProcessor methods
  with the documented signatures.
- The processor records events when those methods fire.
- M2 surfaces (verify_chain, export_envelopes) raise NotImplementedError.

M2 will replace test_envelope_chain.py with envelope-emission tests.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def _make_processor(trace_id: str | None = None):
    """Construct a processor with a temp-dir-backed store so tests don't
    pollute ``~/.phionyx/openai_agents_audit``."""
    from phionyx_openai_agents import FilesystemEnvelopeStore, PhionyxTracingProcessor

    tmp = Path(tempfile.mkdtemp(prefix="phionyx-oai-smoke-"))
    return PhionyxTracingProcessor(trace_id=trace_id, store=FilesystemEnvelopeStore(root=tmp))


# ---------------------------------------------------------------------------
# Module-level smoke
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    import phionyx_openai_agents as pkg

    assert pkg.__version__.startswith("0.1.0a1")
    assert hasattr(pkg, "PhionyxTracingProcessor")
    assert hasattr(pkg, "FilesystemEnvelopeStore")
    assert hasattr(pkg, "HmacSigner")
    assert hasattr(pkg, "build_envelope")
    assert hasattr(pkg, "verify_chain")


def test_package_loads_without_sdk() -> None:
    """The package must import cleanly even if openai-agents is absent.

    The SDK import is deferred to ``register()`` call time. Constructing
    a processor + calling its event methods must NOT require the SDK.
    """
    # Just constructing should work — no SDK import here.
    p = _make_processor()
    assert p.trace_id.startswith("phionyx-openai-agents-")


def test_processor_exposes_all_six_interface_methods() -> None:
    """The processor must duck-type the SDK's TracingProcessor surface."""
    expected_methods = {
        "on_trace_start",
        "on_trace_end",
        "on_span_start",
        "on_span_end",
        "shutdown",
        "force_flush",
    }
    p = _make_processor()
    for name in expected_methods:
        assert callable(getattr(p, name)), f"missing: {name}"


# ---------------------------------------------------------------------------
# Stub event recording — these survive into M2 (the same events are recorded)
# ---------------------------------------------------------------------------


class _FakeTrace:
    def __init__(self, trace_id: str, name: str = "workflow") -> None:
        self.trace_id = trace_id
        self.name = name
        self.workflow_name = "demo-workflow"
        self.group_id = None
        self.metadata = {"k": "v"}


class _FakeSpan:
    def __init__(self, span_id: str, trace_id: str, name: str = "operation") -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = None
        self.name = name
        self.started_at = "2026-05-23T00:00:00Z"
        self.ended_at = "2026-05-23T00:00:01Z"
        self.error = None
        self.span_data = None


def test_on_trace_start_and_end_record_events() -> None:
    from phionyx_openai_agents import PhionyxTracingProcessor

    p = _make_processor()
    trace = _FakeTrace(trace_id="tr-1")

    p.on_trace_start(trace)
    p.on_trace_end(trace)

    assert len(p.events) == 2
    assert p.events[0].event_type == "trace_start"
    assert p.events[0].obj_id == "tr-1"
    assert p.events[0].payload["trace_id"] == "tr-1"
    assert p.events[0].payload["workflow_name"] == "demo-workflow"
    assert p.events[1].event_type == "trace_end"


def test_on_span_start_and_end_record_events() -> None:
    from phionyx_openai_agents import PhionyxTracingProcessor

    p = _make_processor()
    span = _FakeSpan(span_id="sp-1", trace_id="tr-1")

    p.on_span_start(span)
    p.on_span_end(span)

    assert len(p.events) == 2
    assert p.events[0].event_type == "span_start"
    assert p.events[0].obj_id == "sp-1"
    assert p.events[0].payload["span_id"] == "sp-1"
    assert p.events[0].payload["trace_id"] == "tr-1"
    assert p.events[1].event_type == "span_end"


def test_shutdown_and_force_flush_record_events() -> None:
    p = _make_processor(trace_id="px-001")

    p.force_flush()
    p.shutdown()

    assert len(p.events) == 2
    assert p.events[0].event_type == "processor_flush"
    assert p.events[1].event_type == "processor_shutdown"
    # Both flush + shutdown reference the Phionyx trace_id, not any SDK trace.
    assert p.events[0].obj_id == "px-001"
    assert p.events[1].obj_id == "px-001"


def test_handler_tolerates_minimally_attributed_objects() -> None:
    """Trace/Span objects with missing attributes degrade gracefully."""

    class Bare:
        pass

    p = _make_processor()
    p.on_trace_start(Bare())  # missing trace_id, name, etc.
    p.on_span_start(Bare())

    assert len(p.events) == 2
    # Defensive getattr fallback yielded 'unknown' for the obj_id.
    assert p.events[0].obj_id == "unknown"
    assert p.events[1].obj_id == "unknown"
    # Payload entries are all None (None is acceptable for defensive serialization).
    assert all(v is None for v in p.events[0].payload.values())


def test_events_property_returns_copy() -> None:
    p = _make_processor()
    p.on_trace_start(_FakeTrace("tr-1"))

    snapshot = p.events
    snapshot.clear()
    assert len(p.events) == 1


# ---------------------------------------------------------------------------
# register() helper — SDK import deferred to call time
# ---------------------------------------------------------------------------


def test_register_without_sdk_raises_clear_error() -> None:
    """register() defers SDK import; without SDK it raises ImportError with a clear hint."""
    from phionyx_openai_agents import PhionyxTracingProcessor

    p = PhionyxTracingProcessor()
    with pytest.raises(ImportError, match="openai-agents"):
        p.register()
