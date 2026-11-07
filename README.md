# Signalpost bot

Evidence-backed public profiles for Norwegian organisation numbers. Python 3.11+, **standard library only** (no installs, no API keys, no paid services).

## Run (official command, any OS)
```
python run.py run --organisations companies.jsonl --output out/envelopes.jsonl --report out/report.json --run-id smoke-001 --expected-count 100 --state state --site site
```
(On Windows use `python` or `py`; on macOS/Linux `python3`. No environment variable or install step is needed.)
- `--organisations` takes the official JSONL manifest (objects with `organisation_number`), a JSON array, or a plain list of numbers. Starter-kit flags `--bulk`, `--profiles-output`, `--workers` are accepted.
- `--time-budget SECONDS` (optional, default off): at 70% of the budget external sources are skipped (registry data kept); at 95% any unfinished company gets a typed timeout envelope, so every input still returns one envelope.
- Default is 16 workers; progress prints every 10 companies.
- `--previous out/last-envelopes.jsonl` supplies the previous snapshot; material changes are written into each envelope's `changes`.
- Exit code is non-zero if envelope count != input count != `--expected-count`, if numbers are duplicated, or if any envelope breaks the contract.
- Outputs: `out/envelopes.jsonl` (one terminal envelope per input), `out/report.json` (requests, runtime, cost, per-family hit rate), optional `site/index.html`.
- `pip install -e .` also provides a `signalpost` command.

## Verify
```bash
python -m unittest discover -s tests -v    # offline, no network
python run.py validate --file out/envelopes.jsonl
```
The report's `envelopes` and `distinct_organisations` must both equal `inputs`, and `silent_drops` must be 0.

## Sources
Official: data.brreg.no (Enhetsregisteret, Regnskapsregisteret, roller, underenheter; NLOD 2.0). Open data: Wikidata (CC0), GLEIF.
Company-owned: the website verified for that exact company, under robots.txt and crawl-delay.
Wikidata's robots.txt disallows this bot on its query endpoint; the bot respects that and records `blocked` (low impact: very few small companies have items). Not used: LinkedIn/Meta/Glassdoor/Indeed clients, search-result scraping, any LLM, any paid API, NAV feed (needs an approved token).

## Rules the code enforces
- **Website identity gate** (publish only if one holds): (1) the exact organisation number is printed on the page; (2) the URL came from an official source (registry/Wikidata) and the legal name is on the page; (3) a guessed domain whose token equals the legal name exactly, with the name on the page, plus ONE of: the registered postal code/place, the full legal name with its legal form, the organisation number on its contact/about/privacy/terms page, or (for .no) the organisation number in Norid's public RDAP registrant record. Anything weaker is `ambiguous` and never published.
- **Not the group's or a brand's site:** a candidate is rejected (`ambiguous`) if it is also the registered website of the parent unit (checked up to two levels up the registry hierarchy), if it names itself as another entity (og:site_name / Organization markup / title), or if it publishes other organisation numbers without ours. Nothing (social, news, jobs, contact) is mined from a rejected site.
- **Social accounts:** a link counts only if the account handle carries the company's own name (or the site's own Organization markup lists it in `sameAs`). Agency, theme-vendor, platform and parent-group links are dropped; company pages only on LinkedIn (no people, schools, showcases). Two competing accounts on one network are `ambiguous`, never both published.
- **News:** only individually addressable posts with a real publication date (JSON-LD `datePublished`, `article:published_time`, RSS `pubDate`). `dateModified`, Atom `<updated>`, sitemap `<lastmod>` and retrieval time are never publication dates. Menu labels ("See more news"), product/service pages, comment feeds and listing pages are not news. Items from a site-wide feed need a post-shaped URL or a news category, otherwise they are recorded as `ambiguous` candidates.
- **Latest accounts:** the newest filing by period end; company accounts beat consolidated for the same period; liquidation accounts never displace a normal filing; short periods are flagged. `financials|history` lists only 12-month, same-basis filings, oldest first; a missing line item is `null`, never 0.
- **Registered workplaces:** every page of the register's sub-unit list is read; the count is the register's own total; an incomplete list is reported as such.
- Absence is a typed state with a note, never zero. Available claims need evidence, entity link, hash, span; financial and activity claims need a date.
- Re-runs overwrite per-company state (idempotent) and emit typed changes. A source that could not be read (failed/blocked) never produces a change and never overwrites the last supported value.
- Interrupted batches resume with `--resume` (ledger of finished companies); duplicated input lines are counted in the report (`input_lines`, `distinct_inputs`, `duplicate_input_lines`) and get one envelope per distinct organisation number.

## Commands
```
python run.py run --organisations in.jsonl --output out/envelopes.jsonl --report out/report.json --run-id r1 --expected-count 100 --site site [--resume] [--previous old.jsonl] [--time-budget SECONDS]
python run.py validate --file out/envelopes.jsonl                         # contract check, exit 1 on any issue
python run.py measure  --file out/envelopes.jsonl --inputs in.jsonl       # local consistency metrics (not an official score)
python run.py diagnose                                                    # are the data sources reachable from this machine?
python -m unittest discover -s tests                                      # offline suite (pytest also runs it if installed)
```

## Companion website (JBox)

A public, read-only explorer for the agent's output, built with JBox for the Signalpost JBOX bonus:
**https://product-signalpost-explorer-a-public-web.sites.jboxai.com/**

- It shows the 100-company smoke run produced by this agent (`reports/smoke-companies.jsonl` -> envelopes). No data is invented or added on the site.
- Find a company by name or organisation number, filter (verified website, dated news, industry), sort, open a company, and compare two.
- Each fact has a Source panel (URL, retrieved time, quoted text). Unknowns show a reason and are never shown as zero.
- The agent itself does not depend on the site. The agent's own offline viewer is generated with `--site out/site`.

## Limitations
Recall is bounded by free official sources and company-owned pages. Registry families are complete on the sample; website-derived families (website, social, contact, news, hiring) are sparse because most sampled companies have no verifiable own website. Measured by the author on 6 Nov 2026 (Windows, Python 3.14, live network, v1.5.0): 126 offline tests pass; 100-company smoke run gave 100 envelopes, 0 contract issues, 0 failed, $0 cost; `measure` reports publication_ready=true. This is internal consistency, not an official score. See `reports/`.

## Repository map
`run.py` entry point | `src/signalpost/` code | `tests/` offline tests | `reports/` 100-company smoke evidence | `AGENT.md` policy | `CRAWLERS.md` connectors & budgets | `IDENTITY_RESOLUTION.md` gates | `DATA_SCHEMA.md` envelope | `REFRESH.md` snapshots & diffs | `EVAL.md` evaluation | `LIMITATIONS.md` gaps & source rights | `requirements.txt` (no dependencies) | `pyproject.toml`.

### Connectivity check
`python run.py diagnose` prints OK/FAIL for the registry and GLEIF with the reason. Run it first if a run returns no data.
