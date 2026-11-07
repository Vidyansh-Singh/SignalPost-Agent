"""Per-company orchestration, refresh diffs, synthesis, validation.

Guarantees: exactly one terminal envelope per input; one company's failure cannot
abort the batch; absence is typed; re-running a company overwrites its state key.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import sources as S
from .core import AGENT_VERSION, Claim, Ctx, find_orgnums, is_valid, normalise, utcnow
from .contract import check_file, validate  # noqa: F401  (re-exported)
from .fetch import Fetcher

FAMILIES = ["legal_identity", "financials", "leadership", "locations", "group_structure",
            "digital_presence", "social_profiles", "contact", "hiring", "public_activity",
            "workforce", "business_profile"]
MATERIAL = {"legal_identity", "financials", "leadership", "group_structure", "digital_presence"}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    """One JSON file per organisation number: re-runs overwrite, never append."""

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root else None
        if self.root:
            (self.root / "companies").mkdir(parents=True, exist_ok=True)

    def load(self, org: str) -> dict | None:
        p = self.root / "companies" / f"{org}.json" if self.root else None
        try:
            return json.loads(p.read_text("utf-8")) if p and p.exists() else None
        except Exception:
            return None

    def save(self, org: str, snap: dict) -> None:
        if self.root:
            atomic_write(self.root / "companies" / f"{org}.json", json.dumps(snap, ensure_ascii=False, sort_keys=True))


def seed_from_envelopes(state: "State", path: str | Path) -> int:
    """Previous-snapshot input: rebuild per-company snapshots from an earlier envelope JSONL.
    Existing state wins; this only fills gaps. Malformed lines/envelopes are skipped, never fatal."""
    n = 0
    text = Path(path).read_bytes().decode("utf-8-sig", "replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            env = json.loads(line)
            org = env.get("organisation_number")
            if not isinstance(org, str) or not is_valid(org) or state.load(org) is not None:
                continue
            ts = (env.get("refresh") or {}).get("last_refreshed_at") or (env.get("run") or {}).get("completed_at")
            claims = {}
            for c in env.get("claims") or []:
                ok = c.get("availability") == "available"
                claims[c["claim_key"]] = {"family": c.get("field_family"), "field": c.get("field"),
                                          "availability": c.get("availability"),
                                          "hash": vhash(c.get("value")) if ok else None,
                                          "value": c.get("value") if ok else None,
                                          "evidence_ids": c.get("evidence_ids", []), "first": ts, "last": ts}
            runs = int((env.get("refresh") or {}).get("run_count", 1))
            state.save(org, {"claims": claims, "last_run_at": ts, "runs": runs})
            n += 1
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            continue
    return n


def vhash(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def dedupe(claims: list[Claim]) -> list[Claim]:
    best: dict[str, Claim] = {}
    for c in claims:
        cur = best.get(c.claim_key)
        if cur is None:
            best[c.claim_key] = c
        elif c.availability == "available" and cur.availability != "available":
            best[c.claim_key] = c
        elif c.availability == "available" and cur.availability == "available":
            if c.value != cur.value:
                cur.alternatives.append({"value": c.value, "evidence_ids": c.evidence_ids})
            else:
                cur.evidence_ids = list(dict.fromkeys(cur.evidence_ids + c.evidence_ids))
    return list(best.values())


TRANSIENT = ("failed", "blocked")   # the source could not be read this time: says nothing about the fact itself


def diff(prev: dict | None, claims: list[Claim]) -> tuple[list[dict], dict]:
    """Typed change detection. A claim whose source was temporarily unreadable keeps its previous supported value
    (carried forward untouched) and produces no change; a claim in a family that was not attempted this run (budget
    cut, outage) is carried forward too. Only a successful look that finds something different is a change."""
    now = utcnow()
    old = (prev or {}).get("claims") or {}
    cur: dict[str, dict] = {}
    for c in claims:
        rec = {"family": c.field_family, "field": c.field, "availability": c.availability,
               "hash": vhash(c.value) if c.availability == "available" else None,
               "value": c.value if c.availability == "available" else None,
               "evidence_ids": c.evidence_ids, "first": now, "last": now}
        o = old.get(c.claim_key)
        if c.availability in TRANSIENT and o and o.get("hash"):
            cur[c.claim_key] = {**o, "last_attempt": {"state": c.availability, "at": now}}   # carried forward
            continue
        if o and o.get("hash") and o.get("hash") == rec["hash"]:
            rec["first"] = o.get("first") or now
        cur[c.claim_key] = rec
    looked = {c.field_family for c in claims if c.availability in ("available", "not_available", "not_applicable")}
    carried = {k for k, r in cur.items() if r.get("last_attempt")}
    for k, o in old.items():
        if k not in cur and o.get("hash") and o.get("family") not in looked:
            cur[k] = o                      # family not examined this run: keep what we knew
            carried.add(k)
    changes: list[dict] = []
    if prev is not None:
        def mat(r): return "material" if r["family"] in MATERIAL else "minor"
        for k, r in cur.items():
            if k in carried:
                continue
            o = old.get(k)
            if r["availability"] == "available" and (o is None or o.get("hash") is None):
                changes.append({"claim_key": k, "field": r["field"], "change_type": "added",
                                "current_value": r["value"], "observed_at": now, "materiality": mat(r)})
            elif o and o.get("hash") and r["hash"] != o["hash"]:
                kind = "changed" if r["availability"] == "available" else "removed"
                changes.append({"claim_key": k, "field": r["field"], "change_type": kind,
                                "previous_value": o.get("value"), "current_value": r["value"],
                                "first_observed_at": o.get("first"), "observed_at": now, "materiality": mat(r)})
        for k, o in old.items():
            if k not in cur and o.get("hash"):
                changes.append({"claim_key": k, "field": o.get("field"), "change_type": "removed",
                                "previous_value": o.get("value"), "observed_at": now, "materiality": "minor"})
    return changes, {"claims": cur, "last_run_at": now, "runs": int((prev or {}).get("runs", 0)) + 1}


def coverage(claims: list[Claim]) -> dict:
    fams = {f: {"attempted": 0, "available": 0, "states": {}} for f in FAMILIES}
    for c in claims:
        f = fams.setdefault(c.field_family, {"attempted": 0, "available": 0, "states": {}})
        f["attempted"] += 1
        f["states"][c.availability] = f["states"].get(c.availability, 0) + 1
        f["available"] += c.availability == "available"
    with_data = sum(1 for v in fams.values() if v["available"])
    return {"families": fams, "families_expected": len(FAMILIES), "families_with_data": with_data,
            "family_coverage_ratio": round(with_data / len(FAMILIES), 4),
            "claims_total": len(claims), "claims_available": sum(c.availability == "available" for c in claims)}


def synthesise(org: str, ident: dict, claims: list[Claim], changes: list[dict], cov: dict) -> dict:
    """Extractive only: every sentence is built from claims that carry evidence."""
    idx = {}
    for c in claims:
        if c.availability == "available":
            idx.setdefault(f"{c.field_family}|{c.field}", c)
    used: list[str] = []

    def take(k):
        c = idx.get(k)
        if c:
            used.append(c.claim_key)
        return c

    name = ident.get("legal_name") or f"Organisation {org}"
    form, reg = take("legal_identity|organisation_form"), take("legal_identity|registration_date")
    ind = take("business_profile|industry_description")
    head = f"{name} ({org})" + (f" — {form.value}" if form else "") + (f", {ind.value}" if ind else "")
    s = [f"{name} is registered in the Norwegian Central Coordinating Register under organisation number {org}"
         + (f" as a {form.value}" if form else "") + (f", registered {reg.value}" if reg else "") + "."]
    flags = ident.get("status_flags") or []
    s.append("The registry records status flag(s): " + ", ".join(f.replace("_", " ") for f in flags) + "."
             if flags else "The registry records no bankruptcy or liquidation flag.")
    rev, prof, per = take("financials|latest_revenue"), take("financials|latest_profit_for_year"), take("financials|latest_period")
    parts = ([f"operating revenue of {int(rev.value):,} {rev.unit}".replace(",", " ")] if rev else []) + \
            ([f"a profit for the year of {int(prof.value):,} {prof.unit}".replace(",", " ")] if prof else [])
    s.append(f"The latest filed annual accounts report {' and '.join(parts)}" + (f" for the period ending {per.value}" if per else "") + "."
             if parts else "No filed annual-accounts figures were retrievable from the accounts register.")
    hist = take("financials|history")
    if hist and isinstance(hist.value, list) and len(hist.value) >= 2:
        a_, b_ = hist.value[-2], hist.value[-1]
        if isinstance(a_.get("revenue"), (int, float)) and isinstance(b_.get("revenue"), (int, float)):
            n1, n2 = f"{int(a_['revenue']):,}".replace(",", " "), f"{int(b_['revenue']):,}".replace(",", " ")
            pct = ""
            if a_["revenue"]:
                d = (b_["revenue"] - a_["revenue"]) / abs(a_["revenue"]) * 100
                pct = f", {'up' if d >= 0 else 'down'} {abs(d):.1f}%"
            s.append(f"Operating revenue moved from {n1} {hist.unit} ({a_['period']}) to {n2} {hist.unit} ({b_['period']}){pct}, "
                     "both 12-month filings.")
    emp, wp = take("workforce|registered_employees"), take("locations|registered_workplace_count")
    bits = ([f"{emp.value} registered employees"] if emp else []) + ([f"{wp.value} registered workplace(s)"] if wp else [])
    if bits:
        s.append("The register lists " + " and ".join(bits) + ".")
    roles = [c for c in claims if c.field_family == "leadership" and c.availability == "available"
             and c.field.startswith("role_") and c.field != "role_holder_count" and isinstance(c.value, str)]
    if roles:
        used.extend(c.claim_key for c in roles[:4])
        s.append("Registered role holders include " + "; ".join(f"{c.value} ({c.note})" for c in roles[:4]) + ".")
    site = take("digital_presence|official_website")
    if site:
        s.append(f"The verified company website is {site.value} (identity proven by {', '.join(site.entity_link.matched_on)}).")
    else:
        amb = next((c for c in claims if c.field == "official_website" and c.availability == "ambiguous"), None)
        s.append("A candidate website was found but did not meet the identity threshold, so it is not published."
                 if amb else "No company-owned website could be verified for this organisation number.")
    jobs, act = take("hiring|open_position_count"), None
    if jobs:
        s.append(f"{jobs.value} open position(s) were listed on the company's own careers page at retrieval time.")
    dates = [c.value["date_published"] for c in claims if c.field == "news_item" and c.availability == "available"]
    if dates:
        s.append(f"The most recent dated public activity observed is {max(dates)}.")
    mat = [f"{c['field']}: {c.get('previous_value')!r} -> {c.get('current_value')!r} ({c['change_type']})"
           for c in changes if c["materiality"] == "material"][:12] or ["No material change detected since the previous run."]
    unknown = [f"{f}: no value published ({', '.join(sorted(i['states'])) or 'not attempted'})"
               for f, i in sorted(cov["families"].items()) if not i["available"]]
    return {"headline": head, "summary": s, "material_changes": mat, "unknowns": unknown,
            "grounded_claim_keys": sorted(set(used))}


class Cancelled(BaseException):
    """Raised inside a worker whose company already received a terminal envelope."""


class Agent:
    def __init__(self, fetcher: Fetcher, state: State, enable_site: bool = True, soft_deadline: float | None = None):
        self.f, self.state, self.enable_site = fetcher, state, enable_site
        self.soft_deadline = soft_deadline  # time.monotonic() value; after it, registry-only mode
        self.degraded = 0
        self._gate = threading.Lock()
        self._sealed: set[str] = set()   # orgs whose terminal envelope has been issued by the batch runner
        self._saved: set[str] = set()    # orgs whose worker already committed state

    # ---- cancellation gate: after seal(), a worker can no longer write state ----
    def seal(self, raw: str) -> bool:
        """Mark the company terminal. Returns False if its worker had already committed state (so the worker's
        own envelope is the consistent answer and must be used instead of a timeout envelope)."""
        key = normalise(raw) or str(raw)[:32]
        with self._gate:
            if key in self._saved:
                return False
            self._sealed.add(key)
            return True

    def _sealed_now(self, org: str) -> bool:
        with self._gate:
            return org in self._sealed

    def _check(self, org: str) -> None:
        if self._sealed_now(org):
            raise Cancelled(org)

    def _commit(self, org: str, snap: dict) -> None:
        with self._gate:  # check-and-write is atomic with respect to seal()
            if org in self._sealed:
                raise Cancelled(org)
            self.state.save(org, snap)
            self._saved.add(org)

    def _shell(self, org, valid, ident, claims, evidence, errors, run_id, started, t0, terminal=None,
               note_refresh=None) -> dict:
        claims = dedupe(claims)
        used = {i for c in claims for i in c.evidence_ids}
        evidence = [e for e in evidence if e.id in used]
        prev = self.state.load(org) if valid else None
        changes, snap = diff(prev, claims)
        cov = coverage(claims)
        syn = synthesise(org, ident, claims, changes, cov)
        if terminal is None:
            avail = cov["claims_available"]
            terminal = ("failed" if not avail else "partial" if errors or cov["family_coverage_ratio"] < 0.5 else "completed")
        env = {"organisation_number": org, "input_valid": valid, "legal_identity": ident,
               "claims": [asdict(c) for c in claims], "evidence": [asdict(e) for e in evidence],
               "changes": changes, "errors": errors, "synthesis": syn,
               "refresh": note_refresh or {"last_refreshed_at": utcnow(), "previous_run_at": (prev or {}).get("last_run_at"),
                                           "run_count": snap["runs"], "is_first_observation": prev is None,
                                           "material_change_count": sum(c["materiality"] == "material" for c in changes),
                                           "recommended_refresh_interval_days": 7 if changes else 14},
               "coverage": cov,
               "operations": {"requests": self.f.requests, "cache_hits": self.f.cache_hits,
                              "runtime_ms": int((time.monotonic() - t0) * 1000), "third_party_cost_usd": 0.0,
                              "robots_denied": self.f.robots_denied},
               "run": {"run_id": run_id, "agent_version": AGENT_VERSION, "started_at": started,
                       "completed_at": utcnow(), "terminal_status": terminal}}
        if valid and terminal != "failed" or (valid and cov["claims_available"]):
            self._commit(org, snap)  # a failed refresh keeps the last supported state
        return env

    def _terminal(self, raw, kind: str, note: str, valid: bool) -> dict:
        org = normalise(raw) or str(raw)[:32]
        c = Ctx(org)
        c.absent("legal_identity", "legal_name", "failed" if valid else "not_applicable", note)
        err = [{"stage": "run", "target": org, "kind": kind, "message": note, "retryable": valid, "occurred_at": utcnow()}]
        t0 = time.monotonic()
        env = self._shell(org, valid, {"organisation_number": org, "legal_name": None, "status_flags": []},
                          c.claims, [], err, "terminal", utcnow(), t0, "failed",
                          {"last_refreshed_at": utcnow(), "is_first_observation": True, "note": note})
        return env

    def timeout_envelope(self, raw: str, limit: float) -> dict:
        return self._terminal(raw, "TimeoutError", f"per-company budget of {limit}s exceeded", is_valid(raw))

    def run_one(self, raw: str, hint: dict | None = None) -> dict:
        try:
            return self._run_one(raw, hint)
        except Cancelled:
            return {}  # discarded by the runner: a timeout envelope was already issued

    def _group_hosts(self, ident: dict) -> set[str]:
        """Registry websites of the parent unit and of its parent (two levels). A subsidiary that lists one of these
        as its own is pointing at the group's site."""
        hosts: set[str] = set()
        nxt, hops = ident.get("parent_orgnr"), 0
        while nxt and hops < 2 and is_valid(nxt):
            d, _ = self.f.get_json(f"{S.ER}/enheter/{nxt}", ttl=21_600)
            if not isinstance(d, dict):
                break
            site = d.get("hjemmeside")
            if isinstance(site, str) and site.strip():
                h = urlparse(site if "//" in site else "//" + site).netloc.lower().removeprefix("www.")
                if h:
                    hosts.add(h)
            nxt, hops = d.get("overordnetEnhet") if isinstance(d.get("overordnetEnhet"), str) else None, hops + 1
        return hosts

    def _run_one(self, raw: str, hint: dict | None = None) -> dict:
        t0, started, run_id = time.monotonic(), utcnow(), uuid.uuid4().hex[:12]
        org = normalise(raw)
        if not is_valid(org):
            return self._terminal(raw, "InvalidOrganisationNumber",
                                  "input is not a valid Norwegian organisation number (9 digits, MOD-11)", False)
        c, errors = Ctx(org), []

        def fail(stage, e):
            errors.append({"stage": stage, "target": org, "kind": type(e).__name__, "message": str(e)[:300],
                           "retryable": True, "occurred_at": utcnow()})

        ident = {"organisation_number": org, "status_flags": []}
        data, r = self.f.get_json(f"{S.ER}/enheter/{org}", ttl=21_600)
        if not isinstance(data, dict):
            st = "blocked" if r.blocked_by_robots else "not_available" if r.status == 404 else "failed"
            c.absent("legal_identity", "legal_name", st,
                     "no unit exists in the register for this organisation number" if st == "not_available"
                     else f"registry lookup unsuccessful ({r.error})")
            return self._shell(org, True, ident, c.claims, c.evidence, errors, run_id, started, t0,
                               "partial" if st == "not_available" else "failed")
        try:
            ident = S.registry_identity(c, data, r)
        except Exception as e:  # noqa: BLE001
            fail("registry.identity", e)

        for label, url, mapper, fam, fld in (
                ("roles", f"{S.ER}/enheter/{org}/roller", S.registry_roles, "leadership", "role_holders"),
                ("subunits", None, S.registry_subunits, "locations", "registered_workplaces"),
                ("accounts", f"{S.RR}/{org}", S.registry_accounts, "financials", "latest_annual_accounts")):
            try:
                payload, rr = S.fetch_subunits(self.f, org) if label == "subunits" else self.f.get_json(url, ttl=21_600)
                if payload is None:
                    st = "blocked" if rr.blocked_by_robots else "not_available" if rr.status in (404, 204) else "failed"
                    c.absent(fam, fld, st, f"{label} endpoint returned no payload ({rr.error})")
                else:
                    mapper(c, payload, rr)
            except Exception as e:  # noqa: BLE001
                fail(f"registry.{label}", e)
                c.absent(fam, fld, "failed", f"{label} raised {type(e).__name__}")
        self._check(org)
        late = self.soft_deadline is not None and time.monotonic() > self.soft_deadline
        if late:
            self.degraded += 1
            errors.append({"stage": "budget", "target": org, "kind": "SoftDeadline",
                           "message": "time budget nearly spent: external sources skipped, registry data kept",
                           "retryable": True, "occurred_at": utcnow()})
        for label, fn in (() if late else (("wikidata", S.wikidata), ("gleif", S.gleif))):
            self._check(org)
            try:
                fn(c, self.f)
            except Exception as e:  # noqa: BLE001
                fail(f"connector.{label}", e)

        self._check(org)
        if self.enable_site and not late:
            try:
                extra = [x.value for x in c.claims if x.field == "wikidata_official_website" and x.availability == "available"]
                hw = (hint or {}).get("website") if isinstance((hint or {}).get("website"), str) else None
                nm = ident.get("legal_name") or (hint or {}).get("name") or ""
                cands = S.candidates(nm, ident.get("registry_website_raw") or hw, extra)
                if not cands:
                    c.absent("digital_presence", "official_website", "not_available", "no website candidate could be generated")
                else:
                    off = set()
                    for u in [ident.get("registry_website_raw"), hw] + extra:
                        if isinstance(u, str) and u.strip():
                            host = urlparse(u if "//" in u else "//" + u).netloc.lower().removeprefix("www.")
                            if host:
                                off.add(host)
                    places = [ident.get("postal_place"), ident.get("municipality"), (hint or {}).get("municipality")]
                    res = S.verify_site(c, self.f, ident.get("legal_name") or "", cands, official=off,
                                        places=places, postal=ident.get("postal_code"),
                                        group_hosts=self._group_hosts(ident))
                    if res:
                        url, hr, pg, link = res
                        S.mine_site(c, self.f, url, hr, pg, link, name=ident.get("legal_name") or "")
            except Exception as e:  # noqa: BLE001
                fail("website", e)
        self._check(org)
        return self._shell(org, True, ident, c.claims, c.evidence, errors, run_id, started, t0)


def _org_from(obj) -> tuple[str, dict]:
    if isinstance(obj, dict):
        for k in ("organisation_number", "organisasjonsnummer", "orgnr", "org_number", "organization_number"):
            if obj.get(k) not in (None, ""):
                return str(obj[k]).strip(), obj
        return "", obj
    if isinstance(obj, (list, tuple, set)):
        return "", {}
    return str(obj).strip(), {}


class ManifestError(ValueError):
    """The input file cannot be used at all (missing, unreadable, not text, or empty)."""


def read_manifest(path: str | Path, stats: dict | None = None) -> tuple[list[str], dict[str, dict]]:
    """Accept JSONL objects, a JSON array/object, or plain text (one number per line, first CSV column).
    Returns ordered unique inputs and optional per-org hints. Malformed lines are kept as inputs (they receive a
    typed `not_applicable` envelope) rather than aborting the batch; only an unreadable file raises ManifestError."""
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as e:
        raise ManifestError(f"cannot read input file {path}: {e.strerror or type(e).__name__}") from None
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raw = raw_bytes.decode("utf-16", "replace")           # Windows PowerShell redirects write UTF-16
    elif b"\x00" in raw_bytes[:4096]:
        raise ManifestError("input file looks binary (NUL bytes); expected JSON, JSONL or text")
    else:
        raw = raw_bytes.decode("utf-8-sig", "replace")
    items: list = []
    txt = raw.strip()
    parsed = False
    if txt[:1] in ("[", "{"):
        try:
            data = json.loads(txt)
            if isinstance(data, dict):
                data = data.get("organisations") or data.get("companies") or data.get("items") or [data]
            if isinstance(data, list):
                items, parsed = data, True
        except (ValueError, RecursionError):
            parsed = False
    if not parsed:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line[:1] == "{":
                try:
                    items.append(json.loads(line))
                    continue
                except (ValueError, RecursionError):
                    pass
            first = line.split(",")[0].split(";")[0].split("\t")[0].strip().strip('"')
            if first.lower().startswith(("org", "id")) and not first[:1].isdigit():
                continue
            items.append(first[:64])
    orgs, hints, seen, lines = [], {}, set(), 0
    for it in items:
        o, hint = _org_from(it)
        o = o[:64]
        key = normalise(o) or o
        if o:
            lines += 1
        if not o or key in seen:
            continue
        seen.add(key)
        orgs.append(o)
        if hint:
            hints[normalise(o)] = {k: v for k, v in hint.items() if isinstance(k, str)}
    if not orgs:
        raise ManifestError(f"no organisation numbers found in {path}")
    if stats is not None:
        stats.update(input_lines=lines, distinct_inputs=len(orgs), duplicate_input_lines=lines - len(orgs))
    return orgs, hints


def read_inputs(path: str | Path) -> list[str]:
    return read_manifest(path)[0]


class Ledger:
    """Append-only, fsync'd record of finished envelopes so an interrupted batch can resume without redoing or
    duplicating work. One line per organisation; the last line for an organisation wins. Only envelopes that carry
    data (`completed`/`partial`) are reused; `failed` ones (timeouts, outages) are retried on resume."""

    def __init__(self, path: str | Path, agent_version: str = AGENT_VERSION):
        self.path, self.version = Path(path), agent_version
        self.done: dict[str, dict] = {}
        self._lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_bytes().decode("utf-8", "replace").splitlines():
                try:
                    env = json.loads(line)
                    org = env["organisation_number"]
                    if env["run"]["agent_version"] == self.version and env["run"]["terminal_status"] in ("completed", "partial"):
                        self.done[org] = env
                except (ValueError, KeyError, TypeError):
                    continue          # a torn last line from a crash is simply ignored
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, raw: str) -> dict | None:
        return self.done.get(normalise(raw))

    def record(self, env: dict) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(env, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def run_batch(inputs: list[str], agent: Agent, *, workers: int = 16, company_timeout: float = 150.0,
              hints: dict[str, dict] | None = None, hard_deadline: float | None = None,
              on_progress=None, ledger: Ledger | None = None) -> list[dict]:
    """Always returns len(inputs) envelopes in input order, even past the hard deadline. Once a timeout or
    isolation envelope is issued for a company its worker is sealed: it can no longer write state."""
    hints = hints or {}
    ex = ThreadPoolExecutor(max_workers=workers)
    resumed = {raw: ledger.get(raw) for raw in inputs} if ledger else {}
    futs = [None if resumed.get(raw) else ex.submit(agent.run_one, raw, hints.get(normalise(raw))) for raw in inputs]
    out = []
    for i, (raw, fu) in enumerate(zip(inputs, futs), 1):
        if fu is None:
            out.append(resumed[raw])
            if on_progress and (i % 10 == 0 or i == len(inputs)):
                on_progress(i, len(inputs))
            continue
        limit = company_timeout
        if hard_deadline is not None:
            limit = max(0.01, min(company_timeout, hard_deadline - time.monotonic()))
        env = None
        try:
            env = fu.result(timeout=limit)
        except FutTimeout:
            if agent.seal(raw):
                fu.cancel()
                env = agent.timeout_envelope(raw, limit)
            else:  # worker committed in the instant the timer fired: its envelope matches the saved state
                try:
                    env = fu.result(timeout=30)
                except Exception as e:  # noqa: BLE001
                    env = agent._terminal(raw, type(e).__name__, f"isolated failure: {str(e)[:200]}", is_valid(raw))
        except Exception as e:  # noqa: BLE001
            agent.seal(raw)
            env = agent._terminal(raw, type(e).__name__, f"isolated failure: {str(e)[:200]}", is_valid(raw))
        if not env:  # worker was cancelled before the runner sealed it (should not happen); never emit nothing
            env = agent._terminal(raw, "Cancelled", "worker cancelled before completion", is_valid(raw))
        out.append(env)
        if ledger:
            ledger.record(env)
        if on_progress and (i % 10 == 0 or i == len(inputs)):
            on_progress(i, len(inputs))
    ex.shutdown(wait=False, cancel_futures=True)
    return out
