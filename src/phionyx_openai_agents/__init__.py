"""
phionyx-openai-agents
=====================

OpenAI Agents SDK tracing bridge for Phionyx runtime evidence.

Every Trace and Span emitted by the OpenAI Agents SDK is recorded as a
signed, hash-chained envelope entry. Third parties can verify the chain
offline without trusting the agent's narration.

Status: alpha (v0.1.0a1.dev0) — M1 skeleton.

Public surface (target API, M2)::

    from agents import add_trace_processor   # OpenAI Agents SDK
    from phionyx_openai_agents import PhionyxTracingProcessor

    processor = PhionyxTracingProcessor()
    add_trace_processor(processor)

    # ... run any Agents SDK workflow ...

    processor.verify_chain()
    processor.export_envelopes("evidence.jsonl")

The processor is a stub in M1 — it accepts events but does not yet emit
envelopes. M2 wires the envelope chain.
"""

from .audit_chain import (
    EnvelopeContext,
    EnvelopeStore,
    FilesystemEnvelopeStore,
    HmacSigner,
    Signer,
    build_envelope,
    canonical_json,
    envelope_hash,
    verify_chain,
)
from .processor import PhionyxTracingProcessor

__version__ = "0.1.0a1"
__all__ = [
    "PhionyxTracingProcessor",
    "EnvelopeContext",
    "EnvelopeStore",
    "FilesystemEnvelopeStore",
    "HmacSigner",
    "Signer",
    "build_envelope",
    "canonical_json",
    "envelope_hash",
    "verify_chain",
    "__version__",
]
