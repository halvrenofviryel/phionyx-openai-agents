"""Mock Trace/Span quickstart (no SDK or model invocation).

This exercises processor callbacks with hand-built objects, whether or not
openai-agents is installed. It is not an SDK integration test. Envelopes are
unsigned unless the caller explicitly configures a signer.

Run from the repository after installation: python examples/quickstart.py
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


def main() -> None:
    audit_root = Path(tempfile.mkdtemp(prefix="phionyx-oai-demo-"))
    processor = PhionyxTracingProcessor(
        trace_id="quickstart-demo",
        store=FilesystemEnvelopeStore(root=audit_root),
    )

    mode = "mocked Trace/Span"
    print(f"Mode        : {mode}")

    # The mock callback sequence: trace_start →
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
    print(f"Hash chain  : {status} ({report['envelope_count']} envelopes; "
          f"{len(report['errors'])} errors)")

    print("Signatures  : NOT_EVALUATED (hash check only)")

    export_path = audit_root / "quickstart-evidence.jsonl"
    exported = processor.export_envelopes(export_path)
    print(f"Exported    : {exported} envelopes → {export_path}")
    print(f"Audit root  : {audit_root}")


if __name__ == "__main__":
    main()
