## 0.1.0a4 (candidate)

- Emit the package version consistently in callback/supervisor envelopes.
- Make the hash-only scope of `verify_chain` explicit in every report and in
  examples. Signature verification is still NOT_EVALUATED; no authentication
  capability is added. Existing `ok` behavior is retained.
- Label the quickstart as mocked Trace/Span regardless of SDK installation.
