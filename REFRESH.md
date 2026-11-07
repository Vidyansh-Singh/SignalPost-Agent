# Refresh, snapshots and diffs
- State is one JSON snapshot per organisation number (`--state`, default `state/companies/{org}.json`) holding every claim's hash, value, evidence ids and first/last observation time. Writes are atomic.
- `--previous <envelopes.jsonl>` seeds snapshots from an earlier run's envelopes (existing state wins).
- A run diffs current claims against the snapshot and emits typed `changes` (`added`, `changed`, `removed`) with `materiality` (`material` for identity, financials, leadership, group and website; otherwise `minor`).
- Re-running unchanged data emits no changes (idempotent). The envelope file is rewritten in full on each run, so duplicates cannot accumulate.
- A failed refresh does not overwrite the last supported snapshot (a company with no available claims is not saved). Fetch results are cached per URL with a TTL (registry 6 h, company pages 12 h, connectors 7 d).
- Suggested cadence in `refresh.recommended_refresh_interval_days`: 7 days after a change, otherwise 14.

## Timeout isolation (v1.3.0)
When a company exceeds its budget the runner seals it and issues the terminal `failed` envelope. A sealed worker is
cancelled at its next stage boundary and its state write is refused under a lock, so an abandoned worker can never
change the stored snapshot after the envelope was issued. If the worker committed in the instant the timer fired, its
own envelope is used so envelope and state always agree. Unreadable `--previous` files degrade to first observation.

## v1.5.0: transient failures and resume
- A claim whose source failed or was blocked this run keeps its previous supported value (carried forward, `last_attempt` recorded) and yields no change. Families not attempted in a run (budget cut, outage) are carried forward too.
- `--resume` reuses finished (`completed`/`partial`) envelopes of the same agent version from the ledger (`<output>.ledger.jsonl`, fsync'd per company, torn last lines ignored); `failed` envelopes are retried.
