"""OpenAI Agents SDK ``TracingProcessor`` adapter for Phionyx runtime evidence.

M2 status: envelope-emitting. Each SDK callback event becomes:
    1. An ``AgentMessageEnvelope`` from ``phionyx_core.contracts.envelopes``
       capturing trace_id, turn_id, message_id, timestamp_utc, nonce,
       payload (event_type + SDK trace_id + SDK span_id + serialized data).
    2. A signed, hash-chained outer envelope (see ``audit_chain.py``) that
       links the message to its predecessor in the chain and is signed by
       the operator's Signer.

The processor emits to an :class:`EnvelopeStore` (filesystem by default).
``verify_chain()`` re-reads the persisted chain and checks integrity.
``export_envelopes(path)`` writes the chain as JSONL for sharing.

Design notes:
- The SDK's ``TracingProcessor`` is abstract; we duck-type rather than
  subclass so the package loads cleanly without the SDK installed.
- Trace + Span attribute shapes are read defensively because the
  documented attribute set in the SDK's public docs is partial.
- ``register()`` defers the SDK import to call time.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phionyx_core.contracts.envelopes import AgentMessageEnvelope
from phionyx_core.contracts.participants import ParticipantRef, ParticipantType

from .audit_chain import (
    EnvelopeContext,
    EnvelopeStore,
    FilesystemEnvelopeStore,
    HmacSigner,
    Signer,
    build_envelope,
    canonical_json,
    verify_chain,
)

__version__ = "0.1.0a1"

_DEFAULT_SENDER = ParticipantRef(
    id="openai_agents.tracing_processor",
    type=ParticipantType.SYSTEM,
    name="OpenAI Agents SDK Tracing (Phionyx instrumented)",
)
_DEFAULT_RECEIVER = ParticipantRef(
    id="phionyx.evidence_chain",
    type=ParticipantType.SYSTEM,
    name="Phionyx Runtime Evidence Sink",
)


@dataclass
class _PendingEvent:
    """In-memory event record (M1 legacy surface, retained for inspection)."""

    event_type: str
    obj_id: str
    """``trace_id`` or ``span_id`` of the SDK object that fired the callback,
    or the Phionyx processor trace_id for shutdown / force_flush."""

    timestamp_iso: str
    payload: dict[str, Any] = field(default_factory=dict)


class PhionyxTracingProcessor:
    """OpenAI Agents SDK TracingProcessor that emits signed Phionyx envelopes per event.

    Parameters
    ----------
    trace_id:
        Logical trace ID for the Phionyx envelope chain. All envelopes
        emitted by this processor share it. The SDK emits its own
        per-Trace ``trace_id`` field which is captured INSIDE the
        envelope payload; this Phionyx trace_id groups all envelopes
        from this processor instance. Auto-generated if omitted.
    operator_signing_key:
        Reserved for Ed25519. M2 ships :class:`HmacSigner` (demo).
        Pass a custom :class:`Signer` via ``signer=`` to override.
    signer:
        Custom :class:`Signer`. Defaults to ``HmacSigner()``.
    store:
        Custom :class:`EnvelopeStore`. Defaults to
        :class:`FilesystemEnvelopeStore` rooted at
        ``~/.phionyx/openai_agents_audit`` (override via
        ``PHIONYX_OPENAI_AGENTS_AUDIT_ROOT`` env var).
    sender / receiver:
        :class:`ParticipantRef` identities written into every envelope.

    Usage::

        from agents import add_trace_processor
        from phionyx_openai_agents import PhionyxTracingProcessor

        processor = PhionyxTracingProcessor()
        add_trace_processor(processor)
        # ... run any Agents SDK workflow ...
        processor.verify_chain()      # {"ok": True, ...}
        processor.export_envelopes("evidence.jsonl")
    """

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        operator_signing_key: bytes | str | None = None,
        signer: Signer | None = None,
        store: EnvelopeStore | None = None,
        sender: ParticipantRef | None = None,
        receiver: ParticipantRef | None = None,
    ) -> None:
        self.trace_id = trace_id or f"phionyx-openai-agents-{uuid.uuid4().hex[:12]}"
        self._operator_signing_key = operator_signing_key
        self._signer: Signer = signer or HmacSigner()
        self._store: EnvelopeStore = store or FilesystemEnvelopeStore()
        self._sender = sender or _DEFAULT_SENDER
        self._receiver = receiver or _DEFAULT_RECEIVER

        # Monotonic counters scoped to this processor instance.
        self._turn_index_iter = itertools.count(start=0)
        self._turn_id_iter = itertools.count(start=1)
        self._lock = threading.Lock()  # cross-thread safety for callback storms

        # M1 legacy in-memory event log retained for inspection.
        self._events: list[_PendingEvent] = []
        # In-memory mirror of the emitted envelope chain.
        self._envelopes: list[dict[str, Any]] = []

    # --- inspection surfaces ------------------------------------------------

    @property
    def events(self) -> list[_PendingEvent]:
        return list(self._events)

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        return list(self._envelopes)

    @property
    def store(self) -> EnvelopeStore:
        return self._store

    # --- core emission ------------------------------------------------------

    def _record_and_emit(self, event_type: str, obj_id: str, payload: dict[str, Any]) -> None:
        """Record the event and emit a signed envelope into the chain."""
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            self._events.append(
                _PendingEvent(
                    event_type=event_type,
                    obj_id=obj_id,
                    timestamp_iso=now_iso,
                    payload=payload,
                )
            )

            turn_id = next(self._turn_id_iter)
            turn_index = next(self._turn_index_iter)

            message = AgentMessageEnvelope.create(
                protocol="openai-agents",
                sender_participant_ref=self._sender,
                receiver_participant_ref=self._receiver,
                trace_id=self.trace_id,
                turn_id=turn_id,
                payload={
                    "event_type": event_type,
                    "sdk_obj_id": obj_id,
                    "data": payload,
                },
                ttl_seconds=0,  # audit envelopes do not expire by design
                metadata={
                    "phionyx.event_type": event_type,
                    "phionyx.handler_version": __version__,
                    "phionyx.adapter": "openai_agents",
                },
            )

            ctx = EnvelopeContext(
                trace_id=self.trace_id,
                turn_index=turn_index,
                event_type=event_type,
                agent_message_payload=message.model_dump(mode="json"),
                package_version=__version__,
            )
            previous = self._store.head(self.trace_id)
            envelope = build_envelope(ctx, previous_hash=previous, signer=self._signer)
            self._store.append(self.trace_id, envelope)
            self._envelopes.append(envelope)

    @staticmethod
    def _trace_to_dict(trace: Any) -> dict[str, Any]:
        """Best-effort serialization of an SDK Trace object."""
        return {
            "trace_id": getattr(trace, "trace_id", None),
            "name": getattr(trace, "name", None),
            "workflow_name": getattr(trace, "workflow_name", None),
            "group_id": getattr(trace, "group_id", None),
            "metadata": getattr(trace, "metadata", None),
        }

    @staticmethod
    def _span_to_dict(span: Any) -> dict[str, Any]:
        """Best-effort serialization of an SDK Span object.

        ``started_at`` / ``ended_at`` are serialized via ``str(...)`` when
        present so datetime instances (which AgentMessageEnvelope's
        ``payload`` typing accepts as JSON-serializable strings) survive
        the canonical_json pass.
        """
        started = getattr(span, "started_at", None)
        ended = getattr(span, "ended_at", None)
        error = getattr(span, "error", None)
        span_data = getattr(span, "span_data", None)
        return {
            "span_id": getattr(span, "span_id", None),
            "trace_id": getattr(span, "trace_id", None),
            "parent_id": getattr(span, "parent_id", None),
            "name": getattr(span, "name", None),
            "started_at": str(started) if started is not None else None,
            "ended_at": str(ended) if ended is not None else None,
            "error": str(error) if error else None,
            "span_data_type": type(span_data).__name__ if span_data is not None else None,
        }

    # --- TracingProcessor interface (M2 — envelope-emitting) ---------------

    def on_trace_start(self, trace: Any) -> None:
        self._record_and_emit(
            "trace_start",
            str(getattr(trace, "trace_id", "unknown")),
            self._trace_to_dict(trace),
        )

    def on_trace_end(self, trace: Any) -> None:
        self._record_and_emit(
            "trace_end",
            str(getattr(trace, "trace_id", "unknown")),
            self._trace_to_dict(trace),
        )

    def on_span_start(self, span: Any) -> None:
        self._record_and_emit(
            "span_start",
            str(getattr(span, "span_id", "unknown")),
            self._span_to_dict(span),
        )

    def on_span_end(self, span: Any) -> None:
        self._record_and_emit(
            "span_end",
            str(getattr(span, "span_id", "unknown")),
            self._span_to_dict(span),
        )

    def shutdown(self) -> None:
        """SDK signal: processor is shutting down; flush state.

        Emits a ``processor_shutdown`` envelope so the audit chain has a
        terminal marker — verifiers can detect a chain that was never
        properly closed.
        """
        self._record_and_emit("processor_shutdown", self.trace_id, {})

    def force_flush(self) -> None:
        """SDK signal: caller requests immediate flush of pending events.

        We emit immediately on each callback (no batching), so this is
        primarily a recorded marker. Still emits a ``processor_flush``
        envelope so verifiers see the caller's intent.
        """
        self._record_and_emit("processor_flush", self.trace_id, {})

    # --- registration helper ------------------------------------------------

    def register(self) -> None:
        """Register this processor with the OpenAI Agents SDK.

        Defers the SDK import to call time so the adapter loads cleanly
        in environments without the SDK installed.
        """
        try:
            from agents import add_trace_processor  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "phionyx-openai-agents requires the openai-agents SDK to register. "
                "Install with `pip install openai-agents`."
            ) from exc
        add_trace_processor(self)

    # --- verification & export ---------------------------------------------

    def verify_chain(self) -> dict[str, Any]:
        """Re-read the persisted chain and verify integrity.

        Returns the structured report from
        :func:`phionyx_openai_agents.audit_chain.verify_chain` —
        ``{"ok": bool, "envelope_count": int, "errors": [...]}``.
        """
        envelopes = list(self._store.iter_chain(self.trace_id))
        return verify_chain(envelopes)

    def export_envelopes(self, path: str | Path) -> int:
        """Export the chain to JSONL. Returns the count exported."""
        envelopes = list(self._store.iter_chain(self.trace_id))
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for env in envelopes:
                fh.write(canonical_json(env) + "\n")
        return len(envelopes)
