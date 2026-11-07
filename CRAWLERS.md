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

## robots.txt decisions and network guard (v1.3.0)
robots.txt is resolved per host following RFC 9309 and cached:
| robots.txt response | Decision |
|---|---|
| 200 | parse rules, apply to every URL |
| 404 / 410 / other 4xx | allow |
| 401 / 403 | disallow (cached for the run) |
| 429, 5xx, timeout, network error | unreachable: **fail closed** (disallow), re-checked after 300 s |
| DNS failure / connection refused / certificate error | dead host, nothing to fetch |

Blocked results surface as `blocked` claims with the reason in the note. Every connection is made through a guarded
socket: the host is resolved once, every address must be a public unicast address (no private, loopback, link-local,
CGNAT, multicast, reserved, IPv4-mapped private), and the connection goes to that validated address, so a DNS answer
that changes between check and connect (DNS rebinding) cannot reach an internal host. Redirects are re-validated;
URLs with embedded credentials are refused.

## Response size and HTTP client (v1.3.1)
- Bodies are read in 64 KiB chunks up to 3 MB. A body that declares or reaches more than that is **rejected**
  (`response_too_large`), never truncated, because a truncated page would be hashed and mined as if complete. A 60 s
  wall-clock cap per response stops slow-drip servers. robots.txt is read up to 500 KiB (RFC 9309).
- Each `Fetcher` owns a private urllib opener; the process-global opener is never modified.

## Open-data API hosts and diagnostics (v1.3.2)
`data.brreg.no`, `api.gleif.org` and `rdap.norid.no` publish APIs meant for programmatic use under open terms, so their
robots.txt is not consulted: a failing robots.txt on an API host must not switch the registry off (v1.3.1 did exactly
that: robots fail-closed applied to Brreg and every company failed). Company websites and Wikidata still obey robots.txt
and fail closed. The run report now lists `robots_by_host` with the decision and the underlying reason, error strings
carry the reason (`robots_unreachable:HTTP 503`), and `python run.py diagnose` checks the three sources from the
current machine and prints plain reasons.
