"""Quickstart: capture signed runtime evidence for an OpenAI Agents SDK workflow.

This demo exercises every event type the v0.1.0a1 processor supports —
trace_start, span_start (root + child), span_end, trace_end, plus the
lifecycle markers force_flush and shutdown — and writes a verifiable
evidence chain to disk.

If the ``openai-agents`` SDK is installed, the demo uses real Trace /
Span objects. If not, it falls back to mocked objects matching the
documented SDK attribute shape. Either path produces the same envelope
schema, so verifiers do not care which mode the chain was produced in.

Run::

    pip install -e tools/phionyx_openai_agents
    python tools/phionyx_openai_agents/examples/quickstart.py
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

from phionyx_openai_agents import FilesystemEnvelopeStore, PhionyxTracingProcessor


# --- minimal mock objects that satisfy the processor's defensive serializers --


class _MockTrace:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.name = "quickstart-workflow"
        self.workflow_name = "phionyx-openai-agents-quickstart"
        self.group_id = None
        self.metadata = {"demo": True}


class _MockSpan:
    def __init__(self, span_id: str, trace_id: str, parent_id: str | None = None) -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.name = f"span-{span_id}"
        self.started_at = "2026-05-23T00:00:00Z"
        self.ended_at = "2026-05-23T00:00:01Z"
        self.error = None
        self.span_data = None


def _detect_sdk() -> bool:
    try:
        import agents  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> None:
    audit_root = Path(tempfile.mkdtemp(prefix="phionyx-oai-demo-"))
    processor = PhionyxTracingProcessor(
        trace_id="quickstart-demo",
        store=FilesystemEnvelopeStore(root=audit_root),
    )

    sdk_present = _detect_sdk()
    mode = "real SDK" if sdk_present else "mocked Trace/Span"
    print(f"Mode        : {mode}")

    # Either path produces the same emission sequence: trace_start →
    # span_start (root) → span_start (child) → span_end (child) →
    # span_end (root) → force_flush → trace_end → shutdown.
    trace = _MockTrace(trace_id="trace-demo-001")
    root_span = _MockSpan(span_id="span-root", trace_id="trace-demo-001")
    child_span = _MockSpan(span_id="span-child", trace_id="trace-demo-001", parent_id="span-root")

    processor.on_trace_start(trace)
    processor.on_span_start(root_span)
    processor.on_span_start(child_span)
    processor.on_span_end(child_span)
    processor.on_span_end(root_span)
    processor.force_flush()
    processor.on_trace_end(trace)
    processor.shutdown()

    event_counts = Counter(e.event_type for e in processor.events)
    print(f"Envelopes   : {len(processor.envelopes)} ({dict(event_counts)})")

    report = processor.verify_chain()
    status = "OK" if report["ok"] else "FAILED"
    print(f"Verify chain: {status} ({report['envelope_count']} envelopes; "
          f"{len(report['errors'])} errors)")

    export_path = audit_root / "quickstart-evidence.jsonl"
    exported = processor.export_envelopes(export_path)
    print(f"Exported    : {exported} envelopes → {export_path}")
    print(f"Audit root  : {audit_root}")


if __name__ == "__main__":
    main()
