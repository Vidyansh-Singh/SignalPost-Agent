"""Output contract validator. Pure functions, standard library only.

Covers the starter kit's OUTPUT_CONTRACT.md (required keys and allowed availability states), the evaluation
contract's official-run checks (source, retrieval time, reporting period; missing never turned into zero; one
envelope per input) and this agent's own invariants. Every issue is a human-readable string; [] means valid.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import STATES, is_valid

TERMINAL = {"completed", "partial", "failed"}
SOURCE_CLASSES = {"official_registry", "open_data_api", "company_owned"}
CHANGE_TYPES = {"added", "changed", "removed"}
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"^[0-9a-f]{64}$")
TOP = {"organisation_number": str, "input_valid": bool, "legal_identity": dict, "claims": list, "evidence": list,
       "changes": list, "errors": list, "synthesis": dict, "refresh": dict, "coverage": dict,
       "operations": dict, "run": dict}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _static_url_ok(url: Any) -> bool:
    """Static (no DNS) check that an evidence URL is a public-looking http(s) URL."""
    if not isinstance(url, str):
        return False
    pr = urlparse(url)
    host = (pr.hostname or "").lower()
    if pr.scheme not in ("http", "https") or not host or "." not in host:
        return False
    return not re.fullmatch(r"[\d.]+|[0-9a-f:]+", host) and not host.endswith((".local", ".localhost", ".internal"))


def validate(env: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(env, dict):
        return ["envelope is not a JSON object"]
    for k, t in TOP.items():
        if k not in env:
            out.append(f"official: missing top-level key {k!r}")
        elif not isinstance(env[k], t):
            out.append(f"official: {k!r} must be {t.__name__}")
    if out:
        return out  # structure too broken to inspect further

    org, claims, evidence = env["organisation_number"], env["claims"], env["evidence"]
    if env["input_valid"] and not is_valid(org):
        out.append("input_valid is true but organisation number fails the checksum")

    run = env["run"]
    for k in ("run_id", "agent_version"):
        if not isinstance(run.get(k), str) or not run.get(k):
            out.append(f"official: run.{k} missing")
    for k in ("started_at", "completed_at"):
        if not isinstance(run.get(k), str) or not TS.match(run.get(k, "")):
            out.append(f"official: run.{k} is not an ISO-8601 UTC timestamp")
    if isinstance(run.get("started_at"), str) and isinstance(run.get("completed_at"), str) \
            and TS.match(run["started_at"]) and TS.match(run["completed_at"]) and run["completed_at"] < run["started_at"]:
        out.append("run.completed_at precedes run.started_at")
    if run.get("terminal_status") not in TERMINAL:
        out.append(f"official: run.terminal_status {run.get('terminal_status')!r} not in {sorted(TERMINAL)}")

    ops = env["operations"]
    for k in ("requests", "runtime_ms"):
        if not isinstance(ops.get(k), int) or isinstance(ops.get(k), bool) or ops[k] < 0:
            out.append(f"official: operations.{k} must be a non-negative integer")
    if not _num(ops.get("third_party_cost_usd")) or ops["third_party_cost_usd"] < 0:
        out.append("official: operations.third_party_cost_usd must be a non-negative number")

    ev: dict[str, dict] = {}
    for i, e in enumerate(evidence):
        if not isinstance(e, dict):
            out.append(f"evidence[{i}] is not an object")
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            out.append(f"evidence[{i}] has no id")
            continue
        if eid in ev:
            out.append(f"duplicate evidence id {eid}")
        ev[eid] = e
        if not _static_url_ok(e.get("source_url")):
            out.append(f"{eid}: source_url is not a public http(s) URL")
        if e.get("source_class") not in SOURCE_CLASSES:
            out.append(f"{eid}: source_class {e.get('source_class')!r} not allowed")
        if not isinstance(e.get("retrieved_at"), str) or not TS.match(e["retrieved_at"]):
            out.append(f"{eid}: retrieved_at is not an ISO-8601 UTC timestamp")
        if not isinstance(e.get("content_sha256"), str) or not SHA.match(e["content_sha256"]):
            out.append(f"{eid}: content_sha256 is not a 64-hex SHA-256")
        if not isinstance(e.get("claim_span"), str) or not e["claim_span"].strip():
            out.append(f"{eid}: empty claim_span")
        if not isinstance(e.get("source_policy"), str) or not e["source_policy"].strip():
            out.append(f"{eid}: missing source_policy")
        for k in ("publication_date", "reporting_period"):
            if e.get(k) is not None and not (isinstance(e[k], str) and DATE.match(e[k])):
                out.append(f"{eid}: {k} is not a date")

    seen: set[str] = set()
    referenced: set[str] = set()
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            out.append(f"claims[{i}] is not an object")
            continue
        key = c.get("claim_key")
        if not isinstance(key, str) or not key:
            out.append(f"claims[{i}] has no claim_key")
            continue
        if key in seen:
            out.append(f"duplicate claim_key {key}")
        seen.add(key)
        if isinstance(org, str) and not key.startswith(org + "|"):
            out.append(f"{key}: claim_key does not start with the organisation number")
        for k in ("field_family", "field"):
            if not isinstance(c.get(k), str) or not c[k]:
                out.append(f"{key}: missing {k}")
        st = c.get("availability")
        if st not in STATES:
            out.append(f"{key}: availability {st!r} not allowed")
            continue
        if not _num(c.get("confidence")) or not 0 <= c["confidence"] <= 1:
            out.append(f"{key}: confidence must be a number in [0, 1]")
        ids = c.get("evidence_ids")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            out.append(f"{key}: evidence_ids must be a list of strings")
            ids = []
        referenced.update(ids)
        for x in ids:
            if x not in ev:
                out.append(f"{key}: dangling evidence {x}")
        if st == "available":
            if c.get("value") in (None, "", [], {}):
                out.append(f"{key}: available with empty value")
            if not ids:
                out.append(f"{key}: available without evidence")
            link = c.get("entity_link")
            if not isinstance(link, dict):
                out.append(f"{key}: available without entity link")
            else:
                if link.get("organisation_number") != org:
                    out.append(f"{key}: entity_link points at a different organisation number")
                if not link.get("method") or not _num(link.get("score")) or not 0 <= link["score"] <= 1:
                    out.append(f"{key}: incomplete entity_link")
            if not env["input_valid"]:
                out.append(f"{key}: available claim on an invalid input")
            fam, val = c.get("field_family"), c.get("value")
            spans = " ".join(ev[x].get("claim_span", "") for x in ids if x in ev)
            if fam == "financials" and c.get("field") == "history":
                h = val if isinstance(val, list) else []
                ends = [x.get("period") for x in h if isinstance(x, dict)]
                if len(h) < 2 or ends != sorted(ends) or len(set(ends)) != len(ends):
                    out.append(f"{key}: history must list at least two distinct periods, oldest first")
                for x in h:
                    if not isinstance(x, dict) or x.get("months") != 12 or any(
                            isinstance(x.get(k), bool) for k in x):
                        out.append(f"{key}: history entry is not a like-for-like 12-month period")
                        break
                if any(p_ and p_ not in spans for p_ in ends):
                    out.append(f"{key}: history period not present in its evidence span")
            elif fam == "financials":
                if c.get("field") != "latest_period":
                    if not _num(val):
                        out.append(f"{key}: financial value is not numeric")
                    elif str(val) not in spans:
                        out.append(f"{key}: financial value {val} is not present in its evidence span")
                    if not c.get("unit"):
                        out.append(f"{key}: financial claim without unit")
                if not any(ev[x].get("reporting_period") for x in ids if x in ev):
                    out.append(f"{key}: financial claim without reporting period")
            if c.get("field") == "news_item":
                d = val.get("date_published") if isinstance(val, dict) else None
                if not (isinstance(d, str) and DATE.match(d)) or not any(ev[x].get("publication_date") == d for x in ids if x in ev):
                    out.append(f"{key}: news item without a matching publication date")
        else:
            if _num(c.get("value")):
                out.append(f"{key}: absence rendered as a number")
            if not isinstance(c.get("note"), str) or not c["note"].strip():
                out.append(f"{key}: {st} without an explanatory note")
    for eid in ev:
        if eid not in referenced:
            out.append(f"{eid}: evidence not referenced by any claim")

    for i, ch in enumerate(env["changes"]):
        if not isinstance(ch, dict) or ch.get("change_type") not in CHANGE_TYPES \
                or ch.get("materiality") not in ("material", "minor") or not ch.get("claim_key") or not ch.get("observed_at"):
            out.append(f"changes[{i}] is malformed")
    for i, er in enumerate(env["errors"]):
        if not isinstance(er, dict) or not er.get("stage") or not er.get("kind") or not isinstance(er.get("message"), str):
            out.append(f"errors[{i}] is malformed")

    syn = env["synthesis"]
    if not isinstance(syn.get("headline"), str) or not syn.get("headline"):
        out.append("synthesis.headline missing")
    if not isinstance(syn.get("summary"), list) or not syn["summary"] or any(not isinstance(x, str) for x in syn["summary"]):
        out.append("synthesis.summary must be a non-empty list of strings")
    for k in ("material_changes", "unknowns", "grounded_claim_keys"):
        if not isinstance(syn.get(k), list):
            out.append(f"synthesis.{k} must be a list")
    for k in syn.get("grounded_claim_keys") or []:
        if k not in seen:
            out.append(f"synthesis references unknown claim {k}")

    if not env["refresh"].get("last_refreshed_at"):
        out.append("refresh.last_refreshed_at missing")
    cov = env["coverage"]
    n_avail = sum(1 for c in claims if isinstance(c, dict) and c.get("availability") == "available")
    if cov.get("claims_total") != len(claims) or cov.get("claims_available") != n_avail:
        out.append("coverage counts do not match the claims list")
    return out


def check_file(path: str | Path) -> tuple[int, list[str]]:
    """Validate an envelope JSONL file. Never raises on malformed content: problems become issues."""
    issues: list[str] = []
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return 0, [f"cannot read {path}: {e.strerror or type(e).__name__}"]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return 0, [f"file is not valid UTF-8 ({e.reason} at byte {e.start})"]
    seen: set[str] = set()
    n = 0
    for ln, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        n += 1
        try:
            env = json.loads(line)
        except json.JSONDecodeError as e:
            issues.append(f"line {ln}: invalid JSON ({e.msg} at column {e.colno})")
            continue
        org = env.get("organisation_number") if isinstance(env, dict) else None
        if isinstance(org, str):
            if org in seen:
                issues.append(f"line {ln}: duplicate envelope for {org}")
            seen.add(org)
        issues += [f"line {ln} ({org}): {m}" for m in validate(env)]
    if n == 0:
        issues.append("file contains no envelopes")
    return n, issues


def _norm(t: Any) -> str:
    return " ".join(str(t).lower().replace(",", " ").split())


def measure(path: str | Path, inputs: str | Path | None = None) -> dict:
    """Mechanical quality metrics for an envelope file. These are LOCAL checks of internal consistency, not an
    official Builderr score and not a measure of factual correctness against ground truth."""
    envs, bad_lines, raw_n = [], 0, 0
    try:
        text = Path(path).read_bytes().decode("utf-8-sig", "replace")
    except OSError as e:
        return {"publication_ready": False, "error": f"cannot read {path}: {e.strerror}"}
    for line in text.splitlines():
        if line.strip():
            raw_n += 1
            try:
                e = json.loads(line)
                envs.append(e) if isinstance(e, dict) else None
            except ValueError:
                bad_lines += 1
    n, _ = check_file(path)
    issues = sum(len(validate(e)) for e in envs)
    orgs = [e.get("organisation_number") for e in envs]
    avail = [(e, c) for e in envs for c in e.get("claims", []) if isinstance(c, dict) and c.get("availability") == "available"]
    ev_of = {e.get("organisation_number"): {x.get("id"): x for x in e.get("evidence", []) if isinstance(x, dict)} for e in envs}
    sup = span_ok = comp = dated_ok = dated_n = link_ok = 0
    for e, c in avail:
        evs = [ev_of[e["organisation_number"]].get(i) for i in c.get("evidence_ids", [])]
        evs = [x for x in evs if x]
        comp += bool(evs) and all(x.get("source_url") and x.get("retrieved_at") and x.get("content_sha256") and x.get("claim_span")
                                  and x.get("source_policy") and x.get("selector") for x in evs)
        span_ok += bool(evs) and all(isinstance(x.get("claim_span"), str) and x["claim_span"].strip() and x.get("claim_span") != "(no text span)" for x in evs)
        val = c.get("value")
        if isinstance(val, (str, int, float)) and not isinstance(val, bool):
            sup += any(_norm(val) in _norm(x.get("claim_span", "")) or str(val) in (x.get("source_url"), x.get("final_url")) for x in evs)
        else:
            sup += bool(evs)   # structured values: support = evidence exists with a selector (checked by `comp`)
        link = c.get("entity_link") or {}
        link_ok += link.get("organisation_number") == e.get("organisation_number")
        if c.get("field_family") == "public_activity" or (c.get("field_family") == "financials"):
            dated_n += 1
            dated_ok += any(x.get("publication_date") or x.get("reporting_period") for x in evs)
    fam: dict[str, int] = {}
    for e, c in avail:
        fam[c["field_family"]] = fam.get(c["field_family"], 0) + 1
    fam_cos = {f: len({e["organisation_number"] for e, c in avail if c["field_family"] == f}) for f in fam}
    keys_dup = sum(len(e.get("claims", [])) - len({c.get("claim_key") for c in e.get("claims", []) if isinstance(c, dict)}) for e in envs)
    ev_dup = sum(len(e.get("evidence", [])) - len({x.get("id") for x in e.get("evidence", []) if isinstance(x, dict)}) for e in envs)
    expected = None
    if inputs:
        try:
            from .pipeline import read_manifest
            expected = set(normalise_org(x) for x in read_manifest(inputs)[0])
        except Exception:  # noqa: BLE001
            expected = None
    out = {
        "envelopes": len(envs), "unparseable_lines": bad_lines, "contract_issues": issues,
        "distinct_organisations": len(set(orgs)),
        "duplicate_envelopes": len(orgs) - len(set(orgs)),
        "duplicate_claim_keys_within_envelopes": keys_dup, "duplicate_evidence_ids_within_envelopes": ev_dup,
        "available_claims": len(avail),
        "evidence_completeness_rate": round(comp / len(avail), 4) if avail else None,
        "evidence_span_rate": round(span_ok / len(avail), 4) if avail else None,
        "value_supported_by_evidence_rate": round(sup / len(avail), 4) if avail else None,
        "entity_link_matches_envelope_rate": round(link_ok / len(avail), 4) if avail else None,
        "dated_claims_with_date_rate": round(dated_ok / dated_n, 4) if dated_n else None,
        "company_coverage_by_family": {f: round(k / max(len(envs), 1), 3) for f, k in sorted(fam_cos.items())},
        "available_claims_by_family": dict(sorted(fam.items())),
        "terminal_status": {s: sum(1 for e in envs if (e.get("run") or {}).get("terminal_status") == s) for s in TERMINAL},
    }
    if expected is not None:
        out["inputs"] = len(expected)
        out["missing_inputs"] = len(expected - set(orgs))
        out["unexpected_envelopes"] = len(set(orgs) - expected)
    failed_share = out["terminal_status"].get("failed", 0) / len(envs) if envs else 1.0
    out["warnings"] = ([f"{round(failed_share * 100)}% of envelopes are 'failed': the run is contract-valid but carries almost no data; "
                        "run `python run.py diagnose`"] if failed_share > 0.5 else [])
    out["publication_ready"] = bool(envs) and issues == 0 and bad_lines == 0 and out["duplicate_envelopes"] == 0 \
        and keys_dup == 0 and ev_dup == 0 and (expected is None or (out["missing_inputs"] == 0 and out["unexpected_envelopes"] == 0)) \
        and (out["evidence_span_rate"] is None or out["evidence_span_rate"] >= 0.99) and not out["warnings"]
    return out


def normalise_org(raw: Any) -> str:
    from .core import normalise
    return normalise(raw) or str(raw)
