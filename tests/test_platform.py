"""Platform-level checks: ledger/resume, duplicate accounting, idempotent refresh, source policy, secrets, grounded
synthesis, viewer, measurement. Offline."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_offline as T  # noqa: F401
from test_offline import ER, ORG, RR, ACCOUNTS, FakeFetcher, agent, res, setUpModule, tearDownModule  # noqa: F401
from test_reviewer_fixes import AccountsRules, SUB, entity, run, valid_orgs

from signalpost import cli
from signalpost.contract import measure, validate
from signalpost.pipeline import Agent, Ledger, State, read_manifest, run_batch
from signalpost.render import render

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()


class LedgerResume(Base):
    def test_interrupted_batch_resumes_without_redoing_or_duplicating(self):
        x, y = valid_orgs(2, 880000000)
        orgs = [ORG, x, "not-a-number", y]
        ledger_path = self.tmp / "l.jsonl"
        calls: list[str] = []

        class Counting(FakeFetcher):
            def get(self, url, **k):
                calls.append(url)
                return super().get(url, **k)
        # first process finishes only the first two companies, then "crashes"
        a1 = Agent(Counting(), State(self.tmp / "s"))
        run_batch(orgs[:3], a1, workers=2, ledger=Ledger(ledger_path))
        first_calls = len(calls)
        # a torn last line from the crash must not break loading
        with open(ledger_path, "a", encoding="utf-8") as fh:
            fh.write('{"organisation_number": "123", "run": {')
        calls.clear()
        led = Ledger(ledger_path)
        self.assertEqual(sorted(led.done), sorted([ORG, x]))   # completed/partial are reused; the failed one is not
        out = run_batch(orgs, Agent(Counting(), State(self.tmp / "s")), workers=2, ledger=led)
        self.assertEqual([e["organisation_number"] for e in out], orgs)   # one per input, in order, no duplicates
        self.assertFalse([u for u in calls if f"/{ORG}" in u or f"/{ORG}/" in u])  # finished company was not re-researched
        self.assertGreater(first_calls, 0)

    def test_ledger_ignores_other_agent_versions(self):
        p = self.tmp / "l.jsonl"
        env = agent(self.tmp / "x").run_one(ORG)
        env["run"]["agent_version"] = "0.0.1"
        p.write_text(json.dumps(env) + "\n", encoding="utf-8")
        self.assertEqual(Ledger(p).done, {})

    def test_cli_resume_flag_end_to_end(self):
        man = self.tmp / "in.txt"
        man.write_text(f"{ORG}\n{ORG}\n", encoding="utf-8")                       # duplicate input line
        orig = cli.Fetcher
        cli.Fetcher = lambda *a, **k: FakeFetcher()
        argv = sys.argv
        try:
            out, rep = self.tmp / "o.jsonl", self.tmp / "r.json"
            base = ["signalpost", "run", "--organisations", str(man), "--output", str(out), "--report", str(rep),
                    "--run-id", "t", "--state", str(self.tmp / "st"), "--ledger", str(self.tmp / "led.jsonl")]
            sys.argv = base + ["--expected-count", "1"]
            self.assertEqual(cli.main(), 0)
            r = json.loads(rep.read_text(encoding="utf-8"))
            self.assertEqual((r["input_lines"], r["distinct_inputs"], r["duplicate_input_lines"], r["envelopes"]), (2, 1, 1, 1))
            sys.argv = base + ["--expected-count", "1", "--resume"]
            self.assertEqual(cli.main(), 0)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 1)
        finally:
            cli.Fetcher, sys.argv = orig, argv


class DuplicatesAndIdempotence(Base):
    def test_no_duplicate_claims_or_evidence_in_any_envelope(self):
        env = agent(self.tmp).run_one(ORG)
        keys = [c["claim_key"] for c in env["claims"]]
        ids = [e["id"] for e in env["evidence"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(ids), len(set(ids)))

    def test_rerun_on_identical_snapshots_reports_no_changes(self):
        a = agent(self.tmp)
        a.run_one(ORG)
        again = a.run_one(ORG)
        self.assertEqual(again["changes"], [])
        self.assertFalse(again["refresh"]["is_first_observation"])

    def test_formatting_only_difference_is_not_a_change(self):
        a = agent(self.tmp)
        a.run_one(ORG)
        routes = dict(T.ROUTES)
        routes[f"{ER}/enheter/{ORG}"] = res("", {**T.ENTITY, "navn": "FIXTURE INDUSTRIER AS"})
        self.assertEqual(Agent(FakeFetcher(routes), a.state).run_one(ORG)["changes"], [])

    def test_failed_refresh_keeps_last_supported_state_and_reports_no_material_change(self):
        a = agent(self.tmp)
        a.run_one(ORG)
        before = (self.tmp / "state" / "companies" / f"{ORG}.json").read_text(encoding="utf-8")
        down = Agent(FakeFetcher({}, default=lambda u: res(u, text="", status=None, error="timeout")), a.state)
        env = down.run_one(ORG)
        self.assertEqual(env["run"]["terminal_status"], "failed")
        self.assertEqual([c for c in env["changes"] if c["materiality"] == "material"], [])
        self.assertEqual((self.tmp / "state" / "companies" / f"{ORG}.json").read_text(encoding="utf-8"), before)

    def test_corrected_value_is_a_change_with_previous_value(self):
        a = agent(self.tmp)
        a.run_one(ORG)
        routes = dict(T.ROUTES)
        routes[f"{ER}/enheter/{ORG}"] = res("", {**T.ENTITY, "navn": "FIXTURE NY AS"})
        env = Agent(FakeFetcher(routes), a.state).run_one(ORG)
        ch = [c for c in env["changes"] if c["field"] == "legal_name"]
        self.assertEqual((ch[0]["previous_value"], ch[0]["current_value"], ch[0]["materiality"]),
                         ("FIXTURE INDUSTRIER AS", "FIXTURE NY AS", "material"))

    def test_duplicate_envelopes_are_measured(self):
        env = agent(self.tmp).run_one(ORG)
        f = self.tmp / "dup.jsonl"
        f.write_text(json.dumps(env) + "\n" + json.dumps(env) + "\n", encoding="utf-8")
        m = measure(f)
        self.assertEqual(m["duplicate_envelopes"], 1)
        self.assertFalse(m["publication_ready"])


class SourcePolicyAndSecrets(Base):
    ALLOWED = {"data.brreg.no", "api.gleif.org", "query.wikidata.org", "rdap.norid.no"}
    PROHIBITED = ("linkedin.com", "facebook.com", "instagram.com", "glassdoor", "indeed.", "google.", "bing.", "duckduckgo",
                  "brave.com", "arbeidsplassen", "nav.no", "twitter.com", "x.com", "tiktok", "youtube")

    def test_full_pipeline_only_contacts_permitted_hosts_and_never_a_social_or_search_site(self):
        seen: list[str] = []

        class Spy(FakeFetcher):
            def get(self, url, **k):
                seen.append(url)
                return super().get(url, **k)
        routes = dict(T.ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text=T.HOME.replace(
            "</body>", '<a href="https://www.facebook.com/fixtureindustrier">f</a><a href="https://www.linkedin.com/company/fixture-industrier">l</a></body>'), ctype="text/html")
        agent(self.tmp, Spy(routes)).run_one(ORG)
        hosts = {re.match(r"https?://([^/]+)", u).group(1).lower() for u in seen}
        self.assertTrue(seen)
        for h in hosts:
            self.assertFalse(any(p in h for p in self.PROHIBITED), h)
            self.assertTrue(h in self.ALLOWED or h.endswith("fixtureindustrier.no"), h)

    def test_starter_kit_linkedin_scripts_and_search_connectors_are_absent_from_the_scored_path(self):
        files = [p for p in ROOT.rglob("*") if p.suffix in (".py", ".toml", ".txt") and "tests" not in p.parts and "__pycache__" not in p.parts]
        self.assertTrue(files)
        names = " ".join(p.name for p in files)
        for bad in ("linkedin_guest", "brave", "google_news", "google_maps", "youtube_search", "fagfolkguiden"):
            self.assertNotIn(bad, names)
        code = "\n".join(p.read_text("utf-8", "replace") for p in (ROOT / "src").rglob("*.py"))
        for bad in ("run_linkedin_guest", "api.search.brave", "google.com/search", "bing.com/search", "duckduckgo.com", "serpapi"):
            self.assertNotIn(bad, code)
        # social hosts appear only as recognisers of links found on a company's own site, never as fetch targets
        from signalpost.fetch import Fetcher
        f = Fetcher(None, delay=0, obey_robots=False)
        orig = Fetcher._open
        Fetcher._open = lambda self, req, timeout: (_ for _ in ()).throw(AssertionError("network must not be touched"))
        try:
            self.assertTrue(f.get("https://localhost/").error.startswith("unsafe_url"))
        finally:
            Fetcher._open = orig

    def test_no_secrets_in_repository(self):
        pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{10,}|"
                         r"-----BEGIN [A-Z ]*PRIVATE KEY-----|api[_-]?key\s*[=:]\s*['\"][A-Za-z0-9]{16,})", re.I)
        for p in ROOT.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".md", ".txt", ".toml", ".json", ".jsonl", ".html") and "__pycache__" not in p.parts:
                self.assertIsNone(pat.search(p.read_text("utf-8", "replace")), str(p))

    def test_environment_is_not_logged_or_embedded(self):
        code = "\n".join(p.read_text("utf-8") for p in (ROOT / "src").rglob("*.py"))
        self.assertNotIn("os.environ", code)  # the agent needs no secrets; nothing can leak one


class GroundedSynthesis(Base):
    NUM = re.compile(r"\d[\d ]*\d|\d")

    def test_every_number_and_date_in_the_summary_traces_to_a_claim(self):
        env = agent(self.tmp).run_one(ORG)
        haystack = json.dumps([c["value"] for c in env["claims"] if c["availability"] == "available"], ensure_ascii=False)
        haystack += " " + " ".join(c.get("period") or "" for c in env["claims"]) + " " + ORG
        flat = re.sub(r"\s", "", haystack)
        for sent in env["synthesis"]["summary"]:
            for m in self.NUM.findall(sent):
                self.assertIn(re.sub(r"\s", "", m), flat, f"{m!r} in {sent!r}")
        self.assertTrue(env["synthesis"]["grounded_claim_keys"])
        keys = {c["claim_key"] for c in env["claims"]}
        self.assertLessEqual(set(env["synthesis"]["grounded_claim_keys"]), keys)

    def test_ambiguous_site_is_never_promoted_to_a_verified_fact(self):
        routes = dict(T.ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html>Helt annet AS org.nr. 999 999 999</html>", ctype="text/html")
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        txt = " ".join(env["synthesis"]["summary"])
        self.assertNotIn("verified company website is", txt)
        self.assertIn("did not meet the identity threshold", txt)

    def test_like_for_like_comparison_is_stated_only_for_twelve_month_filings(self):
        rows = [AccountsRules.row("2024-01-01", "2024-12-31", 20), AccountsRules.row("2025-01-01", "2025-12-31", 30)]
        env = run(self.tmp, {f"{ER}/enheter/{SUB}": res("", entity(SUB, "ZETA RETAIL AS")), f"{RR}/{SUB}": res("", rows)})
        txt = " ".join(env["synthesis"]["summary"])
        self.assertIn("moved from 20 NOK (2024-12-31) to 30 NOK (2025-12-31), up 50.0%, both 12-month filings", txt)
        short = [AccountsRules.row("2024-01-01", "2024-12-31", 20), AccountsRules.row("2025-07-01", "2025-12-31", 30)]
        env = run(self.tmp, {f"{ER}/enheter/{SUB}": res("", entity(SUB, "ZETA RETAIL AS")), f"{RR}/{SUB}": res("", short)})
        self.assertNotIn("moved from", " ".join(env["synthesis"]["summary"]))


class Viewer(Base):
    def test_pages_link_evidence_escape_markup_and_never_present_candidates_as_facts(self):
        routes = dict(T.ROUTES)
        routes["https://www.fixtureindustrier.no/"] = res("", text="<html>Helt annet AS org.nr. 999 999 999</html>", ctype="text/html")
        routes[f"{ER}/enheter/{ORG}"] = res("", {**T.ENTITY, "navn": 'EVIL <script>alert(1)</script> AS'})
        env = agent(self.tmp, FakeFetcher(routes)).run_one(ORG)
        render([env], self.tmp / "site")
        page = (self.tmp / "site" / f"{ORG}.html").read_text("utf-8")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn('name="viewport"', page)
        self.assertIn("https://data.brreg.no/", page)                 # evidence link
        self.assertIn("retrieved ", page)
        self.assertIn("sha256", page)
        self.assertIn("not published as a fact", page)                # ambiguous website
        index = (self.tmp / "site" / "index.html").read_text("utf-8")
        self.assertIn('type="search"', index)
        self.assertIn(f'{ORG}.html', index)

    def test_javascript_urls_are_never_linked(self):
        env = agent(self.tmp).run_one(ORG)
        env["evidence"][0]["source_url"] = "javascript:alert(1)"
        render([env], self.tmp / "site")
        self.assertNotIn('href="javascript:', (self.tmp / "site" / f"{ORG}.html").read_text("utf-8"))


class Measure(Base):
    def test_all_failed_run_is_flagged_and_warned_about(self):
        down = Agent(FakeFetcher({}, default=lambda u: res(u, text="", status=None, error="timeout")), State(self.tmp / "s"))
        envs = run_batch([ORG], down, workers=1)
        f = self.tmp / "o.jsonl"
        f.write_text(json.dumps(envs[0]) + "\n", encoding="utf-8")
        m = measure(f)
        self.assertTrue(m["warnings"])
        self.assertFalse(m["publication_ready"])
        man, orig, argv = self.tmp / "in.txt", cli.Fetcher, sys.argv
        man.write_text(f"{ORG}\n", encoding="utf-8")
        cli.Fetcher = lambda *a, **k: FakeFetcher({}, default=lambda u: res(u, text="", status=None, error="timeout"))
        import contextlib
        import io
        err = io.StringIO()
        try:
            sys.argv = ["signalpost", "run", "--organisations", str(man), "--output", str(self.tmp / "o2.jsonl"),
                        "--report", str(self.tmp / "r.json"), "--state", str(self.tmp / "st")]
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cli.main()
        finally:
            cli.Fetcher, sys.argv = orig, argv
        self.assertIn("python run.py diagnose", err.getvalue())

    def test_clean_run_is_publication_ready_and_every_input_is_accounted_for(self):
        envs = run_batch([ORG, "bad-number"], agent(self.tmp), workers=2)
        f, inp = self.tmp / "o.jsonl", self.tmp / "i.txt"
        f.write_text("".join(json.dumps(e) + "\n" for e in envs), encoding="utf-8")
        inp.write_text(f"{ORG}\nbad-number\n", encoding="utf-8")
        m = measure(f, inp)
        self.assertEqual((m["envelopes"], m["missing_inputs"], m["unexpected_envelopes"], m["contract_issues"]), (2, 0, 0, 0))
        self.assertTrue(m["publication_ready"])
        self.assertEqual(m["evidence_span_rate"], 1.0)

    def test_missing_input_blocks_publication(self):
        envs = run_batch([ORG], agent(self.tmp), workers=1)
        f, inp = self.tmp / "o.jsonl", self.tmp / "i.txt"
        f.write_text(json.dumps(envs[0]) + "\n", encoding="utf-8")
        inp.write_text(f"{ORG}\n{valid_orgs(1, 870000000)[0]}\n", encoding="utf-8")
        m = measure(f, inp)
        self.assertEqual(m["missing_inputs"], 1)
        self.assertFalse(m["publication_ready"])

    def test_malformed_company_and_company_with_no_facts_do_not_break_the_batch(self):
        none = Agent(FakeFetcher({}, default=lambda u: res(u, text="", status=404, error="http_404")), State(self.tmp / "s"))
        out = run_batch([ORG, "§§§", "", valid_orgs(1, 860000000)[0]], none, workers=2)
        self.assertEqual(len(out), 4)
        for e in out:
            self.assertEqual(validate(e), [])
            self.assertIn(e["run"]["terminal_status"], ("failed", "partial"))


if __name__ == "__main__":
    unittest.main()
