# Known gaps, licences and source restrictions
- Website coverage is low (about 7% of a random 100-company sample): most small registered companies have no findable site, and guessed domains are published only with corroboration.
- Static HTML only: JavaScript-rendered sites, vacancy listings on applicant-tracking platforms, and sites that block bots yield `not_available`/`blocked`.
- Hiring comes only from schema.org JobPosting data on the company's own pages (0% in the smoke sample). The NAV job feed is not used (needs an approved token).
- No reviews, ratings, sentiment, social-platform content or traffic data.
- Wikidata's robots.txt disallows this bot, so that connector returns `blocked`. Norid RDAP is used only as corroboration, and whether it exposes organisation numbers has not been verified live.
- Group structure covers registry superior units and LEI only.
- Source rights: Brønnøysund open data under NLOD 2.0; GLEIF open data; Wikidata CC0; Norid public registration data; company pages fetched under robots.txt and crawl delay, used only to extract facts with source links and hashes. No credentials, no LLM, no paid services; no secrets are needed.

## v1.3.0 notes
- Fail-closed robots handling means a host with a failing robots.txt (5xx/timeout) yields `blocked` for up to 5 minutes
  instead of being fetched; this trades some recall for compliance.
- The connection guard checks the resolved address; traffic through an operator-configured HTTP proxy relies on that proxy's own policy.

## v1.5.0 notes
- Not run live from the authoring environment (network blocked): live recall and the news/social filters on real sites are unmeasured until you run the 100-company smoke test.
- Site-wide feed items without a post-shaped URL or news category are withheld (precision over recall); plain-permalink blogs may lose items unless their sitemap or a news-scoped feed carries them.
- Duplicate input lines collapse to one envelope per distinct organisation number (reported, not silent).
- Browser rendering is not used: JavaScript-only sites yield nothing.
- `pytest` was unavailable in the authoring environment; the suite is `unittest`-based and pytest-compatible but only unittest was executed.

## v1.5.0 additions (second reviewer feedback)
- CMS starter posts ("Hello world!", "Hei, verden!", sample pages) and /hello-world/-style URLs are never news.
- Parked / domain-for-sale / default-server pages are rejected as company websites and never mined (unless the page prints the exact organisation number).
- LinkedIn: only /company/ pages are accepted; /in/ and /pub/ profiles are people and are rejected.
- Hiring: careers pages now also publish links to recruitment systems (Webcruiter, Teamtailor, Recruitee, ...) found on the company's own careers page.
- Revenue: the summary states the change vs the previous same-length year (percent); company pages show a revenue-by-year table.
- Viewer: sort, run-state filter, and a two-company compare view. Not verified on live sites; only offline tests.
