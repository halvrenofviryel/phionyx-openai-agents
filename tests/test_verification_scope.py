"""Regress the public boundary: a matching hash is not authentication."""
from copy import deepcopy
from importlib.metadata import version
from phionyx_openai_agents.audit_chain import (
    EnvelopeContext, Ed25519Signer, GENESIS_HASH, build_envelope, verify_chain,
)


def test_signature_mutation_never_claims_authentication():
    # Public, deterministic TEST seed only. No production key.
    signer = Ed25519Signer("11" * 32)
    env = build_envelope(EnvelopeContext(trace_id="scope-test", turn_index=0,
        event_type="test", agent_message_payload={"text": "before"},
        package_version=version("phionyx-openai-agents")), previous_hash=GENESIS_HASH, signer=signer)
    assert env["integrity"]["signature"].startswith("ed25519:")
    changed = deepcopy(env)
    changed["integrity"]["signature"] = "ed25519:" + "00" * 64
    for candidate in ([env], [changed], []):
        report = verify_chain(candidate)
        assert report["verification_scope"] == "HASH_CHAIN_ONLY"
        assert report["signature_verification"] == "NOT_EVALUATED"
        assert report["ok"] is (True if candidate else None)
    changed["message"]["text"] = "after"
    report = verify_chain([changed])
    assert report["ok"] is False
    assert report["signature_verification"] == "NOT_EVALUATED"
