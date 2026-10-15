# Envelope, claims and evidence
One JSON object per input organisation number, in input order:
`organisation_number`, `input_valid`, `legal_identity`, `claims[]`, `evidence[]`, `changes[]`, `errors[]`, `synthesis`, `refresh`, `coverage`, `operations`, `run`.
- `run`: `run_id`, `agent_version`, `started_at`, `completed_at`, `terminal_status` (`completed` | `partial` | `failed`).
- `claims[]`: `claim_key` (unique), `field_family`, `field`, `availability` (`available` | `not_available` | `blocked` | `not_applicable` | `ambiguous` | `failed`), `value`, `confidence`, `evidence_ids`, `entity_link` (`organisation_number`, `method`, `score`, `matched_on`), `alternatives`, `unit`, `period`, `note`.
- `evidence[]`: `id`, `source_url`, `final_url`, `http_status`, `source_class` (`official_registry` | `open_data_api` | `company_owned`), `source_policy`, `retrieved_at`, `content_sha256`, `claim_span`, `selector`, `extractor`, `publication_date`, `reporting_period`.
- `changes[]`: `claim_key`, `field`, `change_type` (`added` | `changed` | `removed`), `previous_value`, `current_value`, `first_observed_at`, `observed_at`, `materiality`.
- `errors[]`: `stage`, `target`, `kind`, `message`, `retryable`, `occurred_at`.
- `operations`: `requests`, `cache_hits`, `runtime_ms`, `third_party_cost_usd`, `robots_denied`.
Contract rules enforced by `validate()` and by the run's exit code: an `available` claim has a value, an entity link and evidence with span, hash, policy and retrieval time; financial and activity claims carry a reporting period or publication date; a non-available claim never has a numeric value and always has an explanatory note; claim keys are unique; one envelope per input.
