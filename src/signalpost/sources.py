"""Source mappers: official registry, open-data connectors, company-site extraction."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from .core import Claim, Ctx, EntityLink, find_orgnums
from .fetch import FetchResult, Fetcher

ER = "https://data.brreg.no/enhetsregisteret/api"
RR = "https://data.brreg.no/regnskapsregisteret/regnskap"
REG_POLICY = "Brønnøysundregistrene open data (Enhetsregisteret/Regnskapsregisteret), NLOD 2.0"
WD_POLICY = "Wikidata SPARQL endpoint, content CC0"
GLEIF_POLICY = "GLEIF LEI public API, open data"
SITE_POLICY = "company-owned public page, retrieved under robots.txt and crawl-delay"


def _fmt_addr(a: dict) -> str:
    lines = a.get("adresse")
    street = " ".join(lines) if isinstance(lines, list) else (lines or "")
    return ", ".join(str(x) for x in (street, a.get("postnummer"), a.get("poststed")) if x)


# ------------------------------------------------------------------ registry
def registry_identity(c: Ctx, d: dict, r: FetchResult) -> dict:
    link = c.link("official_registry_exact", "organisasjonsnummer")
    ident: dict[str, Any] = {"organisation_number": c.org, "status_flags": []}

    def add(fam, fld, val, sel, conf=0.99):
        eid = c.ev(r, "official_registry", REG_POLICY, sel, f"{fld}: {val}", "brreg_enhet.v1")
        c.add(fam, fld, val, eid, link, conf)

    name = d.get("navn")
    if isinstance(name, str) and name.strip():
        ident["legal_name"] = name.strip()
        add("legal_identity", "legal_name", name.strip(), "$.navn")
    else:
        c.absent("legal_identity", "legal_name", "not_available", "registry record has no name")
    form = (d.get("organisasjonsform") or {}).get("beskrivelse")
    if form:
        ident["organisation_form"] = form
        add("legal_identity", "organisation_form", form, "$.organisasjonsform.beskrivelse")
    for key, fld in (("registreringsdatoEnhetsregisteret", "registration_date"),
                     ("stiftelsesdato", "incorporation_date")):
        if d.get(key):
            add("legal_identity", fld, str(d[key])[:10], f"$.{key}")
    nk = d.get("naeringskode1") or {}
    if nk.get("beskrivelse"):
        add("business_profile", "industry_description", nk["beskrivelse"], "$.naeringskode1.beskrivelse", 0.97)
    if nk.get("kode"):
        add("business_profile", "industry_code", nk["kode"], "$.naeringskode1.kode", 0.97)
    for key, fld in (("konkurs", "bankrupt"), ("underAvvikling", "in_liquidation"),
                     ("underTvangsavviklingEllerTvangsopplosning", "forced_liquidation")):
        v = d.get(key)
        if isinstance(v, bool):
            add("legal_identity", fld, v, f"$.{key}")
            if v:
                ident["status_flags"].append(fld)
    emp = d.get("antallAnsatte")
    if isinstance(emp, int) and not isinstance(emp, bool):
        add("workforce", "registered_employees", emp, "$.antallAnsatte", 0.95)
    else:
        c.absent("workforce", "registered_employees", "not_available",
                 "registry reports no employee count (absence is not zero)")
    addr = d.get("forretningsadresse")
    if isinstance(addr, dict):
        ident["postal_code"] = str(addr.get("postnummer") or "") or None
        ident["postal_place"] = str(addr.get("poststed") or "") or None
        ident["municipality"] = str(addr.get("kommune") or "") or None
    if isinstance(addr, dict) and _fmt_addr(addr):
        add("locations", "registered_business_address", _fmt_addr(addr), "$.forretningsadresse", 0.97)
    parent = d.get("overordnetEnhet")
    if isinstance(parent, str) and parent.isdigit():
        ident["parent_orgnr"] = parent
        add("group_structure", "parent_unit", parent, "$.overordnetEnhet", 0.95)
    else:
        c.absent("group_structure", "relationships", "not_available",
                 "registry record declares no superior unit")
    site = d.get("hjemmeside")
    ident["registry_website_raw"] = site if isinstance(site, str) else None
    return ident


def registry_roles(c: Ctx, d: dict, r: FetchResult) -> None:
    link = c.link("official_registry_roles", "organisasjonsnummer")
    people = []
    for grp in d.get("rollegrupper") or []:
        for role in grp.get("roller") or []:
            if role.get("fratraadt"):
                continue
            rt = role.get("type") or {}
            code, desc = rt.get("kode") or "UKJENT", rt.get("beskrivelse") or rt.get("kode") or "role"
            nm = (role.get("person") or {}).get("navn") or {}
            full = " ".join(x for x in (nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")) if x) \
                if isinstance(nm, dict) else str(nm)
            if not full:
                full = (role.get("enhet") or {}).get("navn") or ""
                if isinstance(full, list):
                    full = full[0] if full else ""
            if full:  # birth dates are deliberately never read
                people.append((code, desc, full.strip()))
    if not people:
        c.absent("leadership", "role_holders", "not_available", "roles endpoint returned no active role holder")
        return
    for i, (code, desc, full) in enumerate(people[:40]):
        eid = c.ev(r, "official_registry", REG_POLICY, f"$.rollegrupper[].roller[{i}]",
                   f"{desc}: {full}", "brreg_roller.v1")
        c.add("leadership", f"role_{code.lower()}", full, eid, link, 0.97,
              key_extra=f"{i}:{full.lower()}", note=desc)
    eid = c.ev(r, "official_registry", REG_POLICY, "$.rollegrupper",
               f"{len(people)} active role assignments", "brreg_roller.v1")
    c.add("leadership", "role_holder_count", len(people), eid, link, 0.98)


SUBUNIT_PAGE_SIZE, SUBUNIT_MAX_PAGES, SUBUNIT_CLAIM_CAP = 100, 10, 100


def fetch_subunits(f: Fetcher, org: str):
    """All registered sub-units, page by page (`page`/`size`, totals from the register's own `page` block).
    Returns (payload, first_response). payload is None only when the first page itself failed; later failures are
    reported through `_failed_pages` so a partial list is never presented as complete."""
    base = f"{ER}/underenheter?overordnetEnhet={org}&size={SUBUNIT_PAGE_SIZE}"
    first, r0 = f.get_json(base + "&page=0", ttl=21_600)
    if not isinstance(first, dict):
        return None, r0
    rows = list(((first.get("_embedded") or {}).get("underenheter")) or [])
    page = first.get("page") if isinstance(first.get("page"), dict) else {}
    total_pages = page.get("totalPages") if isinstance(page.get("totalPages"), int) else 1
    failed: list[int] = []
    for n in range(1, min(total_pages, SUBUNIT_MAX_PAGES)):
        d, rr = f.get_json(base + f"&page={n}", ttl=21_600)
        if isinstance(d, dict):
            rows += list(((d.get("_embedded") or {}).get("underenheter")) or [])
        else:
            failed.append(n)
    return {"_embedded": {"underenheter": rows}, "page": page, "_failed_pages": failed,
            "_pages_total": total_pages, "_pages_read": min(total_pages, SUBUNIT_MAX_PAGES)}, r0


def registry_subunits(c: Ctx, d: dict, r: FetchResult) -> None:
    link = c.link("official_subunit_parent_link", "overordnetEnhet")
    rows = [u for u in (((d.get("_embedded") or {}).get("underenheter")) or []) if isinstance(u, dict)]
    page = d.get("page") if isinstance(d.get("page"), dict) else {}
    declared = page.get("totalElements") if isinstance(page.get("totalElements"), int) and not isinstance(page.get("totalElements"), bool) else None
    if not rows and not declared:
        c.absent("locations", "registered_workplaces", "not_available", "no sub-units registered for this unit")
        return
    seen: set[str] = set()
    kept = 0
    for i, u in enumerate(rows):
        so = str(u.get("organisasjonsnummer") or "")
        if so in seen:
            continue                      # same sub-unit returned twice (paging overlap): one fact
        seen.add(so)
        if kept >= SUBUNIT_CLAIM_CAP:
            continue
        pretty = _fmt_addr(u.get("beliggenhetsadresse") or {})
        nm = u.get("navn") or so
        eid = c.ev(r, "official_registry", REG_POLICY, f"$._embedded.underenheter[{i}]",
                   f"{so} {nm} — {pretty}", "brreg_underenheter.v1")
        c.add("locations", "registered_workplace",
              {"organisation_number": so or None, "name": nm, "address": pretty or None,
               "employees": u.get("antallAnsatte"), "parent_organisation_number": c.org},
              eid, link, 0.96, key_extra=so or str(i))
        kept += 1
    # The count is the register's own total when it states one; otherwise only what was actually read.
    count = declared if declared is not None else len(seen)
    sel = "$.page.totalElements" if declared is not None else "$._embedded.underenheter"
    eid = c.ev(r, "official_registry", REG_POLICY, sel, f"{count} registered sub-units", "brreg_underenheter.v1")
    c.add("locations", "registered_workplace_count", count, eid, link, 0.97)
    failed = d.get("_failed_pages") or []
    cut = d.get("_pages_total", 1) > d.get("_pages_read", 1)
    if failed or cut or len(seen) < (declared or 0) or kept < len(seen):
        why = []
        if failed:
            why.append(f"page(s) {failed} could not be retrieved")
        if cut:
            why.append(f"only the first {d.get('_pages_read')} of {d.get('_pages_total')} pages were read")
        if kept < len(seen):
            why.append(f"only the first {kept} sub-units are listed individually")
        c.absent("locations", "registered_workplaces_complete", "failed" if failed else "ambiguous",
                 "sub-unit list is incomplete: " + "; ".join(why or [f"{len(seen)} of {declared} read"]))


_FIN = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")),
    ("operating_cost", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad")),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat")),
    ("profit_for_year", ("resultatregnskapResultat", "aarsresultat")),
    ("total_assets", ("eiendeler", "sumEiendeler")),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital")),
]


def _dig(o: Any, path) -> Any:
    for k in path:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def _d(x: Any):
    from datetime import date
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def pick_accounts(rows: list[dict]) -> tuple[dict | None, dict]:
    """Choose the newest filing. Rows without a valid period end are ignored; liquidation accounts only count when
    nothing else exists; for the same period end the company's own accounts (SELSKAP) win over consolidated ones.
    Returns (row, info) where info describes basis and period length so nothing is compared across unlike periods."""
    ok = []
    for x in rows:
        til, fra = _d(_dig(x, ("regnskapsperiode", "tilDato"))), _d(_dig(x, ("regnskapsperiode", "fraDato")))
        if til:
            ok.append((x, til, fra))
    if not ok:
        return None, {}
    normal = [t for t in ok if not t[0].get("avviklingsregnskap")] or ok
    best = max(normal, key=lambda t: (t[1], str(t[0].get("regnskapstype") or "").upper() == "SELSKAP"))
    row, til, fra = best
    days = (til - fra).days + 1 if fra else None
    months = round(days / 30.4375) if days else None
    return row, {"til": til.isoformat(), "fra": fra.isoformat() if fra else None, "months": months,
                 "basis": str(row.get("regnskapstype") or "").upper() or None,
                 "liquidation": bool(row.get("avviklingsregnskap")), "filings": len(ok)}


def account_history(rows: list[dict], latest: dict, info: dict) -> list[dict]:
    """Comparable filings, oldest first. Same accounting basis as the latest, 12-month periods only, no liquidation
    accounts, one filing per period end. Needs at least two to be a comparison at all."""
    basis = info.get("basis")
    by_end: dict[str, dict] = {}
    for x in rows:
        til, fra = _d(_dig(x, ("regnskapsperiode", "tilDato"))), _d(_dig(x, ("regnskapsperiode", "fraDato")))
        if not til or not fra or x.get("avviklingsregnskap"):
            continue
        if str(x.get("regnskapstype") or "").upper() != (basis or ""):
            continue
        if round(((til - fra).days + 1) / 30.4375) != 12:
            continue
        by_end.setdefault(til.isoformat(), x)
    if info.get("months") != 12 or len(by_end) < 2:
        return []
    out = []
    for end in sorted(by_end)[-5:]:
        x = by_end[end]
        item: dict[str, Any] = {"period": end, "months": 12}
        for fld, path in _FIN:
            v = _dig(x, path)
            item[fld] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append(item)
    return out


def registry_accounts(c: Ctx, payload: Any, r: FetchResult) -> None:
    rows = [x for x in (payload if isinstance(payload, list) else [payload]) if isinstance(x, dict)]
    row, info = pick_accounts(rows)
    if not row:
        c.absent("financials", "latest_annual_accounts", "not_available",
                 "accounts register holds no filed annual accounts with a valid period (absence is not zero)")
        return
    link = c.link("official_accounts_lookup", "organisasjonsnummer")
    period, unit = info["til"], row.get("valuta") or "NOK"
    tag = f"{info['months']} months" if info["months"] else "period length unknown"
    extra = ", ".join(x for x in (info["basis"] and f"{info['basis'].lower()} accounts", tag,
                                  "liquidation accounts" if info["liquidation"] else None) if x)
    note = f"newest of {info['filings']} filing(s); {extra}" + ("; not a 12-month period, do not compare with other years"
                                                                  if info["months"] and info["months"] != 12 else "")
    for fld, path in _FIN:
        v = _dig(row, path)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            c.absent("financials", f"latest_{fld}", "not_available",
                     f"filed accounts ({period}) do not expose this line item")
            continue
        eid = c.ev(r, "official_registry", REG_POLICY, "$." + ".".join(path),
                   f"{fld} {v} {unit} ({period}; {extra})", "brreg_regnskap.v1", period=period)
        c.add("financials", f"latest_{fld}", v, eid, link, 0.98, unit=unit, period=period, note=note)
    eid = c.ev(r, "official_registry", REG_POLICY, "$.regnskapsperiode",
               f"accounting period ending {period} ({extra})", "brreg_regnskap.v1", period=period)
    c.add("financials", "latest_period", period, eid, link, 0.99, period=period, note=note)
    hist = account_history(rows, row, info)
    if hist:
        eid = c.ev(r, "official_registry", REG_POLICY, "$[*].regnskapsperiode",
                   "; ".join(f"{h['period']} ({h['months']} months)" for h in hist), "brreg_regnskap.v1", period=hist[-1]["period"])
        c.add("financials", "history", hist, eid, link, 0.97, unit=unit, period=hist[-1]["period"],
              note="filed periods oldest to newest; only 12-month, non-liquidation filings on the same accounting basis as the "
                   "latest filing, so every adjacent pair is like-for-like. A missing line item is null, never 0")


# ------------------------------------------------------------ open connectors
SPARQL = ('SELECT ?item ?itemLabel ?site WHERE { ?item wdt:P2333 "%s" . '
          'OPTIONAL { ?item wdt:P856 ?site } '
          'SERVICE wikibase:label { bd:serviceParam wikibase:language "nb,en" } } LIMIT 5')


def wikidata(c: Ctx, f: Fetcher) -> None:
    url = f"https://query.wikidata.org/sparql?query={quote(SPARQL % c.org)}&format=json"
    data, r = f.get_json(url, ttl=604_800)
    if data is None:
        st = "blocked" if r.blocked_by_robots else "failed"
        c.absent("digital_presence", "wikidata_entity", st, f"wikidata lookup unsuccessful ({r.error})")
        return
    rows = ((data.get("results") or {}).get("bindings")) or []
    if not rows:
        c.absent("digital_presence", "wikidata_entity", "not_available",
                 "no Wikidata item carries this organisation number (P2333)")
        return
    row, link = rows[0], c.link("wikidata_p2333_exact", "P2333", 0.99)
    qid = ((row.get("item") or {}).get("value") or "").rsplit("/", 1)[-1]
    if qid:
        eid = c.ev(r, "open_data_api", WD_POLICY, "$.bindings[0].item", f"{qid} has P2333={c.org}", "wikidata.v1")
        c.add("digital_presence", "wikidata_entity", qid, eid, link, 0.93)
    site = (row.get("site") or {}).get("value")
    if site:
        eid = c.ev(r, "open_data_api", WD_POLICY, "$.bindings[0].site", f"P856 {site}", "wikidata.v1")
        c.add("digital_presence", "wikidata_official_website", site, eid, link, 0.88)


def gleif(c: Ctx, f: Fetcher) -> None:
    url = (f"https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]={c.org}"
           "&filter[entity.legalAddress.country]=NO&page[size]=5")
    data, r = f.get_json(url, ttl=604_800)
    if data is None:
        st = "blocked" if r.blocked_by_robots else "failed"
        c.absent("group_structure", "lei", st, f"GLEIF lookup unsuccessful ({r.error})")
        return
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        c.absent("group_structure", "lei", "failed", "GLEIF response has an unexpected shape (no `data` list)")
        return
    # A filter is a request, not proof: accept only records whose own attributes carry exactly this registry number.
    good = []
    for row in rows:
        ent = ((row or {}).get("attributes") or {}).get("entity") or {}
        country = (ent.get("legalAddress") or {}).get("country")
        lei = (row or {}).get("id")
        if isinstance(lei, str) and re.fullmatch(r"[A-Z0-9]{20}", lei) and str(ent.get("registeredAs") or "").replace(" ", "") == c.org \
                and country in (None, "NO"):
            good.append(lei)
    good = sorted(set(good))
    if not good:
        c.absent("group_structure", "lei", "not_available", "no LEI record carries this organisation number" if not rows else
                 "GLEIF returned records, but none states this exact organisation number as its registration")
        return
    if len(good) > 1:
        c.absent("group_structure", "lei", "ambiguous", f"{len(good)} LEI records state this organisation number",
                 alternatives=[{"lei": x} for x in good[:5]])
        return
    eid = c.ev(r, "open_data_api", GLEIF_POLICY, "$.data[0].attributes.entity.registeredAs",
               f"LEI {good[0]} registeredAs {c.org}", "gleif.v2")
    c.add("group_structure", "lei", good[0], eid, c.link("gleif_registered_as_exact", "entity.registeredAs", 0.99), 0.94)


# ----------------------------------------------------------------- html parse
class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.feeds: list[str] = []
        self.times: list[tuple[str, str]] = []
        self.title = ""
        self.names: list[str] = []      # og:site_name / application-name: how the site names itself
        self.pubmeta: list[str] = []    # article:published_time / itemprop=datePublished (publication dates only)
        self.h1: list[str] = []
        self._h1 = False
        self._skip = 0
        self._ld = False
        self._ld_buf: list[str] = []
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            if tag == "script" and "ld+json" in (a.get("type") or "").lower():
                self._ld, self._ld_buf = True, []
            else:
                self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and a.get("content") and (a.get("property") or a.get("name") or "").lower() in ("og:site_name", "application-name"):
            self.names.append(a["content"].strip())
        elif tag == "meta" and a.get("content") and ((a.get("property") or a.get("name") or "").lower() == "article:published_time"
                                                      or (a.get("itemprop") or "").lower() == "datepublished"):
            self.pubmeta.append(a["content"].strip())
        elif tag == "h1":
            self._h1 = True
            self.h1.append("")
        elif tag == "a" and a.get("href"):
            self._a = [a["href"], []]
        elif tag == "link" and a.get("href") and any(k in (a.get("type") or "").lower() for k in ("rss", "atom")):
            self.feeds.append(a["href"])
        elif tag == "time" and a.get("datetime"):
            self.times.append((a["datetime"], ""))

    def handle_endtag(self, tag):
        if tag == "script" and self._ld:
            self.jsonld.append("".join(self._ld_buf))
            self._ld = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._h1 = False
        elif tag == "a" and self._a:
            self.links.append((self._a[0], " ".join("".join(self._a[1]).split())))
            self._a = None

    def handle_data(self, data):
        if self._ld:
            self._ld_buf.append(data)
        elif not self._skip:
            self.text.append(data)
            if self._h1 and self.h1:
                self.h1[-1] += data
            if self._in_title:
                self.title += data
            if self._a:
                self._a[1].append(data)

    @property
    def body(self) -> str:
        return " ".join(" ".join(self.text).split())

    def ld_objects(self):
        for raw in self.jsonld:
            try:
                data = json.loads(raw.strip())
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    yield cur
                    stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
                elif isinstance(cur, list):
                    stack.extend(cur)


def parse(r: FetchResult) -> Page:
    p = Page()
    try:
        p.feed(r.text)
        p.close()
    except Exception:
        pass
    return p


def _types(o: dict) -> set[str]:
    t = o.get("@type")
    return {t.lower()} if isinstance(t, str) else {str(x).lower() for x in t} if isinstance(t, list) else set()


# ------------------------------------------------------------------ website
SUFFIX = re.compile(r"\b(as|asa|ans|da|ba|sa|nuf|enk|kf|ks|if|stiftelse)\b", re.I)
BUCKETS = {
    "contact": ("kontakt", "contact"),
    "about": ("om-oss", "omoss", "about"),
    "careers": ("karriere", "jobb", "stilling", "career", "jobs", "ledige"),
    "news": ("nyheter", "aktuelt", "news", "presse", "press", "blogg", "blog"),
    "locations": ("avdeling", "lokasjon", "kontorer", "locations", "offices", "finn-oss"),
    "team": ("ansatte", "team", "ledelse", "management", "people"),
}


def slug(s: str) -> str:
    s = SUFFIX.sub(" ", (s or "").lower()).replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"[^a-z0-9]", "", s)


def _words(name: str) -> list[str]:
    n = SUFFIX.sub(" ", (name or "").lower()).replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", n).split() if w]


def candidates(name: str, registry_raw: str | None, extra: list[str], limit: int = 8) -> list[str]:
    """Candidate homepages only. Nothing here is evidence; verify_site is the gate.
    Order: official/registry-provided first, then generated guesses by likelihood."""
    out: list[str] = []

    def push(u):
        u = (u or "").strip()
        if not u:
            return
        if not u.startswith(("http://", "https://")):
            u = "https://" + u.lstrip("/")
        p = urlparse(u)
        if p.netloc and "." in p.netloc:
            n = f"{p.scheme}://{p.netloc}/"
            if n not in out:
                out.append(n)

    push(registry_raw)
    for e in extra:
        push(e)
    w = _words(name)
    if w:
        joined, hyph = "".join(w), "-".join(w)
        stems = [joined]
        if len(w) > 1:
            stems.append(hyph)
            if len(w[0]) >= 5:
                stems.append(w[0])
        for st in stems:
            if 4 <= len(st) <= 40:
                push(f"https://www.{st}.no")
        for st in stems[:1]:
            if 4 <= len(st) <= 40:
                push(f"https://{st}.com")
    return out[:limit]


# Words that do not distinguish one company from another; ignored when asking "does this site name this company?"
GENERIC = frozenset("""norge norway norsk nordic scandinavia europe international group gruppen holding holdings invest
investment eiendom eiendomsutvikling drift service services consulting consult solutions teknikk industri transport handel
shipping maritime energy energi media design partner partners utvikling the and og avd kommunikasjon rederi""".split())


def _tokens(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 3 and w not in GENERIC}


def _declared_names(pg) -> list[str]:
    """How the page names its own owner: og:site_name, application-name and the schema.org Organization name."""
    out = list(pg.names)
    for o in pg.ld_objects():
        if _types(o) & {"organization", "corporation", "localbusiness", "store", "ngo"}:
            for k in ("legalName", "name"):
                if isinstance(o.get(k), str) and o[k].strip():
                    out.append(o[k].strip())
    return out


def identity_conflict(name: str, pg, host_exact: bool = False) -> str | None:
    """None when the page plausibly belongs to `name`; otherwise why it looks like someone else's site (a parent
    group, a brand, an agency). A mention of the name in body text never makes a site the company's own."""
    mine = _tokens(name)
    if not mine:
        return None
    declared = {t for n in _declared_names(pg) for t in _tokens(n)}
    if declared:
        if not declared & mine:
            return f"site names itself '{_declared_names(pg)[0][:60]}', not the registered company"
        if not host_exact and declared < mine and (mine - declared):
            return (f"site names itself '{_declared_names(pg)[0][:60]}', which is narrower than the registered name "
                    "(parent group or brand site)")
        return None
    head = _tokens(pg.title) | {t for h in pg.h1[:2] for t in _tokens(h)}
    if head and not head & mine and not host_exact:
        return "title/heading name a different organisation; the registered name appears only in body text"
    return None


def _norm_text(t: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (t or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")) + " "


def _located(text: str, places: list[str], postal: str | None) -> list[str]:
    """Corroboration that a page belongs to the registered place: exact postal code or place name as a word."""
    hits, t = [], _norm_text(text)
    if postal and re.fullmatch(r"\d{4}", postal) and f" {postal} " in t:
        hits.append("postal_code")
    for pl in places:
        w = _norm_text(pl).strip()
        if len(w) >= 3 and f" {w} " in t:
            hits.append("place:" + w)
            break
    return hits


def _form_phrase(name: str, text: str) -> bool:
    """True when the full legal name INCLUDING its legal form (e.g. 'Wedo AS') appears as a phrase."""
    m = re.search(r"\b(as|asa|ans|da|ba|sa|nuf|enk|kf|ks|if)\s*$", (name or "").lower().strip())
    words = _words(name)
    if not m or not words:
        return False
    return f" {' '.join(words)} {m.group(1)} " in _norm_text(text)


def _norid_registrant(c: Ctx, f: Fetcher, host: str):
    """For .no domains: Norid's public RDAP record may list the registrant organisation number.
    Schema-agnostic: we only look for OUR exact organisation number in the response."""
    host = host.lower().removeprefix("www.")
    if not host.endswith(".no"):
        return None
    dom = ".".join(host.split(".")[-2:])
    r = f.get(f"https://rdap.norid.no/domain/{dom}", ttl=604_800, accept="application/rdap+json")
    if r.ok and c.org in find_orgnums(r.text):
        return r, dom
    return None


def rd_ok(c: Ctx, rd, final: str, name: str, home_r, home_pg):
    r, dom = rd
    eid = c.ev(r, "open_data_api", "Norid RDAP public domain registration data", "rdap:registrant",
               f"registrant record for {dom} contains organisation number {c.org}", "norid_rdap.v1", url=r.url)
    link = EntityLink(c.org, "norid_rdap_registrant_orgnr", 1.0, ["domain_registrant_organisation_number"])
    c.add("digital_presence", "official_website", final, eid, link, 0.97)
    return final, home_r, home_pg, link  # later mining must use the homepage, not the RDAP record


def _host_token(url: str) -> str:
    return slug(urlparse(url).netloc.lower().removeprefix("www.").rsplit(".", 1)[0])


def verify_site(c: Ctx, f: Fetcher, name: str, cands: list[str], *, official: set[str] | None = None,
                places: list[str] | None = None, postal: str | None = None, group_hosts: set[str] | None = None):
    """Identity gate. Publishes only when ONE of these holds:
      1. the exact organisation number is printed on the page;
      2. the candidate came from an official source (registry/Wikidata) AND the legal name is on the page;
      3. a guessed domain whose token equals the legal name exactly, the legal name is on the page, AND the page
         (or its contact/about page) shows the registered postal code or place.
    Everything weaker is recorded as ambiguous and never published."""
    official = official or set()
    group_hosts = group_hosts or set()
    places = [p for p in (places or []) if p]
    state, note, alts = "not_available", "no candidate satisfied the identity gate", []
    nm = slug(name)
    for url in cands:
        r = f.get(url, ttl=43_200)
        if r.blocked_by_robots:
            if state == "not_available":
                state, note = "blocked", f"{url} disallowed by robots.txt"
            continue
        if not r.ok or not r.is_html:
            continue
        pg = parse(r)
        body, found = pg.body, find_orgnums(pg.body)
        final = r.final_url or url
        parked = parked_reason(pg, final)
        if parked and c.org not in found:
            if state == "not_available":
                state, note = "not_available", f"candidate {final} rejected: {parked}"
            alts.append({"url": final, "reason": "parked_or_for_sale_domain", "detail": parked})
            continue
        host_exact = bool(nm) and _host_token(final) == nm
        fhost = urlparse(final).netloc.lower().removeprefix("www.")
        if (fhost in group_hosts or urlparse(url).netloc.lower().removeprefix("www.") in group_hosts) and not (c.org in found and len(found) == 1):
            # The registry lists the SAME site for the parent unit: it is the group's site unless it identifies only us.
            state, note = "ambiguous", "site is also the registered website of the parent unit; treated as the group's site, not this company's"
            alts.append({"url": final, "reason": "parent_unit_website"})
            continue
        if c.org in found and (len(found) <= 2 or not identity_conflict(name, pg, host_exact)):
            return _site_ok(c, r, pg, final, "orgnr_published_on_site", 1.0, ["organisasjonsnummer"], c.org)
        if c.org not in found and found:
            state, note = "ambiguous", f"site publishes a different organisation number ({sorted(found)[0]})"
            alts.append({"url": final, "reason": "conflicting_orgnr"})
            continue
        name_in = [k for k, v in (("legal_name_in_body", body), ("legal_name_in_title", pg.title)) if nm and nm in slug(v)]
        is_official = urlparse(url).netloc.lower().removeprefix("www.") in official
        other = identity_conflict(name, pg, host_exact)
        if other and (name_in or host_exact or is_official or c.org in found):
            state, note = "ambiguous", f"candidate rejected as not the company's own site: {other}"
            alts.append({"url": final, "reason": "belongs_to_other_entity", "detail": other})
            continue
        if is_official and name_in:
            return _site_ok(c, r, pg, final, "official_source_candidate_name_match", 0.95,
                            ["official_source_listing"] + name_in, name)
        if host_exact and name_in:
            base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
            loc = _located(body, places, postal)
            if _form_phrase(name, body):
                loc = loc + ["legal_name_with_legal_form_on_page"]
            if not loc:
                rd = _norid_registrant(c, f, urlparse(final).netloc)
                if rd:
                    return rd_ok(c, rd, final, name, r, pg)
                # contact/about/privacy/terms pages carry address, legal form, often the organisation number
                keys = BUCKETS["contact"] + BUCKETS["about"] + ("personvern", "privacy", "vilkar", "terms", "impressum")
                hops = []
                for h, label in pg.links:
                    u = urljoin(base + "/", h).split("#")[0]
                    low = (urlparse(u).path + " " + label).lower()
                    if urlparse(u).netloc == urlparse(final).netloc and any(k in low for k in keys) and u not in hops:
                        hops.append(u)
                for u in hops[:4]:
                    rr = f.get(u, ttl=43_200)
                    if not (rr.ok and rr.is_html):
                        continue
                    hp = parse(rr)
                    if c.org in find_orgnums(hp.body):
                        got = _site_ok(c, rr, hp, final, "orgnr_published_on_site", 1.0, ["organisasjonsnummer"], c.org, ev_url=u)
                        return got[0], r, pg, got[3]  # evidence = the hop page; mining continues on the homepage
                    loc = _located(hp.body, places, postal) + (["legal_name_with_legal_form_on_page"] if _form_phrase(name, hp.body) else [])
                    if loc:
                        break
            if loc:
                return _site_ok(c, r, pg, final, "exact_name_domain_and_registered_location", 0.9,
                                ["domain_equals_legal_name"] + name_in + loc, name)
        if name_in or host_exact:
            if state in ("not_available", "blocked"):
                state, note = "ambiguous", "name/domain resembles the company but nothing ties it to the registered entity or location"
            alts.append({"url": final, "matched_on": (["domain_equals_legal_name"] if host_exact else []) + name_in})
    c.absent("digital_presence", "official_website", state, note, alternatives=alts[:5])
    return None


def _site_ok(c, r, pg, final, method, score, on, span, ev_url=None):
    eid = c.ev(r, "company_owned", SITE_POLICY, "html:body", f"identity confirmed by {', '.join(on)}: {span}",
               "website_gate.v1", url=ev_url or final)
    link = EntityLink(c.org, method, score, on)
    c.add("digital_presence", "official_website", final, eid, link, min(0.99, score))
    return final, r, pg, link


SOCIAL = {"linkedin.com": "linkedin", "facebook.com": "facebook", "instagram.com": "instagram",
          "x.com": "x", "twitter.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok"}
BAD = ("/share", "/sharer", "/intent", "/dialog", "login", "signup", "/plugins", "privacy")


PLATFORM_OWN = {"facebook", "instagram", "linkedin", "twitter", "x", "youtube", "tiktok", "google", "sharer", "share",
                "wordpress", "wix", "squarespace", "shopify", "elementor", "webflow", "envato", "themeforest", "godaddy",
                "jimdo", "weebly", "hubspot", "mailchimp"}


def _handle(plat: str, pr) -> str | None:
    """The account identifier of a profile URL, or None when the URL is not a company profile."""
    parts = [x for x in pr.path.split("/") if x]
    if not parts:
        return None
    first = parts[0].lower()
    if plat == "linkedin":
        return parts[1] if first == "company" and len(parts) > 1 else None  # /in/ = a person; school/showcase are not the company
    if plat == "facebook":
        if first in ("pages", "p") and len(parts) > 1:
            return parts[1]
        return None if first in ("profile.php", "people", "groups", "events", "watch", "share", "tr", "plugins") else parts[0]
    if plat == "youtube":
        if first.startswith("@"):
            return first[1:]
        return parts[1] if first in ("c", "user") and len(parts) > 1 else None  # /channel/UC... carries no name
    if plat == "tiktok":
        return first[1:] if first.startswith("@") else None
    return None if first in ("p", "explore", "hashtag", "i", "home", "search", "status") else parts[0]


def social_owner_ok(name: str, plat: str, pr, trusted: bool = False) -> bool:
    """A footer link is not proof of ownership: sites link to the parent group, the web agency, the theme vendor.
    The account handle must carry the company's own name, unless the site's own Organization markup vouches for it."""
    h = _handle(plat, pr)
    if not h:
        return False
    hs = slug(h)
    if not hs or hs in PLATFORM_OWN or any(p in hs for p in ("envato", "themeforest", "elementor", "wordpress")):
        return False
    if trusted:
        return True
    toks = _tokens(name)
    ns = slug(name)
    return any(t in hs for t in toks if len(t) >= 4) or (len(hs) >= 5 and hs in ns) or (len(ns) >= 5 and ns in hs)


class SocialBag:
    """Collects ownership-checked candidates per platform; publishes a platform only when exactly one account
    remains. Two competing accounts on one network are recorded as `ambiguous`, never as two facts."""

    def __init__(self) -> None:
        self.items: dict[str, list[dict]] = {}

    def add(self, plat: str, clean: str, handle: str, r, sel: str, trusted: bool) -> None:
        self.items.setdefault(plat, []).append({"url": clean, "handle": slug(handle), "r": r, "sel": sel, "trusted": trusted})

    def __len__(self) -> int:
        return sum(len(v) for v in self.items.values())

    def flush(self, c: Ctx, link) -> None:
        for plat, items in sorted(self.items.items()):
            pool = [i for i in items if i["trusted"]] or items
            by_handle: dict[str, dict] = {}
            for i in pool:
                by_handle.setdefault(i["handle"], i)
            if len(by_handle) == 1:
                i = next(iter(by_handle.values()))
                eid = c.ev(i["r"], "company_owned", SITE_POLICY, i["sel"],
                           f"link to {plat}: {i['url']} (handle carries the company name)", "social_links.v1")
                c.add("social_profiles", f"{plat}_url", i["url"], eid, link, 0.9, key_extra=i["url"])
            else:
                c.absent("social_profiles", f"{plat}_url", "ambiguous",
                         f"{len(by_handle)} different {plat} accounts are linked and none is vouched for by the site's own "
                         "Organization markup; none published",
                         key_extra="conflict", alternatives=[{"url": v["url"]} for v in by_handle.values()][:5])


def _social(c, r, link, url, sel, bag: SocialBag, name: str = "", trusted: bool = False) -> bool:
    pr = urlparse(url)
    plat = SOCIAL.get(pr.netloc.lower().removeprefix("www."))
    if not plat or any(b in url.lower() for b in BAD) or len(pr.path.strip("/")) < 2:
        return False
    if not social_owner_ok(name, plat, pr, trusted):
        return False
    clean = f"{pr.scheme}://{pr.netloc.lower()}{pr.path.rstrip('/')}"
    bag.add(plat, clean, _handle(plat, pr) or "", r, sel, trusted)
    return True


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
PHONE = re.compile(r"(?:\+47\s?)?(?:\d{2}[\s.]?){3}\d{2}\b")
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})")


def mine_site(c: Ctx, f: Fetcher, base: str, home_r, home_pg: Page, link, budget: int = 14, name: str = "") -> None:
    pk = parked_reason(home_pg, base)
    if pk:
        c.absent("public_activity", "news_item", "not_available", f"site not mined: {pk}", key_extra=base)
        return
    bag = SocialBag()
    _ld_org(c, home_r, home_pg, link, bag, name)
    for h, _ in home_pg.links:
        _social(c, home_r, link, urljoin(base, h), "html:a[href]", bag, name)
    host = urlparse(base).netloc
    buckets: dict[str, list[str]] = {k: [] for k in BUCKETS}
    for h, label in home_pg.links:
        u = urljoin(base, h).split("#")[0]
        if urlparse(u).netloc != host:
            continue
        low = (urlparse(u).path + " " + label).lower()
        for b, keys in BUCKETS.items():
            if any(k in low for k in keys) and u not in buckets[b]:
                buckets[b].append(u)
                break
    plan = [(b, u) for b in BUCKETS for u in buckets[b][:2]][:budget]
    for b, u in plan:
        r = f.get(u, ttl=43_200)
        fam = {"contact": "contact", "about": "business_profile", "careers": "hiring",
               "news": "public_activity", "locations": "locations", "team": "leadership"}[b]
        if r.blocked_by_robots:
            c.absent(fam, f"{b}_page", "blocked", f"{u} disallowed by robots.txt", key_extra=u)
            continue
        if not r.ok or not r.is_html:
            c.absent(fam, f"{b}_page", "failed", f"{u} unavailable ({r.error})", key_extra=u)
            continue
        pg = parse(r)
        if parked_reason(pg, u):
            continue
        try:
            {"contact": _contact, "about": lambda *a: _ld_org(*a, bag, name), "careers": _jobs,
             "news": _news, "locations": _locs, "team": _team}[b](c, r, pg, link)
        except Exception as e:  # noqa: BLE001
            c.absent(fam, f"{b}_extract", "failed", f"extraction raised {type(e).__name__}", key_extra=u)
    for fu in home_pg.feeds[:2]:
        fr = f.get(urljoin(base, fu), ttl=21_600)
        if fr.ok:
            _feed(c, fr, link)
    bag.flush(c, link)
    if not len(bag):
        c.absent("social_profiles", "company_owned_social_links", "not_available",
                 "verified site checked; no company-owned social links found")
    have = sum(1 for x in c.claims if x.field == "news_item" and x.availability == "available")
    if have < 3:
        try:
            sitemap_news(c, f, base, link, budget=6)
        except Exception as e:  # noqa: BLE001
            c.absent("public_activity", "sitemap_news", "failed", f"sitemap discovery raised {type(e).__name__}", key_extra=base)


def _ld_org(c, r, pg, link, seen, name: str = "") -> None:
    hit = False
    for o in pg.ld_objects():
        if not _types(o) & {"organization", "corporation", "localbusiness", "store", "ngo"}:
            continue
        declared = str(o.get("legalName") or o.get("name") or "")
        own = bool(_tokens(name) & _tokens(declared))
        if declared.strip() and not own and _tokens(name):
            continue  # an Organization block that names another entity (parent, agency, partner) is not this company's data
        hit = True
        for key, fam, fld in (("description", "business_profile", "site_description"),
                              ("telephone", "contact", "telephone"), ("email", "contact", "email"),
                              ("foundingDate", "legal_identity", "site_founding_date")):
            v = o.get(key)
            if isinstance(v, str) and v.strip():
                eid = c.ev(r, "company_owned", SITE_POLICY, f"jsonld:Organization.{key}", f"{key}: {v}", "jsonld.v1")
                c.add(fam, fld, v.strip()[:500], eid, link, 0.9, key_extra=v.strip()[:40])
        same = o.get("sameAs")
        for u in ([same] if isinstance(same, str) else (same or [])):
            _social(c, r, link, str(u), "jsonld:sameAs", seen, name, trusted=own)
    if not hit:
        c.absent("business_profile", "structured_organisation_data", "not_available",
                 "page fetched; no schema.org Organization block present", key_extra=r.url)


def _contact(c, r, pg, link) -> None:
    host = urlparse(r.final_url or r.url).netloc.lower().removeprefix("www.")
    mails = {h[7:].split("?")[0] for h, _ in pg.links if h.lower().startswith("mailto:")} | set(EMAIL.findall(pg.body)[:5])
    kept = 0
    for m in sorted(mails):
        dom = m.rsplit("@", 1)[-1].lower().removeprefix("www.")
        if dom != host and not host.endswith("." + dom) and not dom.endswith("." + host):
            continue  # third-party addresses are not claims about this company
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:a[href^=mailto]", f"contact email: {m}", "contact.v1")
        c.add("contact", "email", m, eid, link, 0.9, key_extra=m)
        kept += 1
    if not kept:
        c.absent("contact", "email", "not_available", "contact page fetched; no same-domain email published")
    phones = {re.sub(r"[^\d+]", "", h[4:]) for h, _ in pg.links if h.lower().startswith("tel:")}
    phones |= {re.sub(r"[^\d+]", "", p) for p in PHONE.findall(pg.body)[:3]}
    phones = {p for p in phones if len(re.sub(r"\D", "", p)) >= 8}
    for p in sorted(phones)[:3]:
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:a[href^=tel]", f"contact phone: {p}", "contact.v1")
        c.add("contact", "telephone", p, eid, link, 0.85, key_extra=p)
    if not phones:
        c.absent("contact", "telephone", "not_available", "contact page fetched; no telephone number published")


ATS_HOSTS = ("webcruiter.", "easycruit.", "teamtailor.", "recruitee.", "jobylon.", "varbi.", "lever.co", "greenhouse.io",
             "workable.com", "personio.", "jobbnorge.no", "hrmanager.", "jobs.", "smartrecruiters.", "reachmee.", "cvpartner.")


def _jobs(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        if "jobposting" not in _types(o):
            continue
        title = o.get("title") or o.get("name")
        if not isinstance(title, str) or not title.strip():
            continue
        posted = o.get("datePosted")
        posted = posted[:10] if isinstance(posted, str) else None
        loc = o.get("jobLocation")
        loc = loc[0] if isinstance(loc, list) and loc else loc
        place = ((loc or {}).get("address") or {}).get("addressLocality") if isinstance(loc, dict) else None
        eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:JobPosting",
                   f"{title}" + (f" — {place}" if place else "") + (f" (posted {posted})" if posted else ""),
                   "jobposting_ld.v1", pub=posted)
        c.add("hiring", "open_position", {"title": title.strip()[:200], "location": place,
                                           "date_posted": posted, "url": o.get("url") or r.url},
              eid, link, 0.9, key_extra=title.strip().lower()[:80])
        found += 1
    ats = sorted({h.split("#")[0] for h, _ in pg.links
                  if any(k in urlparse(h).netloc.lower() for k in ATS_HOSTS)})[:3]
    for u in ats:
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:a[href]", f"careers page links to recruitment system: {u}", "careers_ats.v1")
        c.add("hiring", "recruitment_system_url", u, eid, link, 0.8, key_extra=u)
    if found:
        eid = c.ev(r, "company_owned", SITE_POLICY, "html:body", f"{found} vacancy entries listed", "jobs_count.v1")
        c.add("hiring", "open_position_count", found, eid, link, 0.85, key_extra=r.url)
    elif ats:
        pass
    else:
        c.absent("hiring", "open_position", "not_available",
                 "careers page fetched; no structured vacancies listed at retrieval time", key_extra=r.url)


def _undated(c, r, link, headline, url, sel, ex) -> None:
    eid = c.ev(r, "company_owned", SITE_POLICY, sel, headline, ex)
    c.claims.append(Claim(
        f"{c.org}|public_activity|news_item|{headline.lower()[:80]}", "public_activity", "news_item",
        "ambiguous", None, 0.0, [eid], link,
        alternatives=[{"headline": headline[:240], "url": url, "date_published": None}],
        note="item found on a company-owned page but exposes no publication date; withheld from dated activity"))


NOT_NEWS = re.compile(r"^\s*(se |les |vis |last |read |see |view |load |show )?(flere |mer |more |all |alle )?(nyheter|news|saker|artikler|"
                      r"blogg|blog|innlegg|posts|articles|aktuelt|presse|press)\s*(>|»|→)?\s*$|^\s*(les mer|read more|se mer|see more|"
                      r"vis mer|show more|mer|more|kontakt|contact|home|hjem|forside|meny|menu)\s*(>|»|→)?\s*$", re.I)
NOT_NEWS_PATH = ("produkt", "product", "shop", "butikk", "kategori", "category", "tjeneste", "service", "tag", "tags",
                 "cart", "handlekurv", "checkout", "kasse", "login", "logg-inn", "comment", "kommentar")
NEWSY = ("nyhet", "news", "blogg", "blog", "aktuelt", "artikkel", "article", "presse", "press", "innlegg", "stories", "historier")
NEWS_TYPES = {"newsarticle", "blogposting", "article", "report"}


PLACEHOLDER_HEAD = re.compile(
    r"^\s*(hello,? world|hei,? verden|hallo,? verden|hej,? v[aä]rlden|sample (page|post)|eksempel(side|innlegg)|test( post| innlegg)?|"
    r"lorem ipsum.*|just another wordpress.*|(nok )?et wordpress-nettsted|welcome to (your|our) (new )?(site|website|blog)|"
    r"velkommen til (ditt|din|v[aå]r) (nye )?(nettsted|nettside|blogg)|coming soon|kommer snart|under construction|"
    r"under konstruksjon|untitled|uten tittel|auto draft|ny side|new post|nytt innlegg)\s*[!.]*\s*$", re.I)
PLACEHOLDER_PATH = re.compile(r"/(hello-world|hei-verden|hallo-verden|sample-page|eksempelside|auto-draft|__trashed)(/|$)", re.I)

PARKED_PHRASES = (
    "this domain is for sale", "domain is for sale", "domain for sale", "this domain may be for sale", "domain may be for sale",
    "buy this domain", "the domain name is for sale", "this domain is parked", "domain is parked", "domain parking",
    "parked free", "parked domain", "inquire about this domain", "make an offer on this domain", "domain name is available",
    "domenet er til salgs", "dette domenet er til salgs", "domenet er til salg", "kj\u00f8p dette domenet", "kjop dette domenet",
    "domenet er ledig", "domenet er parkert", "domene til salgs", "domener til salgs", "denne siden er parkert",
    "this web page is parked", "this site is not published", "nettsiden er ikke publisert", "web hosting by", "default web site page",
    "apache2 ubuntu default page", "welcome to nginx", "index of /", "it works!", "website coming soon", "siden er under utvikling",
    "domain portfolio", "our domain portfolio", "domeneportef\u00f8lje", "domeneportefolje")
PARKING_HOSTS = ("sedo.com", "dan.com", "afternic.com", "hugedomains.com", "godaddy.com/domainsearch", "parkingcrew.net",
                 "bodis.com", "above.com", "undeveloped.com", "squadhelp.com", "brandbucket.com", "domainmarket.com", "register.no/domene",
                 "domeneshop.no/domain")


def parked_reason(pg, final_url: str = "") -> str | None:
    """A domain-for-sale / parked / default-server page is not a company website; nothing on it is about the company."""
    text = f"{pg.title} {pg.body}".lower()
    hit = next((p for p in PARKED_PHRASES if p in text), None)
    short = len(pg.body.strip()) < 2500
    if hit and (short or hit in ("domain for sale", "domain is for sale", "this domain is for sale", "domenet er til salgs",
                                  "dette domenet er til salgs", "buy this domain", "domain portfolio", "our domain portfolio")):
        return f"page looks like a parked or for-sale domain ({hit!r})"
    for h, _ in pg.links:
        low = (h or "").lower()
        if any(k in low for k in PARKING_HOSTS) and short:
            return "page links to a domain marketplace and has almost no content of its own"
    return None


def _newsy_url(url: str) -> bool:
    """A post-shaped address: a news/blog path segment or a /YYYY/MM/ date path."""
    path = urlparse(url or "").path.lower()
    return any(k in path for k in NEWSY) or bool(re.search(r"/(19|20)\d\d/(0?[1-9]|1[0-2])(/|$)", path))


def _news_ok(head: str, item_url: str, listing_url: str, site_host: str, multi: bool) -> bool:
    """A news item is a real post, not a menu label, a product page or the listing itself."""
    if len(head.strip()) < 8 or NOT_NEWS.match(head) or PLACEHOLDER_HEAD.match(head):
        return False  # menu labels and CMS starter content ("Hello world!", "Hei, verden!") are not company news
    if PLACEHOLDER_PATH.search(urlparse(item_url or "").path):
        return False
    pr = urlparse(item_url or "")
    if item_url:
        if pr.netloc and pr.netloc.lower().removeprefix("www.") != site_host.lower().removeprefix("www."):
            return False
        segs = {x.lower() for x in pr.path.split("/") if x}
        if any(any(seg.startswith(k) for k in NOT_NEWS_PATH) for seg in segs):
            return False
        if multi and item_url.rstrip("/") == listing_url.rstrip("/"):
            return False  # every item pointing at the listing page = not individually addressable
    return True


def _plausible_date(d: str | None) -> bool:
    from datetime import date, timedelta
    try:
        y = date.fromisoformat(d[:10]) if d else None
    except ValueError:
        return False
    return bool(y) and date(1995, 1, 1) <= y <= date.today() + timedelta(days=1)


def _news(c, r, pg, link) -> None:
    found = 0
    arts = [o for o in pg.ld_objects() if _types(o) & NEWS_TYPES]
    multi = len(arts) > 1
    host = urlparse(r.final_url or r.url).netloc
    for o in arts:
        head = o.get("headline") or o.get("name")
        if not isinstance(head, str):
            continue
        item_url = o.get("url") if isinstance(o.get("url"), str) else ""
        if not _news_ok(head, item_url, r.final_url or r.url, host, multi):
            continue
        # Only a publication date counts. dateModified / "updated" say nothing about when the item was published.
        raw = o.get("datePublished")
        pub = raw[:10] if isinstance(raw, str) and ISO.match(raw) and _plausible_date(raw) else None
        if pub is None:
            _undated(c, r, link, head.strip(), item_url or r.url, "jsonld:Article", "news_ld.v1")
            continue
        eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Article.datePublished", f"{head.strip()} ({pub})", "news_ld.v1", pub=pub)
        c.add("public_activity", "news_item", {"headline": head.strip()[:240], "date_published": pub,
                                                "url": item_url or r.url},
              eid, link, 0.88, key_extra=head.strip().lower()[:80])
        found += 1
        if found >= 20:
            break
    if not found:
        c.absent("public_activity", "news_item", "not_available",
                 "news page fetched; no individually addressable posts with a publication date found", key_extra=r.url)


def _feed(c, r, link) -> None:
    from email.utils import parsedate_to_datetime
    feed_url = r.final_url or r.url
    fpath = urlparse(feed_url).path.lower()
    if "comment" in fpath:
        return  # a comments feed lists reader comments, not company announcements
    scoped = any(k in fpath for k in NEWSY)   # a news/blog-specific feed vouches for its items; a site-wide /feed/ does not
    unverified: list[dict] = []
    for raw in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", r.text, re.S | re.I)[:20]:
        tm = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        # pubDate / published / dc:date only. Atom <updated> is a modification time, never a publication date.
        dm = re.search(r"<(?:pubDate|published|dc:date)[^>]*>(.*?)</", raw, re.S | re.I)
        lm = re.search(r"<link[^>]*>([^<]+)</link>|<link[^>]*href=[\"']([^\"']+)", raw, re.S | re.I)
        if not tm:
            continue
        title = " ".join(re.sub(r"<[^>]+>", "", tm.group(1).replace("<![CDATA[", "").replace("]]>", "")).split())
        url = ((lm.group(1) or lm.group(2)) if lm else "").strip()
        if not url or url.rstrip("/") == feed_url.rstrip("/"):
            continue  # not individually addressable
        if not _news_ok(title, url, feed_url, urlparse(feed_url).netloc, True):
            continue
        cats = " ".join(re.findall(r"<category[^>]*>(.*?)</category>", raw, re.S | re.I)).lower()
        if not (scoped or _newsy_url(url) or any(k in cats for k in NEWSY)):
            unverified.append({"headline": title[:160], "url": url})
            continue
        pub = None
        if dm:
            m = ISO.search(dm.group(1))
            try:
                pub = m.group(1) if m else parsedate_to_datetime(dm.group(1).strip()).date().isoformat()
            except Exception:
                pub = None
        if pub is None or not _plausible_date(pub):
            _undated(c, r, link, title, url, "feed:item", "feed.v1")
            continue
        eid = c.ev(r, "company_owned", SITE_POLICY, "feed:item/pubDate", f"{title} ({pub})", "feed.v1", pub=pub)
        c.add("public_activity", "news_item", {"headline": title[:240], "date_published": pub, "url": url},
              eid, link, 0.86, key_extra=title.lower()[:80])
    if unverified:
        c.absent("public_activity", "feed_items", "ambiguous",
                 f"{len(unverified)} feed item(s) are page-type content (no news/blog path, date path or news category) and "
                 "cannot be verified as news posts; none published as news", key_extra=feed_url, alternatives=unverified[:5])


def _article_date(pg: Page) -> tuple[str | None, str | None]:
    """(date, selector) of an article's PUBLICATION date only: JSON-LD datePublished or article:published_time."""
    for o in pg.ld_objects():
        if _types(o) & NEWS_TYPES and isinstance(o.get("datePublished"), str) and _plausible_date(o["datePublished"]):
            return o["datePublished"][:10], "jsonld:Article.datePublished"
    for m in pg.pubmeta:
        if ISO.match(m) and _plausible_date(m):
            return m[:10], "meta[article:published_time]"
    return None, None


def _locs_in(xml: str) -> list[str]:
    return [u.strip() for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I)]


def sitemap_news(c: Ctx, f: Fetcher, base: str, link, budget: int = 6) -> int:
    """Discover post-shaped URLs from permitted sitemaps (index files followed up to depth 2, 4 files), then read the
    article itself. <lastmod> only orders the work; it is never a publication date."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    maps: list[str] = []
    rb = f.get(root + "/robots.txt", ttl=43_200)
    if rb.ok:
        maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text)
    maps += [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    queue, seen, urls, fetched = [(m, 0) for m in dict.fromkeys(maps)], set(), [], 0
    while queue and fetched < 4:
        m, depth = queue.pop(0)
        if m in seen or urlparse(m).netloc.lower().removeprefix("www.") != host:
            continue
        seen.add(m)
        sr = f.get(m, ttl=43_200)
        fetched += 1
        if not sr.ok:
            continue
        locs = _locs_in(sr.text)
        if "<sitemapindex" in sr.text.lower():
            if depth < 2:
                queue += [(u, depth + 1) for u in locs[:10]]
            continue
        entries = re.findall(r"<url>(.*?)</url>", sr.text, re.S | re.I)
        for e in entries[:3000]:
            u = _locs_in(e)
            lm = re.search(r"<lastmod>\s*([^<\s]+)", e, re.I)
            if u and urlparse(u[0]).netloc.lower().removeprefix("www.") == host and _newsy_url(u[0]) \
                    and not any(seg.startswith(k) for seg in urlparse(u[0]).path.lower().split("/") for k in NOT_NEWS_PATH):
                urls.append((lm.group(1)[:10] if lm else "", u[0]))
    urls = sorted(set(urls), reverse=True)
    known = {x.value.get("url") for x in c.claims if x.field == "news_item" and isinstance(x.value, dict)}
    got = 0
    for _, u in urls:
        if got >= budget or u.rstrip("/") in {k.rstrip("/") for k in known if k}:
            continue
        if u.rstrip("/") == root or u.rstrip("/").endswith(tuple("/" + k for k in NEWSY)):
            continue  # section index pages, not posts
        ar = f.get(u, ttl=43_200)
        if not (ar.ok and ar.is_html):
            continue
        pg = parse(ar)
        pub, sel = _article_date(pg)
        head = next((o.get("headline") or o.get("name") for o in pg.ld_objects() if _types(o) & NEWS_TYPES and
                     isinstance(o.get("headline") or o.get("name"), str)), None) or (pg.h1[0].strip() if pg.h1 else "")
        if not pub or not _news_ok(head or "", ar.final_url or u, u, host, False):
            continue
        eid = c.ev(ar, "company_owned", SITE_POLICY, sel, f"{head.strip()} ({pub})", "sitemap_article.v1", pub=pub)
        c.add("public_activity", "news_item", {"headline": head.strip()[:240], "date_published": pub, "url": ar.final_url or u},
              eid, link, 0.86, key_extra=head.strip().lower()[:80])
        got += 1
    if not got:
        c.absent("public_activity", "sitemap_news", "not_available",
                 "sitemap checked; no post with a publication date was found", key_extra=base)
    return got


def _locs(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        if not _types(o) & {"localbusiness", "store", "place"}:
            continue
        a = o.get("address")
        if not isinstance(a, dict):
            continue
        pretty = ", ".join(str(a[k]) for k in ("streetAddress", "postalCode", "addressLocality") if a.get(k))
        if pretty:
            eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Place.address", pretty, "locations_ld.v1")
            c.add("locations", "published_location", {"name": o.get("name"), "address": pretty},
                  eid, link, 0.85, key_extra=pretty.lower()[:80])
            found += 1
    if not found:
        c.absent("locations", "published_location", "not_available",
                 "locations page fetched; no structured addresses present", key_extra=r.url)


def _team(c, r, pg, link) -> None:
    found = 0
    for o in pg.ld_objects():
        n = o.get("name")
        if "person" in _types(o) and isinstance(n, str) and len(n.split()) >= 2:
            role = o.get("jobTitle") if isinstance(o.get("jobTitle"), str) else None
            eid = c.ev(r, "company_owned", SITE_POLICY, "jsonld:Person", f"{n}" + (f" — {role}" if role else ""), "team_ld.v1")
            c.add("leadership", "site_published_person", {"name": n.strip()[:120], "title": role},
                  eid, link, 0.82, key_extra=n.strip().lower())
            found += 1
    if not found:
        c.absent("leadership", "site_published_person", "not_available",
                 "team page fetched; no structured person entries present", key_extra=r.url)
