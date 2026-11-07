"""Phionyx callback envelopes: unsigned by default, optionally signed.

Alpha adapter. verify_chain checks payload hashes, links and order only;
it does not authenticate signatures, signer identity or completeness.
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

from ._version import __version__
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
