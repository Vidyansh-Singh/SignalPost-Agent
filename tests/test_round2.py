"""Fixes after the second reviewer feedback: CMS starter posts, parked domains, person LinkedIn links, hiring, viewer."""
import os, sys, json, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.environ.get("SIGNALPOST_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from urllib.parse import urlparse
from signalpost.sources import _news_ok, parked_reason, social_owner_ok, PLACEHOLDER_HEAD
from signalpost.render import render


class FakePg:
    def __init__(self, title="", body="", links=()):
        self.title, self.body, self.links = title, body, list(links)


class Round2(unittest.TestCase):
    def test_cms_starter_posts_are_not_news(self):
        for h in ("Hei, verden!", "Hello world!", "Hallo verden!", "Sample Page", "Just another WordPress site", "Coming soon", "Auto Draft"):
            self.assertFalse(_news_ok(h + " ", "https://x.no/2021/05/post/", "https://x.no/feed/", "x.no", True), h)
        self.assertFalse(_news_ok("Nice long headline here", "https://x.no/2021/05/hello-world/", "https://x.no/feed/", "x.no", True))
        self.assertTrue(_news_ok("Vi åpner nytt kontor i Oslo", "https://x.no/2025/05/nytt-kontor/", "https://x.no/feed/", "x.no", True))

    def test_parked_domain_pages(self):
        self.assertIsNotNone(parked_reason(FakePg("efactory.no", "This domain is for sale. Contact us for an offer.")))
        self.assertIsNotNone(parked_reason(FakePg("x", "Dette domenet er til salgs"), ""))
        self.assertIsNotNone(parked_reason(FakePg("x", "hi", [("https://dan.com/buy-domain/x.no", "buy")])))
        real = "Vi leverer regnskap og rådgivning. " * 200 + " domenet er til salgs i en artikkel om handel"
        self.assertIsNone(parked_reason(FakePg("Firma AS", "Vi leverer regnskap. " * 200)))

    def test_linkedin_person_profiles_rejected(self):
        self.assertFalse(social_owner_ok("Egalia AS", "linkedin", urlparse("https://www.linkedin.com/in/egalia-ola-nordmann"), trusted=True))
        self.assertFalse(social_owner_ok("Egalia AS", "linkedin", urlparse("https://www.linkedin.com/pub/egalia/1/2/3"), trusted=True))
        self.assertTrue(social_owner_ok("Egalia AS", "linkedin", urlparse("https://www.linkedin.com/company/egalia"), trusted=False))

    def test_viewer_has_sort_filter_compare(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_offline as T
        with tempfile.TemporaryDirectory() as d:
            env = T.agent(Path(d)).run_one(T.ORG)
            render([env, env], Path(d) / "site")
            idx = (Path(d) / "site" / "index.html").read_text(encoding="utf-8")
            for k in ('id="so"', "Compare selected", 'class="cmp"', 'id="st"', "application/json"):
                self.assertIn(k, idx)
            page = (Path(d) / "site" / f"{T.ORG}.html").read_text(encoding="utf-8")
            self.assertIn("Revenue by year", page) if any(c["field"] == "history" for c in env["claims"]) else None

    def test_parked_site_is_never_published_as_the_company_website(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_offline as T
        routes = dict(T.ROUTES)
        for u in list(routes):
            if "fixtureindustrier" in u:
                routes[u] = T.res(u, text="<html><title>fixtureindustrier.no</title>FIXTURE INDUSTRIER AS - This domain is for sale. Buy this domain today.</html>", ctype="text/html")
        with tempfile.TemporaryDirectory() as d:
            env = T.agent(Path(d), T.FakeFetcher(routes)).run_one(T.ORG)
            self.assertFalse([c for c in env["claims"] if c["field"] == "official_website" and c["availability"] == "available"])
            self.assertFalse([c for c in env["claims"] if c["field"] == "news_item" and c["availability"] == "available"])


if __name__ == "__main__":
    unittest.main()
