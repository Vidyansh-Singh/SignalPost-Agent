# Candidate and publication gates
A website is published as `official_website` only if ONE of these holds:
1. `orgnr_published_on_site`: the exact organisation number (valid MOD-11) is printed on a page of the site (home, contact, about, privacy, terms).
2. `official_source_candidate_name_match`: the URL was supplied by an official source (registry field or Wikidata) AND the legal name is on the page.
3. A guessed domain whose token equals the legal name exactly (`domain_equals_legal_name`) AND the name is on the page AND one corroboration: the registered postal code or place on the page, the full legal name with its legal form (e.g. "Wedo AS") on the page, the organisation number on a contact/about/privacy/terms page, or the organisation number in the domain's Norid RDAP registrant record.
Hard rejections: a page that prints a *different* valid organisation number is `ambiguous` (conflicting_orgnr). Substring domains (e.g. a longer brand that merely contains the name) are never accepted.
Everything weaker is `ambiguous` (candidate kept in `alternatives`) or `not_available`. Social links, contact details, jobs and news are only collected from a site that passed this gate, and an email is published only if its domain matches the verified site.
Origin of the rule: a 100-company smoke test showed loose substring matching published a different firm's site for a small Norwegian company; the gate was tightened and covered by tests (`tests/test_offline.py::GateTests`).
