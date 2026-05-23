"""M2 tests for the envelope chain (OpenAI Agents SDK adapter).

Mirrors the LangChain adapter's M2 contract:
- Every SDK callback event emits one signed envelope.
- Envelopes form a hash chain — each previous matches the prior current.
- The chain is persisted to disk and can be re-read.
- ``verify_chain()`` detects payload tamper and broken links.
- ``export_envelopes()`` writes a valid JSONL file.
- AgentMessageEnvelope is the inner record.
- Custom signer is invoked per envelope.

SDK is mocked via plain Python objects matching the documented Trace /
Span attribute set. Real SDK integration deferred to CI environment.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _make_processor(tmp_path: Path, trace_id: str | None = None):
    from phionyx_openai_agents import FilesystemEnvelopeStore, PhionyxTracingProcessor

    return PhionyxTracingProcessor(
        trace_id=trace_id, store=FilesystemEnvelopeStore(root=tmp_path)
    )


class _FakeTrace:
    def __init__(self, trace_id: str, name: str = "workflow") -> None:
        self.trace_id = trace_id
        self.name = name
        self.workflow_name = "demo-workflow"
        self.group_id = None
        self.metadata = {"k": "v"}


class _FakeSpan:
    def __init__(
        self,
        span_id: str,
        trace_id: str,
        name: str = "operation",
        parent_id: str | None = None,
    ) -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.name = name
        self.started_at = "2026-05-23T00:00:00Z"
        self.ended_at = "2026-05-23T00:00:01Z"
        self.error = None
        self.span_data = None


# ---------------------------------------------------------------------------
# Basic emission
# ---------------------------------------------------------------------------


def test_trace_start_emits_one_envelope(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-1"))

    assert len(p.envelopes) == 1
    env = p.envelopes[0]
    assert env["schema"] == "phionyx.openai_agents_event_envelope.v1"
    assert env["subject"]["event_type"] == "trace_start"
    assert env["subject"]["turn_index"] == 0
    assert env["subject"]["runtime"] == "phionyx-openai-agents"
    assert "message" in env
    assert "integrity" in env


def test_full_workflow_emits_envelope_per_event(tmp_path: Path) -> None:
    """A realistic Trace → 2 Spans → trace_end emits 5 envelopes."""
    p = _make_processor(tmp_path)
    tr = _FakeTrace("tr-flow")
    sp1 = _FakeSpan("sp-a", "tr-flow")
    sp2 = _FakeSpan("sp-b", "tr-flow", parent_id="sp-a")

    p.on_trace_start(tr)
    p.on_span_start(sp1)
    p.on_span_end(sp1)
    p.on_span_start(sp2)
    p.on_span_end(sp2)
    p.on_trace_end(tr)

    event_types = [e["subject"]["event_type"] for e in p.envelopes]
    assert event_types == [
        "trace_start",
        "span_start",
        "span_end",
        "span_start",
        "span_end",
        "trace_end",
    ]


def test_chain_hash_continuity(tmp_path: Path) -> None:
    from phionyx_openai_agents.audit_chain import GENESIS_HASH

    p = _make_processor(tmp_path)
    tr = _FakeTrace("tr-1")
    sp = _FakeSpan("sp-1", "tr-1")

    p.on_trace_start(tr)
    p.on_span_start(sp)
    p.on_span_end(sp)
    p.on_trace_end(tr)

    envs = p.envelopes
    assert envs[0]["integrity"]["previous"] == GENESIS_HASH
    for i in range(1, len(envs)):
        assert envs[i]["integrity"]["previous"] == envs[i - 1]["integrity"]["current"]
    assert [e["subject"]["turn_index"] for e in envs] == [0, 1, 2, 3]


def test_envelopes_persisted_to_disk(tmp_path: Path) -> None:
    p = _make_processor(tmp_path, trace_id="persist-test")
    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_trace_end(_FakeTrace("tr-1"))

    trace_dir = tmp_path / "persist-test"
    assert trace_dir.is_dir()
    files = sorted(p.name for p in trace_dir.iterdir())
    assert files == ["00000000.json", "00000001.json", "chain.jsonl"]

    index_lines = (trace_dir / "chain.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(index_lines) == 2
    entry0 = json.loads(index_lines[0])
    entry1 = json.loads(index_lines[1])
    assert entry0["event_type"] == "trace_start"
    assert entry1["event_type"] == "trace_end"
    assert entry1["previous"] == entry0["current"]


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verify_chain_passes_on_clean_chain(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    for i in range(4):
        p.on_span_start(_FakeSpan(f"sp-{i}", "tr-1"))

    report = p.verify_chain()
    assert report["ok"] is True
    assert report["envelope_count"] == 4
    assert report["errors"] == []


def test_verify_chain_detects_tampered_payload(tmp_path: Path) -> None:
    p = _make_processor(tmp_path, trace_id="tamper-test")
    p.on_trace_start(_FakeTrace("tr-orig"))
    p.on_trace_end(_FakeTrace("tr-orig"))

    target = tmp_path / "tamper-test" / "00000000.json"
    env = json.loads(target.read_text(encoding="utf-8"))
    env["message"]["payload"]["data"]["trace_id"] = "tr-tampered"
    target.write_text(json.dumps(env), encoding="utf-8")

    report = p.verify_chain()
    assert report["ok"] is False
    assert any("current_hash mismatch" in e for e in report["errors"])


def test_verify_chain_detects_broken_link(tmp_path: Path) -> None:
    p = _make_processor(tmp_path, trace_id="broken-link-test")
    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_trace_end(_FakeTrace("tr-1"))

    target = tmp_path / "broken-link-test" / "00000001.json"
    env = json.loads(target.read_text(encoding="utf-8"))
    env["integrity"]["previous"] = "sha256:" + "1" * 64
    target.write_text(json.dumps(env), encoding="utf-8")

    report = p.verify_chain()
    assert report["ok"] is False
    assert any("previous_hash mismatch" in e for e in report["errors"])


# ---------------------------------------------------------------------------
# Inner record shape
# ---------------------------------------------------------------------------


def test_inner_record_is_agent_message_envelope(tmp_path: Path) -> None:
    from phionyx_core.contracts.envelopes import AgentMessageEnvelope

    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-inner"))

    env = p.envelopes[0]
    msg = AgentMessageEnvelope(**env["message"])
    assert msg.protocol == "openai-agents"
    assert msg.trace_id == p.trace_id
    assert msg.turn_id == 1
    assert msg.payload["event_type"] == "trace_start"
    assert msg.payload["sdk_obj_id"] == "tr-inner"
    assert msg.payload["data"]["trace_id"] == "tr-inner"
    assert msg.metadata["phionyx.adapter"] == "openai_agents"


def test_turn_id_is_monotonic_per_processor(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    for i in range(3):
        p.on_trace_start(_FakeTrace(f"tr-{i}"))
    turn_ids = [env["message"]["turn_id"] for env in p.envelopes]
    assert turn_ids == [1, 2, 3]


def test_shutdown_and_flush_emit_envelopes(tmp_path: Path) -> None:
    """Lifecycle markers (shutdown/flush) also go onto the chain — verifiers
    can detect chains that were never properly closed."""
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-1"))
    p.force_flush()
    p.shutdown()

    types = [e["subject"]["event_type"] for e in p.envelopes]
    assert types == ["trace_start", "processor_flush", "processor_shutdown"]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_envelopes_writes_valid_jsonl(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-x"))
    p.on_span_start(_FakeSpan("sp-x", "tr-x"))
    p.on_trace_end(_FakeTrace("tr-x"))

    out = tmp_path / "exported.jsonl"
    count = p.export_envelopes(out)
    assert count == 3
    assert out.exists()

    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        env = json.loads(line)
        assert env["schema"] == "phionyx.openai_agents_event_envelope.v1"
        assert "integrity" in env


def test_export_envelopes_creates_parent_dir(tmp_path: Path) -> None:
    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-x"))

    out = tmp_path / "nested" / "dir" / "evidence.jsonl"
    p.export_envelopes(out)
    assert out.exists()


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_hmac_signer_is_deterministic() -> None:
    from phionyx_openai_agents import HmacSigner

    s = HmacSigner()
    sig1 = s.sign("sha256:" + "a" * 64)
    sig2 = s.sign("sha256:" + "a" * 64)
    assert sig1 == sig2
    assert sig1.startswith("hmac-sha256:")


def test_custom_signer_is_invoked(tmp_path: Path) -> None:
    from phionyx_openai_agents import FilesystemEnvelopeStore, PhionyxTracingProcessor

    calls: list[str] = []

    class RecordingSigner:
        def sign(self, current_hash: str) -> str:
            calls.append(current_hash)
            return f"custom:{len(calls)}"

    p = PhionyxTracingProcessor(
        store=FilesystemEnvelopeStore(root=tmp_path),
        signer=RecordingSigner(),
    )
    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_trace_end(_FakeTrace("tr-1"))

    assert len(calls) == 2
    sigs = [env["integrity"]["signature"] for env in p.envelopes]
    assert sigs == ["custom:1", "custom:2"]


# ---------------------------------------------------------------------------
# Filesystem isolation
# ---------------------------------------------------------------------------


def test_filesystem_store_respects_constructor_root(tmp_path: Path) -> None:
    from phionyx_openai_agents import FilesystemEnvelopeStore

    store = FilesystemEnvelopeStore(root=tmp_path)
    assert store.root == tmp_path


def test_filesystem_store_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from phionyx_openai_agents import FilesystemEnvelopeStore

    custom_root = tmp_path / "from_env"
    monkeypatch.setenv("PHIONYX_OPENAI_AGENTS_AUDIT_ROOT", str(custom_root))
    store = FilesystemEnvelopeStore()
    assert store.root == custom_root


# ---------------------------------------------------------------------------
# Module-level verify_chain helper
# ---------------------------------------------------------------------------


def test_module_level_verify_chain_on_processor_envelopes(tmp_path: Path) -> None:
    from phionyx_openai_agents import verify_chain

    p = _make_processor(tmp_path)
    p.on_trace_start(_FakeTrace("tr-1"))
    p.on_trace_end(_FakeTrace("tr-1"))

    report = verify_chain(p.envelopes)
    assert report["ok"] is True
    assert report["envelope_count"] == 2
