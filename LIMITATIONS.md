# Known gaps, licences and source restrictions
- Website coverage is low (about 7% of a random 100-company sample): most small registered companies have no findable site, and guessed domains are published only with corroboration.
- Static HTML only: JavaScript-rendered sites, vacancy listings on applicant-tracking platforms, and sites that block bots yield `not_available`/`blocked`.
- Hiring comes only from schema.org JobPosting data on the company's own pages (0% in the smoke sample). The NAV job feed is not used (needs an approved token).
- No reviews, ratings, sentiment, social-platform content or traffic data.
- Wikidata's robots.txt disallows this bot, so that connector returns `blocked`. Norid RDAP is used only as corroboration, and whether it exposes organisation numbers has not been verified live.
- Group structure covers registry superior units and LEI only.
- Source rights: Brønnøysund open data under NLOD 2.0; GLEIF open data; Wikidata CC0; Norid public registration data; company pages fetched under robots.txt and crawl delay, used only to extract facts with source links and hashes. No credentials, no LLM, no paid services; no secrets are needed.
