# phionyx-openai-agents

> **Status:** alpha (v0.1.0a1) — live on PyPI. TracingProcessor adapter live; 37 tests pass.

OpenAI Agents SDK tracing bridge for [Phionyx](https://phionyx.ai) runtime evidence. This package surfaces on [phionyx.ai/narrative-coherence](https://phionyx.ai/narrative-coherence) as one of the framework adapters that turn third-party agent runs into reviewer-runnable evidence.

Every `Trace` and `Span` emitted by the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) is recorded as a signed, hash-chained envelope entry. Phionyx provides the trust-object substrate above the SDK's own tracing — observability records *what happened*, Phionyx makes it *verifiable*.

## Why

The OpenAI Agents SDK ships its own tracing (`add_trace_processor`, `BatchTraceProcessor`, `BackendSpanExporter`) optimised for *debugging* and *internal observability*. It is not optimised for *third-party verification*: the trace stream is mutable, unsigned, and exported to the operator's choice of backend. Phionyx envelopes are immutable, hash-chained, and signed under the operator's Ed25519 key — they survive review even when the agent and the trace store are not trusted.

## Install

```bash
pip install phionyx-openai-agents          # core
pip install "phionyx-openai-agents[sdk]"   # + openai-agents SDK
```

Source: [github.com/halvrenofviryel/phionyx_openai_agents](https://github.com/halvrenofviryel/phionyx_openai_agents).

## 60-second usage

```python
from agents import add_trace_processor
from phionyx_openai_agents import PhionyxTracingProcessor

processor = PhionyxTracingProcessor()    # default HmacSigner + filesystem store
add_trace_processor(processor)

# ... run any Agents SDK workflow ...

print(f"{len(processor.envelopes)} signed envelopes")
print(f"Chain verifies: {processor.verify_chain()['ok']}")
processor.export_envelopes("evidence/run.jsonl")
```

A complete runnable example (works with or without the SDK installed)
is in [`examples/quickstart.py`](examples/quickstart.py):

```bash
pip install phionyx-openai-agents
python examples/quickstart.py
```

Expected output (mocked-SDK mode):

    Mode        : mocked Trace/Span
    Envelopes   : 8 ({'trace_start': 1, 'span_start': 2, 'span_end': 2, 'processor_flush': 1, 'trace_end': 1, 'processor_shutdown': 1})
    Verify chain: OK (8 envelopes; 0 errors)
    Exported    : 8 envelopes → /tmp/phionyx-oai-demo-XXXXXXXX/quickstart-evidence.jsonl

## Span trees

The processor preserves `parent_id` linkage in every envelope payload, so verifiers can reconstruct the full span tree:

```python
# A typical workflow: supervisor → worker → tool
on_trace_start(trace)
on_span_start(root)                              # parent_id = None
on_span_start(child, parent_id=root.span_id)     # parent_id = "sp-root"
on_span_start(grand, parent_id=child.span_id)    # parent_id = "sp-child"
on_span_end(grand)
on_span_end(child)
on_span_end(root)
on_trace_end(trace)
```

Each `span_start` envelope's payload exposes `parent_id` and `span_id`; together with `trace_id` you can reconstruct any tree shape offline without reading the SDK's own trace stream.

## Status — what's live in v0.1.0a1

- ✅ **PhionyxTracingProcessor** — all 6 SDK `TracingProcessor` methods
  (`on_trace_start/end`, `on_span_start/end`, `shutdown`, `force_flush`)
  emit signed envelopes.
- ✅ **AgentMessageEnvelope** as the inner record (from
  `phionyx_core.contracts.envelopes`).
- ✅ **HmacSigner** demo + **Signer** protocol for Ed25519 swap.
- ✅ **FilesystemEnvelopeStore** with `PHIONYX_OPENAI_AGENTS_AUDIT_ROOT`
  env-var override.
- ✅ **`verify_chain`** — detects payload tamper + broken links.
- ✅ **`export_envelopes`** — JSONL round-trip preserves chain
  byte-exact and re-verifies under module-level helper.
- ✅ **Cross-thread emission lock** — 5-thread × 20-callback test
  yields a dense `[0..99]` turn-index sequence; no race conditions.
- ✅ **Defensive serialization** — minimally-attributed Trace/Span
  objects degrade to `unknown` / `None`, never crash.
- ✅ **`register()` defers SDK import** — package loads cleanly
  without `openai-agents` installed.
- ✅ **37 tests** — smoke, envelope chain, extended scenarios
  (multi-span tree, parent_id chains, error spans, 100-event chain,
  concurrent callbacks, lifecycle edges, JSONL round-trip).

Roadmap beyond v0.1.0a1: v0.1.0 stable schema lock alongside `phionyx-core` v0.5.0 (live as of 2026-05-24); `audit_chain` promotion into `phionyx_core` for shared use across all companion packages (planned for v0.6.0).

## audit_chain vendoring

This package vendors a copy of `audit_chain.py` from `phionyx-langchain-langgraph`. The two copies differ only in namespace constants (schema id, runtime tag, default audit root, env var, HMAC secret prefix); the canonical-JSON discipline and hash format are identical, so verifiers written once apply to both companion packages. The promotion of `audit_chain` into `phionyx_core` (so the companion packages share one canonical implementation) is on the v0.6.0 roadmap.

## License

AGPL-3.0-or-later. Commercial dual-license available — contact founder@phionyx.ai.

## See also

- [phionyx.ai/narrative-coherence](https://phionyx.ai/narrative-coherence) — entry pillar this package surfaces under
- [phionyx.ai/evidence](https://phionyx.ai/evidence) — Evidence Matrix: every load-bearing claim paired with a reviewer-runnable command
- [`phionyx-core`](https://pypi.org/project/phionyx-core/) (PyPI) — core envelope schema + Ed25519 signing
- [`phionyx-langchain-langgraph`](https://github.com/halvrenofviryel/phionyx_langchain_langgraph) (PyPI) — LangChain + LangGraph bridge companion
- [`phionyx-mcp-server`](https://github.com/halvrenofviryel/phionyx-mcp-server) — MCP trust boundary companion
- [`phionyx-pipeline-mcp`](https://github.com/halvrenofviryel/phionyx-pipeline-mcp) — agent self-claim gate companion
- [`phionyx-eval-inspect`](https://github.com/halvrenofviryel/phionyx-eval-inspect) — Inspect AI bridge companion (interop-only; no UK AISI endorsement claim)
