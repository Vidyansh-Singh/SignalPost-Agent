# Refresh, snapshots and diffs
- State is one JSON snapshot per organisation number (`--state`, default `state/companies/{org}.json`) holding every claim's hash, value, evidence ids and first/last observation time. Writes are atomic.
- `--previous <envelopes.jsonl>` seeds snapshots from an earlier run's envelopes (existing state wins).
- A run diffs current claims against the snapshot and emits typed `changes` (`added`, `changed`, `removed`) with `materiality` (`material` for identity, financials, leadership, group and website; otherwise `minor`).
- Re-running unchanged data emits no changes (idempotent). The envelope file is rewritten in full on each run, so duplicates cannot accumulate.
- A failed refresh does not overwrite the last supported snapshot (a company with no available claims is not saved). Fetch results are cached per URL with a TTL (registry 6 h, company pages 12 h, connectors 7 d).
- Suggested cadence in `refresh.recommended_refresh_interval_days`: 7 days after a change, otherwise 14.
