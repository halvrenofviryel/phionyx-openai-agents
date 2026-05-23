"""Audit chain — AgentMessageEnvelope-based hash-chain with HMAC/Ed25519 signing.

This module is vendored from ``phionyx-langchain-langgraph`` for the v0.1.0a1
alpha. The intent is to promote ``audit_chain`` into ``phionyx_core`` for the
v0.5.0 stable release, at which point all companion packages share one
canonical implementation.

On-disk format per envelope::

    {
      "schema": "phionyx.openai_agents_event_envelope.v1",
      "subject": {
          "runtime": "phionyx-openai-agents",
          "version": "<package version>",
          "turn_index": <int>,
          "event_type": "<trace_start|span_end|...>",
          "timestamp_utc": "<ISO8601>"
      },
      "message": <AgentMessageEnvelope.model_dump()>,
      "integrity": {
          "previous": "sha256:...",
          "current": "sha256:...",
          "signature": "...",
          "canonical_json": true
      }
    }

The chain head for a given ``trace_id`` is the most recent envelope's
``integrity.current``. Genesis is a 64-zero sentinel.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

GENESIS_HASH = "sha256:" + "0" * 64
SCHEMA = "phionyx.openai_agents_event_envelope.v1"
RUNTIME = "phionyx-openai-agents"


def canonical_json(payload: Any) -> str:
    """Deterministic JSON encoding: sorted keys, no whitespace, NaN-rejected."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def envelope_hash(payload: dict[str, Any], previous_hash: str) -> str:
    """SHA-256 over canonical-JSON ``{record: payload, previous: previous_hash}``."""
    blob = canonical_json({"record": payload, "previous": previous_hash})
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Signer(Protocol):
    """Signer protocol — production deployments substitute an Ed25519 signer."""

    def sign(self, current_hash: str) -> str: ...


class HmacSigner:
    """Demo-grade HMAC signer.

    NOT cryptographically suitable for production — the secret default is
    public. Production deployments must use an Ed25519 implementation that
    satisfies the ``Signer`` protocol.
    """

    def __init__(self, secret: str = "phionyx.openai_agents.demo.replace.in.production") -> None:
        self._secret = secret.encode("utf-8")

    def sign(self, current_hash: str) -> str:
        import hmac

        digest = hmac.new(self._secret, current_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"


class EnvelopeStore(Protocol):
    """Persistence interface for envelope chains."""

    def head(self, trace_id: str) -> str: ...

    def append(self, trace_id: str, envelope: dict[str, Any]) -> None: ...

    def iter_chain(self, trace_id: str) -> Iterable[dict[str, Any]]: ...


class FilesystemEnvelopeStore:
    """Filesystem-backed envelope persistence.

    Layout::

        <root>/<trace_id>/
            chain.jsonl
            00000000.json
            00000001.json
            ...

    Default root: ``~/.phionyx/openai_agents_audit``. Override with
    ``PHIONYX_OPENAI_AGENTS_AUDIT_ROOT`` env var or constructor argument.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            env_override = os.environ.get("PHIONYX_OPENAI_AGENTS_AUDIT_ROOT")
            root = (
                Path(env_override)
                if env_override
                else Path.home() / ".phionyx" / "openai_agents_audit"
            )
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _trace_dir(self, trace_id: str) -> Path:
        d = self._root / trace_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def head(self, trace_id: str) -> str:
        index = self._trace_dir(trace_id) / "chain.jsonl"
        if not index.exists():
            return GENESIS_HASH
        last: str | None = None
        with index.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if last is None:
            return GENESIS_HASH
        entry = json.loads(last)
        return str(entry["current"])

    def append(self, trace_id: str, envelope: dict[str, Any]) -> None:
        d = self._trace_dir(trace_id)
        turn_index = int(envelope["subject"]["turn_index"])
        envelope_path = d / f"{turn_index:08d}.json"
        envelope_path.write_text(canonical_json(envelope), encoding="utf-8")
        index = d / "chain.jsonl"
        entry = {
            "turn_index": turn_index,
            "current": envelope["integrity"]["current"],
            "previous": envelope["integrity"]["previous"],
            "event_type": envelope["subject"].get("event_type"),
        }
        with index.open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(entry) + "\n")

    def iter_chain(self, trace_id: str) -> Iterable[dict[str, Any]]:
        d = self._trace_dir(trace_id)
        for envelope_path in sorted(d.glob("*.json")):
            if envelope_path.name == "chain.jsonl":
                continue
            yield json.loads(envelope_path.read_text(encoding="utf-8"))


@dataclass
class EnvelopeContext:
    """All inputs needed to build one signed envelope from an SDK event."""

    trace_id: str
    turn_index: int
    event_type: str
    agent_message_payload: dict[str, Any]
    package_version: str


def build_envelope(ctx: EnvelopeContext, *, previous_hash: str, signer: Signer) -> dict[str, Any]:
    """Build a signed, chain-linked envelope from an AgentMessageEnvelope payload."""
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "subject": {
            "runtime": RUNTIME,
            "version": ctx.package_version,
            "turn_index": ctx.turn_index,
            "event_type": ctx.event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        },
        "message": ctx.agent_message_payload,
    }

    current_hash = envelope_hash(payload, previous_hash)
    payload["integrity"] = {
        "previous": previous_hash,
        "current": current_hash,
        "signature": signer.sign(current_hash),
        "canonical_json": True,
    }
    return payload


def verify_chain(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify hash-chain continuity + signature consistency over a list of envelopes.

    Returns a structured report. Does not raise.
    """
    report: dict[str, Any] = {
        "ok": True,
        "envelope_count": len(envelopes),
        "errors": [],
    }
    expected_prev = GENESIS_HASH
    expected_turn = 0
    for i, env in enumerate(envelopes):
        integ = env.get("integrity", {})
        prev = integ.get("previous")
        cur = integ.get("current")
        turn = env.get("subject", {}).get("turn_index")

        if prev != expected_prev:
            report["ok"] = False
            report["errors"].append(
                f"envelope[{i}]: previous_hash mismatch — expected {expected_prev!r}, got {prev!r}"
            )

        payload_for_hash = {k: v for k, v in env.items() if k != "integrity"}
        derived = envelope_hash(payload_for_hash, prev or GENESIS_HASH)
        if derived != cur:
            report["ok"] = False
            report["errors"].append(
                f"envelope[{i}]: current_hash mismatch — stored {cur!r}, derived {derived!r}"
            )

        if turn != expected_turn:
            report["ok"] = False
            report["errors"].append(
                f"envelope[{i}]: turn_index mismatch — expected {expected_turn}, got {turn!r}"
            )

        expected_prev = cur if isinstance(cur, str) else expected_prev
        expected_turn += 1

    return report
