"""Fully offline: network replaced by a fixture router. Run: python -m unittest -v"""
import hashlib
import json
import sys
import urllib.error
from pathlib import Path as _P

import os
# SIGNALPOST_SRC lets the same tests run against another source tree (used to prove the regression tests fail on v1.2.1).
sys.path.insert(0, os.environ.get("SIGNALPOST_SRC") or str(_P(__file__).resolve().parents[1] / "src"))
import tempfile
import unittest
from pathlib import Path

from signalpost import core
from signalpost.core import Claim, EntityLink
from signalpost.fetch import FetchResult, Fetcher
from signalpost.pipeline import Agent, State, diff, run_batch, validate
from signalpost.render import render

ORG = "923609016"

# Deterministic DNS: no test depends on the machine's resolver, network or ISP NXDOMAIN hijacking.
FAKE_DNS = {"private.example.no": "10.0.0.5", "metadata.example.no": "169.254.169.254", "cgnat.example.no": "100.64.0.9"}
_REAL_GETADDRINFO = None


def _fake_getaddrinfo(host, port, *a, **k):
    import socket as _s
    ip = FAKE_DNS.get(str(host).lower(), "93.184.216.34")
    return [(_s.AF_INET, _s.SOCK_STREAM, 6, "", (ip, port or 0))]


def setUpModule():
    global _REAL_GETADDRINFO
    import socket as _s
    _REAL_GETADDRINFO = _s.getaddrinfo
    _s.getaddrinfo = _fake_getaddrinfo


def tearDownModule():
    import socket as _s
    _s.getaddrinfo = _REAL_GETADDRINFO
ER = "https://data.brreg.no/enhetsregisteret/api"
RR = "https://data.brreg.no/regnskapsregisteret/regnskap"


def res(url, payload=None, text=None, status=200, ctype="application/json", error=None):
    body = (json.dumps(payload) if payload is not None else (text or "")).encode()
    ok = status == 200 and not error
    return FetchResult(url=url, final_url=url, status=status, ok=ok, body=body, text=body.decode(),
                       content_type=ctype, content_sha256=hashlib.sha256(body).hexdigest(), error=error)


ENTITY = {"organisasjonsnummer": ORG, "navn": "FIXTURE INDUSTRIER AS",
          "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
          "registreringsdatoEnhetsregisteret": "1995-09-18", "antallAnsatte": 412,
          "naeringskode1": {"kode": "25.620", "beskrivelse": "Bearbeiding av metaller"},
          "konkurs": False, "underAvvikling": False, "hjemmeside": "www.fixtureindustrier.no",
          "forretningsadresse": {"adresse": ["Industriveien 4"], "postnummer": "4001", "poststed": "STAVANGER"}}
ROLES = {"rollegrupper": [{"roller": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "fratraadt": False,
                                       "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}}]}]}
ACCOUNTS = [{"regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "valuta": "NOK",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 884000000},
                                                              "driftsresultat": 83000000}, "aarsresultat": 61500000},
             "eiendeler": {"sumEiendeler": 1204000000}}]
HOME = (f'<html><head><title>Fixture Industrier AS</title><script type="application/ld+json">'
        '{"@type":"Organization","sameAs":["https://www.linkedin.com/company/fixture-industrier"]}</script></head>'
        f'<body><p>Fixture Industrier AS, org.nr. 923 609 016</p><a href="/kontakt">Kontakt</a>'
        '<a href="/karriere">Karriere</a><a href="/nyheter">Nyheter</a></body></html>')
CAREERS = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"Sveiser",'
           '"datePosted":"2026-09-30","jobLocation":{"address":{"addressLocality":"Stavanger"}}}</script></html>')
NEWS = ('<html><script type="application/ld+json">[{"@type":"NewsArticle","headline":"Ny produksjonslinje i drift",'
        '"datePublished":"2026-08-14"},{"@type":"NewsArticle","headline":"Artikkel uten dato her"}]</script></html>')
CONTACT = '<html><a href="mailto:post@fixtureindustrier.no">p</a><a href="mailto:x@gmail.com">x</a><a href="tel:+4751000000">t</a></html>'

ROUTES = {f"{ER}/enheter/{ORG}/roller": res("", ROLES), f"{ER}/enheter/{ORG}": res("", ENTITY),
          f"{RR}/{ORG}": res("", ACCOUNTS),
          "https://www.fixtureindustrier.no/kontakt": res("", text=CONTACT, ctype="text/html"),
          "https://www.fixtureindustrier.no/karriere": res("", text=CAREERS, ctype="text/html"),
          "https://www.fixtureindustrier.no/nyheter": res("", text=NEWS, ctype="text/html"),
          "https://www.fixtureindustrier.no/": res("", text=HOME, ctype="text/html")}


class FakeFetcher(Fetcher):
    def __init__(self, routes=None, default=None):
        super().__init__(None)
        self.routes, self.default = ROUTES if routes is None else routes, default

    def get(self, url, *, ttl=0, accept=None):
        self.requests += 1
        for k, v in sorted(self.routes.items(), key=lambda kv: -len(kv[0])):
            if url == k or url.startswith(k):
                return FetchResult(**{**v.__dict__, "url": url, "final_url": url})
        if self.default:
            return self.default(url)
        if "wikidata" in url:
            return res(url, {"results": {"bindings": []}})
        if "gleif" in url:
            return res(url, {"data": []})
        return res(url, text="", status=404, error="http_404")


def agent(tmp, fetcher=None):
    return Agent(fetcher or FakeFetcher(), State(Path(tmp) / "state"))


class Tests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def test_orgnum(self):
        self.assertTrue(core.is_valid(ORG))
        self.assertFalse(core.is_valid("923609017"))
        self.assertFalse(core.is_valid("12345"))
        self.assertEqual(core.find_orgnums("org.nr. 923 609 016"), {ORG})

    def test_claim_contract(self):
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "available", 1)  # no evidence
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "not_available", 0, note="n")  # absence as zero
        with self.assertRaises(ValueError):
            Claim("k", "f", "x", "not_available")  # no explanation

    def test_happy_path(self):
        env = agent(self.tmp).run_one(f" {ORG} ")
        self.assertEqual(validate(env), [])
        keys = {f"{c['field_family']}|{c['field']}" for c in env["claims"] if c["availability"] == "available"}
        for k in ("legal_identity|legal_name", "financials|latest_revenue", "digital_presence|official_website",
                  "hiring|open_position", "public_activity|news_item", "social_profiles|linkedin_url", "contact|email"):
            self.assertIn(k, keys)
        self.assertNotIn("gmail", json.dumps(env["claims"]))  # third-party address never published
        self.assertIn("884 000 000", " ".join(env["synthesis"]["summary"]))

    def test_undated_news_is_ambiguous_not_published(self):
        env = agent(self.tmp).run_one(ORG)
        und = [c for c in env["claims"] if c["field"] == "news_item" and c["availability"] == "ambiguous"]
        self.assertEqual(len(und), 1)
        self.assertIsNone(und[0]["alternatives"][0]["date_published"])

    def test_wrong_company_site_rejected(self):
        routes = dict(ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html>Helt annet AS org.nr. 999 999 999</html>", ctype="text/html")
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        site = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertEqual(site["availability"], "ambiguous")
        self.assertEqual(validate(env), [])

    def test_weak_match_not_published(self):
        routes = dict(ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html><title>x</title>hello world</html>", ctype="text/html")
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        site = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertNotEqual(site["availability"], "available")

    def test_invalid_input_one_envelope(self):
        env = agent(self.tmp).run_one("not-a-number")
        self.assertFalse(env["input_valid"])
        self.assertEqual(env["run"]["terminal_status"], "failed")
        self.assertEqual(validate(env), [])

    def test_network_down_is_typed(self):
        env = agent(self.tmp, FakeFetcher({}, default=lambda u: res(u, text="", status=None, error="timeout"))).run_one(ORG)
        self.assertEqual(env["run"]["terminal_status"], "failed")
        self.assertTrue(all(c["availability"] != "available" for c in env["claims"]))
        self.assertEqual(validate(env), [])

    def test_missing_registry_company(self):
        env = agent(self.tmp, FakeFetcher({})).run_one(ORG)
        self.assertEqual(env["claims"][0]["availability"], "not_available")

    def test_batch_returns_one_per_input(self):
        batch = [ORG, "", "abc", "000000000", "12 34", ORG]
        envs = run_batch(batch, agent(self.tmp), workers=3)
        self.assertEqual(len(envs), len(batch))
        self.assertTrue(all(validate(e) == [] for e in envs))

    def test_batch_timeout_envelope(self):
        import time

        class Slow(FakeFetcher):
            def get(self, url, **kw):
                time.sleep(2)
                return super().get(url, **kw)
        envs = run_batch([ORG], agent(self.tmp, Slow()), workers=1, company_timeout=0.2)
        self.assertEqual(len(envs), 1)
        self.assertTrue(envs[0]["input_valid"])  # valid number must not be marked invalid
        self.assertEqual(envs[0]["run"]["terminal_status"], "failed")

    def test_rerun_idempotent_and_change_detection(self):
        a = agent(self.tmp)
        first, second = a.run_one(ORG), a.run_one(ORG)
        self.assertTrue(first["refresh"]["is_first_observation"])
        self.assertFalse(second["refresh"]["is_first_observation"])
        self.assertEqual([c for c in second["changes"] if c["change_type"] != "added"], [])
        link = EntityLink(ORG, "m", 1.0, ["x"])
        prev = {"claims": {"k": {"family": "legal_identity", "field": "legal_name", "availability": "available",
                                 "hash": "a", "value": "OLD", "first": "2026-01-01T00:00:00Z"}}, "runs": 1}
        ch, _ = diff(prev, [Claim("k", "legal_identity", "legal_name", "available", "NEW", 1, ["e"], link)])
        self.assertEqual((ch[0]["change_type"], ch[0]["materiality"]), ("changed", "material"))

    def test_render(self):
        env = agent(self.tmp).run_one(ORG)
        self.assertEqual(render([env], Path(self.tmp) / "site"), 1)
        page = (Path(self.tmp) / "site" / f"{ORG}.html").read_text("utf-8")
        self.assertIn("data.brreg.no", page)
        self.assertIn("Kari Nordmann", page)


class GateTests(unittest.TestCase):
    NAME = "BLUE BAY AS"

    def gate(self, routes, cands, official=None):
        from signalpost.core import Ctx
        from signalpost.sources import verify_site
        c = Ctx(ORG)
        res = verify_site(c, FakeFetcher(routes), self.NAME, cands, official=official or set(),
                          places=["SANDVIKA", "BAERUM"], postal="1337")
        site = [x for x in c.claims if x.field == "official_website"]
        return res, site

    def page(self, html):
        return res("", text=f"<html><head><title>Blue Bay</title></head><body>{html}</body></html>", ctype="text/html")

    def test_lookalike_without_location_not_published(self):
        # right name + exact domain, but an asset manager in London: must stay ambiguous
        r, site = self.gate({"https://www.bluebay.no/": self.page("Blue Bay institutional investors London")},
                            ["https://www.bluebay.no/"])
        self.assertIsNone(r)
        self.assertEqual(site[0].availability, "ambiguous")

    def test_guess_with_postal_code_on_contact_page_published(self):
        routes = {"https://www.bluebay.no/": self.page('Blue Bay AS <a href="/kontakt">Kontakt</a>'),
                  "https://www.bluebay.no/kontakt": self.page("Blue Bay, Postveien 1, 1337 Sandvika")}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "exact_name_domain_and_registered_location")

    def test_substring_domain_never_matches(self):
        routes = {"https://www.rbcbluebay.com/": self.page("Blue Bay 1337 Sandvika")}
        r, site = self.gate(routes, ["https://www.rbcbluebay.com/"])
        self.assertIsNone(r)  # host token != legal-name token and not from an official source

    def test_official_source_candidate_with_name_published(self):
        routes = {"https://carlin.no/": self.page("Blue Bay")}
        r, site = self.gate(routes, ["https://carlin.no/"], official={"carlin.no"})
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "official_source_candidate_name_match")

    def test_legal_form_phrase_published(self):
        r, site = self.gate({"https://www.bluebay.no/": self.page("Velkommen. © 2026 Blue Bay AS. Alle rettigheter")},
                            ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertIn("legal_name_with_legal_form_on_page", site[0].entity_link.matched_on)

    def test_orgnr_on_privacy_page_published_with_hop_evidence(self):
        from signalpost.core import Ctx
        from signalpost.sources import verify_site
        routes = {"https://www.bluebay.no/": self.page('Blue Bay <a href="/personvern">Personvern</a>'),
                  "https://www.bluebay.no/personvern": self.page("Blue Bay, org.nr 923 609 016")}
        c = Ctx(ORG)
        out = verify_site(c, FakeFetcher(routes), self.NAME, ["https://www.bluebay.no/"], places=[], postal=None)
        self.assertIsNotNone(out)
        self.assertEqual(out[0], "https://www.bluebay.no/")          # claim value is the homepage
        self.assertTrue(out[1].url.endswith("bluebay.no/"))            # mining continues on the homepage
        self.assertTrue(any(e.source_url.endswith("/personvern") for e in c.evidence))

    def test_norid_rdap_registrant_orgnr_published(self):
        routes = {"https://www.bluebay.no/": self.page("Blue Bay"),
                  "https://rdap.norid.no/domain/bluebay.no": res("", {"entities": [{"roles": ["registrant"],
                                                                                    "handle": "NO923609016"}]})}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNotNone(r)
        self.assertEqual(site[0].entity_link.method, "norid_rdap_registrant_orgnr")

    def test_rdap_for_other_org_ignored(self):
        routes = {"https://www.bluebay.no/": self.page("Blue Bay"),
                  "https://rdap.norid.no/domain/bluebay.no": res("", {"handle": "NO974760673"})}
        r, site = self.gate(routes, ["https://www.bluebay.no/"])
        self.assertIsNone(r)

    def test_unsafe_urls_blocked(self):
        from signalpost.fetch import Fetcher, unsafe_reason
        for u in ("http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/x", "http://169.254.169.254/latest",
                  "http://intranet/", "http://printer.local/", "ftp://example.no/", "https://example.no:8443/",
                  "file:///etc/passwd", "http://[::1]/"):
            self.assertIsNotNone(unsafe_reason(u), u)
        r = Fetcher(None, delay=0, obey_robots=False).get("http://127.0.0.1:80/")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith("unsafe_url"))

    def test_dead_domain_not_retried(self):
        import socket
        from signalpost.fetch import Fetcher
        calls = []
        orig = Fetcher._open

        def boom(self, req, timeout):
            calls.append(1)
            raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        Fetcher._open = boom
        try:
            r = Fetcher(None, delay=0, obey_robots=False).get("https://no-such-host-xyz.no/")
        finally:
            Fetcher._open = orig
        self.assertEqual(len(calls), 1)
        self.assertFalse(r.ok)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def test_soft_deadline_registry_only(self):
        import time
        ff = FakeFetcher()
        a = Agent(ff, State(Path(self.tmp) / "s"), soft_deadline=time.monotonic() - 1)
        env = a.run_one(ORG)
        self.assertEqual(validate(env), [])
        fams = {c["field_family"] for c in env["claims"] if c["availability"] == "available"}
        self.assertIn("financials", fams)
        self.assertNotIn("hiring", fams)
        self.assertFalse(any("fixtureindustrier" in u for u in [e["source_url"] for e in env["evidence"]]))
        self.assertTrue(any(x["kind"] == "SoftDeadline" for x in env["errors"]))

    def test_hard_deadline_still_one_envelope_each(self):
        import time

        class Slow(FakeFetcher):
            def get(self, url, **kw):
                time.sleep(1.5)
                return super().get(url, **kw)
        ag = Agent(Slow(), State(Path(self.tmp) / "s"))
        envs = run_batch([ORG, "923609016", "abc"], ag, workers=2, company_timeout=60,
                         hard_deadline=time.monotonic() + 0.3)
        self.assertEqual(len(envs), 3)
        self.assertTrue(all(validate(e) == [] for e in envs))


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    # ---- 1. robots + network destination ----
    def _robots(self, outcome):
        """Fetcher whose robots.txt request yields `outcome` (HTTPError code | Exception | text); page fetch succeeds."""
        calls = []
        orig = Fetcher._open

        class Resp:
            def __init__(s, body): s.body, s.status, s.headers = body, 200, s
            def read(s, n=-1):
                out, s.body = s.body[:n] if n >= 0 else s.body, s.body[n:] if n >= 0 else b""
                return out
            def geturl(s): return "x"
            def get(s, *a): return "text/html"
            def get_content_charset(s): return "utf-8"
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def fake(self, req, timeout=None):
            url = req.full_url
            calls.append(url)
            if url.endswith("/robots.txt"):
                if isinstance(outcome, int):
                    raise urllib.error.HTTPError(url, outcome, "x", {}, __import__("io").BytesIO(b""))
                if isinstance(outcome, Exception):
                    raise outcome
                return Resp(outcome.encode())
            return Resp(b"<html>ok</html>")
        Fetcher._open = fake
        return calls, orig

    def _get(self, outcome, url="https://example.no/page"):
        calls, orig = self._robots(outcome)
        try:
            f = Fetcher(None, delay=0)
            r = f.get(url)
            r2 = f.get(url)
        finally:
            Fetcher._open = orig
        return f, r, r2, calls

    def test_robots_fail_closed_on_5xx_timeout_and_403(self):
        for outcome in (503, 429, TimeoutError("t"), urllib.error.URLError("reset"), 403, 401):
            f, r, _, calls = self._get(outcome)
            self.assertFalse(r.ok, outcome)
            self.assertTrue(r.blocked_by_robots, outcome)
            self.assertFalse([c for c in calls if not c.endswith("robots.txt")], outcome)  # page never requested

    def test_robots_missing_or_open_allows(self):
        for outcome in (404, 410, "User-agent: *\nDisallow:\n"):
            f, r, _, _ = self._get(outcome)
            self.assertTrue(r.ok, outcome)

    def test_robots_disallow_rules_respected(self):
        f, r, _, _ = self._get("User-agent: *\nDisallow: /page\n")
        self.assertEqual(r.error, "robots_disallow")

    def test_robots_unreachable_is_retried_after_ttl_not_every_call(self):
        import time as _t
        f, r, r2, calls = self._get(503)
        self.assertEqual(sum(c.endswith("robots.txt") for c in calls), 1)  # negative-cached
        h = next(iter(f._robots))
        kind, rp, _ = f._robots[h]
        f._robots[h] = (kind, rp, _t.monotonic() - 1)  # TTL elapsed
        calls2, orig = self._robots("User-agent: *\nDisallow:\n")
        try:
            self.assertTrue(f.get("https://example.no/page").ok)
        finally:
            Fetcher._open = orig

    def test_forbidden_addresses(self):
        from signalpost.fetch import _forbidden_ip, _safe_create_connection
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.169.254", "100.64.0.1", "::1", "fe80::1%eth0",
                   "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1", "fd00::1"):
            self.assertTrue(_forbidden_ip(ip), ip)
        self.assertFalse(_forbidden_ip("93.184.216.34"))
        with self.assertRaises(OSError) as cm:
            _safe_create_connection(("private.example.no", 443), 1)  # DNS answers private at connect time
        self.assertIn("unsafe_address", str(cm.exception))

    def test_open_api_hosts_not_switched_off_by_robots_failure(self):
        for outcome in (503, 403, TimeoutError("t"), urllib.error.URLError("reset")):
            f, r, _, calls = self._get(outcome, url="https://data.brreg.no/enhetsregisteret/api/enheter/1")
            self.assertTrue(r.ok, outcome)
            self.assertFalse([c for c in calls if c.endswith("robots.txt")], outcome)

    def test_company_site_still_fails_closed_and_error_names_reason(self):
        f, r, _, _ = self._get(503)
        self.assertTrue(r.error.startswith("robots_unreachable:HTTP 503"), r.error)
        f, r, _, _ = self._get(TimeoutError("slow"))
        self.assertIn("TimeoutError", r.error)

    def test_hostnames_resolving_to_private_space_blocked(self):
        from signalpost.fetch import unsafe_reason
        for h in ("private.example.no", "metadata.example.no", "cgnat.example.no"):
            self.assertEqual(unsafe_reason(f"https://{h}/"), "private_address", h)
        self.assertIsNone(unsafe_reason("https://www.example.no/"))

    def test_no_process_global_opener_installed(self):
        import urllib.request
        before = urllib.request._opener
        f1, f2 = Fetcher(None), Fetcher(None)
        self.assertIs(urllib.request._opener, before)   # constructing a Fetcher must not mutate urllib globals
        self.assertIsNot(f1._opener, f2._opener)

    def _body_fetch(self, body, headers=None, limit=None):
        import signalpost.fetch as F

        class R:
            status = 200
            def __init__(s):
                s.headers = s; s.b = body
            def get(s, k, d=None): return (headers or {}).get(k.lower(), "text/html" if k.lower() == "content-type" else d)
            def get_content_charset(s): return "utf-8"
            def geturl(s): return "https://example.no/x"
            def read(s, n=-1):
                o, s.b = s.b[:n], s.b[n:]
                return o
            def __enter__(s): return s
            def __exit__(s, *a): return False
        orig, olim = Fetcher._open, F.MAX_BYTES
        Fetcher._open = lambda self, req, timeout: R()
        if limit:
            F.MAX_BYTES = limit
        try:
            return Fetcher(None, delay=0, obey_robots=False).get("https://example.no/x")
        finally:
            Fetcher._open, F.MAX_BYTES = orig, olim

    def test_oversized_body_is_rejected_not_truncated(self):
        r = self._body_fetch(b"x" * 5000, limit=1000)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "response_too_large")
        self.assertEqual(r.body, b"")

    def test_declared_oversize_rejected_without_reading(self):
        r = self._body_fetch(b"x" * 10, headers={"content-length": "999999999"}, limit=1000)
        self.assertEqual(r.error, "response_too_large")

    def test_body_exactly_at_limit_accepted_and_hash_covers_all_of_it(self):
        import hashlib as h
        r = self._body_fetch(b"y" * 1000, limit=1000)
        self.assertTrue(r.ok)
        self.assertEqual(r.content_sha256, h.sha256(b"y" * 1000).hexdigest())

    def test_slow_drip_body_hits_read_deadline(self):
        import signalpost.fetch as F

        class Slow:
            headers = {}
            def read(s, n): return b"z"
        with self.assertRaises(OSError):
            F.read_limited(Slow(), 10**9, deadline_s=0.05)

    def test_credentials_in_url_rejected(self):
        from signalpost.fetch import unsafe_reason
        self.assertEqual(unsafe_reason("https://user:pw@example.no/"), "credentials_in_url")

    # ---- 2. validator + malformed files ----
    def _env(self):
        return Agent(FakeFetcher(), State(self.tmp / "s")).run_one(ORG)

    def test_validator_accepts_good_and_rejects_each_defect(self):
        import copy
        from signalpost.contract import validate as V
        good = self._env()
        self.assertEqual(V(good), [])

        def broke(mut, needle):
            e = copy.deepcopy(good)
            mut(e)
            issues = V(e)
            self.assertTrue(any(needle in i for i in issues), (needle, issues[:5]))

        broke(lambda e: e.pop("evidence"), "missing top-level key 'evidence'")
        broke(lambda e: e["run"].__setitem__("terminal_status", "weird"), "terminal_status")
        broke(lambda e: e["run"].__setitem__("started_at", "yesterday"), "started_at")
        broke(lambda e: e["operations"].__setitem__("requests", "many"), "operations.requests")
        broke(lambda e: e["evidence"][0].__setitem__("source_url", "http://127.0.0.1/x"), "source_url")
        broke(lambda e: e["evidence"][0].__setitem__("content_sha256", "abc"), "content_sha256")
        broke(lambda e: e["evidence"][0].__setitem__("retrieved_at", ""), "retrieved_at")
        broke(lambda e: e["evidence"][0].__setitem__("source_class", "blog"), "source_class")
        broke(lambda e: e["claims"][0].__setitem__("availability", "maybe"), "availability")
        broke(lambda e: e["claims"][0].__setitem__("confidence", 7), "confidence")
        broke(lambda e: e["claims"].append(copy.deepcopy(e["claims"][0])), "duplicate claim_key")
        broke(lambda e: e["claims"][0]["entity_link"].__setitem__("organisation_number", "999999999"), "different organisation")
        broke(lambda e: e["synthesis"]["grounded_claim_keys"].append("nope"), "unknown claim")
        broke(lambda e: e["coverage"].__setitem__("claims_total", 1), "coverage counts")
        fin = next(c for c in good["claims"] if c["field"] == "latest_revenue")

        def tamper(e):
            next(c for c in e["claims"] if c["field"] == "latest_revenue")["value"] = fin["value"] + 1
        broke(tamper, "not present in its evidence span")

        def absent_zero(e):
            c = next(c for c in e["claims"] if c["availability"] != "available")
            c["value"] = 0
        if any(c["availability"] != "available" for c in good["claims"]):
            broke(absent_zero, "absence rendered as a number")

    def test_validator_never_raises_on_garbage(self):
        from signalpost.contract import validate as V
        for junk in (None, 5, [], "x", {}, {"claims": 3}, {"organisation_number": 1, "claims": [None], "evidence": [1]}):
            self.assertIsInstance(V(junk), list)
            self.assertTrue(V(junk))

    def test_check_file_survives_malformed_files(self):
        from signalpost.contract import check_file
        good = json.dumps(self._env())
        cases = {"bad_json": good + "\n{not json\n", "dup": good + "\n" + good + "\n", "empty": "", "blank": "\n\n",
                 "array": "[1,2,3]\n", "scalar": "42\n"}
        for name, text in cases.items():
            p = self.tmp / f"{name}.jsonl"
            p.write_text(text, "utf-8")
            n, issues = check_file(p)
            self.assertTrue(issues, name)
        (self.tmp / "bin.jsonl").write_bytes(b"\xff\xfe\x00\x80garbage")
        self.assertTrue(check_file(self.tmp / "bin.jsonl")[1])
        self.assertTrue(check_file(self.tmp / "missing.jsonl")[1])
        ok = self.tmp / "ok.jsonl"
        ok.write_text(good + "\n", "utf-8")
        self.assertEqual(check_file(ok), (1, []))

    def test_manifest_edge_cases(self):
        from signalpost.pipeline import ManifestError, read_manifest
        def w(name, data):
            p = self.tmp / name
            p.write_bytes(data) if isinstance(data, bytes) else p.write_text(data, encoding="utf-8")
            return p
        o, _ = read_manifest(w("a.jsonl", '{"organisation_number":"923609016"}\n{broken\n923 609 016\n\n123\n'))
        self.assertEqual(o, ["923609016", "{broken", "123"])          # duplicate spelling collapsed, junk kept for typed envelope
        o, _ = read_manifest(w("b.txt", "\ufeff923609016\r\n"))
        self.assertEqual(o, ["923609016"])
        o, _ = read_manifest(w("c.txt", "923609016\n".encode("utf-16")))
        self.assertEqual(o, ["923609016"])
        o, h = read_manifest(w("d.json", json.dumps({"companies": [{"orgnr": 923609016, "name": "X"}, [1, 2], None]})))
        self.assertEqual(o[0], "923609016")
        for bad in (b"\x00\x01\x02binary", b"", b"\n# only comment\n"):
            with self.assertRaises(ManifestError):
                read_manifest(w("e.bin", bad))
        with self.assertRaises(ManifestError):
            read_manifest(self.tmp / "nope.txt")

    def test_cli_reports_unusable_input_without_traceback(self):
        from signalpost import cli
        sys_argv = sys.argv
        try:
            sys.argv = ["signalpost", "run", "--organisations", str(self.tmp / "nope.txt"), "--output", str(self.tmp / "o.jsonl")]
            self.assertEqual(cli.main(), 2)
            sys.argv = ["signalpost", "validate", "--file", str(self.tmp / "nope.jsonl")]
            self.assertEqual(cli.main(), 1)
        finally:
            sys.argv = sys_argv

    def test_seed_from_garbage_previous(self):
        from signalpost.pipeline import seed_from_envelopes
        p = self.tmp / "prev.jsonl"
        p.write_bytes(b'{"organisation_number": "923609016", "claims": [{"nokey": 1}]}\nnot json\n[1]\n\xff\xfe\n')
        self.assertEqual(seed_from_envelopes(State(self.tmp / "st"), p), 0)

    # ---- 3. timed-out tasks cannot mutate state ----
    def test_timed_out_worker_cannot_write_state_after_terminal_envelope(self):
        import threading
        import time as _t
        release = threading.Event()

        class Hang(FakeFetcher):
            def get(self, url, **k):
                if "roller" in url:
                    release.wait(10)
                return super().get(url, **k)
        st = State(self.tmp / "state")
        ag = Agent(Hang(), st)
        envs = run_batch([ORG], ag, workers=2, company_timeout=0.3)
        self.assertEqual(envs[0]["run"]["terminal_status"], "failed")
        self.assertEqual(envs[0]["errors"][0]["kind"], "TimeoutError")
        release.set()
        _t.sleep(1.0)  # let the abandoned worker run to its end
        self.assertIsNone(st.load(ORG), "abandoned worker wrote state after the timeout envelope")
        self.assertFalse(list((self.tmp / "state" / "companies").glob("*")))

    def test_timeout_keeps_previous_snapshot_intact(self):
        import threading
        import time as _t
        st = State(self.tmp / "state")
        Agent(FakeFetcher(), st).run_one(ORG)
        before = (self.tmp / "state" / "companies" / f"{ORG}.json").read_text(encoding="utf-8")
        release = threading.Event()

        class Hang(FakeFetcher):
            def get(self, url, **k):
                if "regnskap" in url:
                    release.wait(10)
                return super().get(url, **k)
        envs = run_batch([ORG], Agent(Hang(), st), workers=1, company_timeout=0.3)
        self.assertEqual(envs[0]["errors"][0]["kind"], "TimeoutError")
        release.set()
        _t.sleep(1.0)
        self.assertEqual((self.tmp / "state" / "companies" / f"{ORG}.json").read_text(encoding="utf-8"), before)

    def test_seal_after_commit_returns_false(self):
        ag = Agent(FakeFetcher(), State(self.tmp / "s"))
        ag.run_one(ORG)
        self.assertFalse(ag.seal(ORG))  # worker already committed: runner must use the worker's envelope

    def test_isolated_exception_also_seals(self):
        class Boom(Agent):
            def run_one(self, raw, hint=None):
                raise RuntimeError("boom")
        ag = Boom(FakeFetcher(), State(self.tmp / "s"))
        envs = run_batch([ORG, "bad"], ag, workers=2)
        self.assertEqual(len(envs), 2)
        self.assertTrue(all(e["run"]["terminal_status"] == "failed" for e in envs))


class ContractTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_manifest_official_jsonl(self):
        from signalpost.pipeline import read_manifest
        p = self.tmp / "m.jsonl"
        p.write_text('{"organisation_number":"923609016","name":"A AS","website":""}\n'
                     '{"organisation_number":"810034882","name":"B AS","website":"b.no"}\n'
                     '{"organisation_number":"923609016","name":"dup"}\n', encoding="utf-8")
        orgs, hints = read_manifest(p)
        self.assertEqual(orgs, ["923609016", "810034882"])
        self.assertEqual(hints["810034882"]["website"], "b.no")

    def test_manifest_other_formats(self):
        from signalpost.pipeline import read_manifest
        for name, body in (("a.txt", "923609016\n# c\n\n810034882\n"),
                           ("b.csv", "organisation_number,name\n923609016,X\n810034882,Y\n"),
                           ("c.json", '[{"organisation_number":"923609016"},"810034882"]'),
                           ("d.json", '{"organisations":["923609016","810034882"]}')):
            p = self.tmp / name
            p.write_text(body, encoding="utf-8")
            self.assertEqual(read_manifest(p)[0], ["923609016", "810034882"], name)

    def test_candidates(self):
        from signalpost.sources import candidates
        c = candidates("SANDNES ELEKTRISKE AS", "", [])
        self.assertIn("https://www.sandneselektriske.no/", c)
        self.assertIn("https://www.sandnes-elektriske.no/", c)
        self.assertEqual(candidates("X AS", "", []), [])  # too short to guess
        self.assertEqual(candidates("A AS", "firma.no", [])[0], "https://firma.no/")

    def test_cli_end_to_end_and_previous(self):
        import signalpost.cli as cli
        import sys
        man = self.tmp / "in.jsonl"
        man.write_text(f'{{"organisation_number":"{ORG}","name":"FIXTURE INDUSTRIER AS"}}\nbad\n', encoding="utf-8")
        out, rep = self.tmp / "o.jsonl", self.tmp / "r.json"
        orig = cli.Fetcher
        cli.Fetcher = lambda *a, **k: FakeFetcher()
        try:
            argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out), "--report", str(rep),
                    "--run-id", "t1", "--expected-count", "2", "--state", str(self.tmp / "s1")]
            sys.argv = argv
            self.assertEqual(cli.main(), 0)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(json.loads(rep.read_text(encoding="utf-8"))["silent_drops"], 0)
            out2 = self.tmp / "o2.jsonl"
            sys.argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out2), "--report", str(rep),
                        "--run-id", "t2", "--expected-count", "2", "--state", str(self.tmp / "s2"), "--previous", str(out)]
            self.assertEqual(cli.main(), 0)
            env = json.loads(out2.read_text(encoding="utf-8").splitlines()[0])
            self.assertFalse(env["refresh"]["is_first_observation"])
            self.assertEqual([c for c in env["changes"] if c["change_type"] != "added"], [])
            sys.argv = ["signalpost", "run", "--organisations", str(man), "--output", str(out2), "--run-id", "t3",
                        "--expected-count", "5", "--state", str(self.tmp / "s3")]
            self.assertEqual(cli.main(), 1)  # wrong expected count must fail loudly
        finally:
            cli.Fetcher = orig


if __name__ == "__main__":
    unittest.main()
