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

## Validation (v1.3.0)
`python run.py validate --file out/profiles.jsonl` checks every envelope (`src/signalpost/contract.py`): required top-level
keys and types, terminal status, ISO-8601 UTC timestamps, non-negative operations counters, evidence fields (public
http(s) URL, allowed source class, 64-hex SHA-256, non-empty span and policy), unique claim keys prefixed with the
organisation number, allowed availability states, confidence in [0,1], no dangling or unused evidence, entity link
pointing at the same organisation, financial values present in their evidence span with unit and reporting period,
news items with a matching publication date, absence never numeric and always explained, synthesis grounded in real
claims, coverage counts matching the claims, one envelope per organisation number. Malformed files (bad JSON lines,
bad encoding, missing file, empty file) are reported as issues, never as a crash.

## v1.5.0 additions
- `financials|history`: list of `{period, months: 12, revenue, operating_cost, operating_result, profit_for_year, total_assets, equity}`, oldest first, same accounting basis as the latest filing; a missing line item is `null`.
- `locations|registered_workplace` carries `parent_organisation_number`; `registered_workplace_count` is the register's own total; `locations|registered_workplaces_complete` (`ambiguous`/`failed`) appears only when the list is incomplete.
- `social_profiles|<platform>_url` is `ambiguous` (with `alternatives`) when two accounts compete. `public_activity|feed_items` is `ambiguous` for page-type feed items. `public_activity|sitemap_news` records sitemap discovery outcomes.
- Report adds `input_lines`, `distinct_inputs`, `duplicate_input_lines`, `robots_by_host`.
