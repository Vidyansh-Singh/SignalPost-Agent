# Source and licence inventory (v1.5.0)

| Source | Used for | Access | Terms / licence | robots.txt | Cost |
|---|---|---|---|---|---|
| data.brreg.no Enhetsregisteret (`/enheter`, `/roller`, `/underenheter`) | identity, roles, registered workplaces, parent unit, registry website | open API, no key | NLOD 2.0 | not consulted (documented open API host) | 0 |
| data.brreg.no Regnskapsregisteret | filed annual accounts, history | open API, no key | NLOD 2.0 | not consulted (open API host) | 0 |
| api.gleif.org | LEI, only when the record states this exact organisation number | open API | CC0 | not consulted (open API host) | 0 |
| rdap.norid.no | corroborates that a .no domain's registrant is this organisation number | public RDAP | Norid RDAP terms | not consulted (protocol service) | 0 |
| query.wikidata.org | items carrying this organisation number (P2333) | SPARQL | CC0 | **obeyed**; currently disallowed for this bot, recorded as `blocked` | 0 |
| Company-owned website verified for that exact company (pages, feeds, sitemaps) | contact, jobs, news, locations, social links | public pages | the company's own terms | **obeyed** with crawl delay; fail-closed if robots.txt is unreachable | 0 |

Not used, by design and by test: LinkedIn, Meta, Glassdoor, Indeed, Google/Bing/Brave search or maps, search-result scraping, NAV/arbeidsplassen (needs an approved token), any LLM or paid API. The starter kit's `run_linkedin_guest_*` scripts are not part of this repository.
Secrets: none are required or read (`os.environ` is not used). Models: none. Third-party cost per official batch: 0 USD.
