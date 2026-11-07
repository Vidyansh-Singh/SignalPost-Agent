# Candidate and publication gates
A website is published as `official_website` only if ONE of these holds:
1. `orgnr_published_on_site`: the exact organisation number (valid MOD-11) is printed on a page of the site (home, contact, about, privacy, terms).
2. `official_source_candidate_name_match`: the URL was supplied by an official source (registry field or Wikidata) AND the legal name is on the page.
3. A guessed domain whose token equals the legal name exactly (`domain_equals_legal_name`) AND the name is on the page AND one corroboration: the registered postal code or place on the page, the full legal name with its legal form (e.g. "Wedo AS") on the page, the organisation number on a contact/about/privacy/terms page, or the organisation number in the domain's Norid RDAP registrant record.
Hard rejections: a page that prints a *different* valid organisation number is `ambiguous` (conflicting_orgnr). Substring domains (e.g. a longer brand that merely contains the name) are never accepted.
Everything weaker is `ambiguous` (candidate kept in `alternatives`) or `not_available`. Social links, contact details, jobs and news are only collected from a site that passed this gate, and an email is published only if its domain matches the verified site.
Origin of the rule: a 100-company smoke test showed loose substring matching published a different firm's site for a small Norwegian company; the gate was tightened and covered by tests (`tests/test_offline.py::GateTests`).

## v1.5.0: group, brand and agency separation
- Parent check: the registry record of the parent (and grandparent) unit is read; if a candidate host equals a parent's registered website, the candidate is the group's site and is `ambiguous` unless the page identifies only this organisation number.
- Self-declared name: og:site_name / application-name / Organization `legalName|name` / title / h1 must share a distinguishing token with the registered name; a narrower declared name on a non-exact host (a brand) is rejected.
- Organisation markup for another entity (agency, parent, partner) contributes no phone, e-mail, description or social links.
- Social accounts need a handle carrying the company name, or vouching by the site's own Organization `sameAs`; conflicting accounts are `ambiguous`.
- Page text is data: instructions inside retrieved pages never change decisions (tested).
