# Connectors, budgets and fallback rules
| Source | Used for | Access |
|---|---|---|
| data.brreg.no Enhetsregisteret `/enheter/{org}`, `/enheter/{org}/roller`, `/underenheter?overordnetEnhet={org}` | identity, roles, workplaces | open API (NLOD 2.0) |
| data.brreg.no Regnskapsregisteret `/regnskap/{org}` | latest filed accounts | open API |
| api.gleif.org LEI records | LEI by registered organisation number | open API |
| query.wikidata.org SPARQL (P2333) | Wikidata item and website | robots.txt currently disallows this bot; respected; recorded as `blocked` |
| rdap.norid.no | corroboration only: does the .no domain's registrant record contain OUR organisation number | public RDAP |
| Company website | contact, social links, JSON-LD Organization/JobPosting/NewsArticle/Place/Person, RSS/Atom | only after the identity gate passes; robots.txt and crawl delay obeyed |
Budgets: per host 1 s delay (registry 0.05 s, GLEIF 0.3 s); 15 s request timeout; 2 retries on 429/5xx/timeouts, none on DNS failure, refused connection or certificate errors; 3 MB body cap; at most 12 internal pages and 2 feeds per verified site; 16 workers; 150 s per company; optional `--time-budget` (70% = registry-only mode, 95% = remaining companies get a typed timeout envelope).
Safe URL policy: only public http/https hosts on ports 80/443; IP literals, localhost/internal names and hosts resolving to private/loopback/link-local/reserved addresses are refused, including on redirects.
Fallbacks: static HTML only (no browser rendering). Candidate websites come from the registry field, Wikidata, then name-derived guesses; guesses never count as evidence on their own.
Not used: LinkedIn, Meta, Glassdoor, Indeed, Google, search engines, any paid or keyed API.
