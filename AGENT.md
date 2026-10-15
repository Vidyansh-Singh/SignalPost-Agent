# Agent research and abstention policy
1. The organisation number is the only stable key. Every run starts from the Brønnøysund registry record.
2. Registry facts (identity, status, address, employees, roles, sub-units, filed accounts) are published as read.
3. Everything about the public brand (website, social links, contact, jobs, news) is a *candidate* until it is tied to the exact entity (see IDENTITY_RESOLUTION.md).
4. No LLM is used anywhere. Summaries are extractive: every sentence is built from claims that carry evidence, and `synthesis.grounded_claim_keys` lists them.
5. Abstain rather than guess. Weak or conflicting matches are returned as `ambiguous` with the candidate kept in `alternatives`; missing data is `not_available`; robots/terms refusals are `blocked`; errors are `failed`; fields that do not apply are `not_applicable`. Absence is never rendered as zero.
6. Undated news items are withheld from dated public activity (state `ambiguous`).
7. A failure in one source or one company never stops the batch; every input still receives exactly one terminal envelope.
