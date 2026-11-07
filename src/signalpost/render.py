"""Static, dependency-free viewer: searchable index + one page per company."""
from __future__ import annotations

import html
import json
from pathlib import Path

E = html.escape
CSS = ("*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:#10151c;background:#f6f7f9}"
       "header{background:#0f172a;color:#fff;padding:16px 20px}header a{color:#9fd0ff}main{max-width:1040px;margin:0 auto;padding:16px}"
       ".card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px;margin:0 0 16px}"
       "table{width:100%;border-collapse:collapse;font-size:.92rem}th,td{text-align:left;padding:8px;border-bottom:1px solid #eef0f3;"
       "vertical-align:top;word-break:break-word}.pill{padding:1px 8px;border-radius:99px;font-size:.75rem;font-weight:600;background:#e2e8f0}"
       ".available{background:#dcfce7}.blocked,.failed{background:#fee2e2}.ambiguous{background:#fef3c7}"
       "input,select{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:1rem}small{color:#64748b}"
       ".wrap{overflow-x:auto}details{margin-top:4px}summary{cursor:pointer;color:#1d4ed8;font-size:.8rem}"
       ".cand{background:#fffbeb;border-left:3px solid #f59e0b;padding:4px 8px;margin-top:4px;font-size:.85rem}"
       ".filters{display:grid;gap:8px;grid-template-columns:1fr 200px}"
       "@media(max-width:640px){main{padding:8px}.filters{grid-template-columns:1fr}table,tbody,tr,td{display:block;width:100%}"
       "tr:first-child{display:none}tr{border:1px solid #e3e6ea;border-radius:8px;margin:0 0 10px;padding:4px}td{border:0}}")


def _href(u) -> str:
    u = str(u or "")
    return E(u) if u.startswith(("http://", "https://")) else "#"


def _v(v) -> str:
    return E(json.dumps(v, ensure_ascii=False)[:300] if isinstance(v, (dict, list)) else str(v)[:300]) if v is not None else "<small>-</small>"


def _evidence(e: dict) -> str:
    return (f'<details><summary>evidence</summary><small>'
            f'<a href="{_href(e["source_url"])}" rel="noopener noreferrer" target="_blank">{E(e["source_url"])}</a><br>'
            f'retrieved {E(e.get("retrieved_at", ""))} &middot; sha256 {E(str(e.get("content_sha256", ""))[:16])}&hellip;<br>'
            f'{E(e.get("source_class", ""))} &middot; selector {E(str(e.get("selector") or "-"))} &middot; {E(str(e.get("extractor") or "-"))}<br>'
            f'{"published " + E(e["publication_date"]) + " &middot; " if e.get("publication_date") else ""}'
            f'{"period " + E(e["reporting_period"]) + " &middot; " if e.get("reporting_period") else ""}'
            f'span: {E(e.get("claim_span", "")[:200])}</small></details>')


def _revenue_table(env: dict) -> str:
    h = next((c for c in env["claims"] if c["field"] == "history" and c["availability"] == "available"), None)
    if not h or not isinstance(h["value"], list):
        return ""
    unit = h.get("unit") or "NOK"
    rows, prev = "", None
    for x in h["value"]:
        r = x.get("revenue")
        d = f"{(r - prev) / abs(prev) * 100:+.1f}%" if isinstance(r, (int, float)) and isinstance(prev, (int, float)) and prev else "-"
        rows += f"<tr><td>{E(str(x.get('period')))}</td><td>{E(f'{int(r):,}'.replace(',', ' ')) if isinstance(r, (int, float)) else '-'} {E(unit)}</td><td>{d}</td></tr>"
        prev = r if isinstance(r, (int, float)) else prev
    return f'<h2>Revenue by year (same-length filings, oldest first)</h2><div class="wrap"><table><tr><th>Period</th><th>Operating revenue</th><th>Change</th></tr>{rows}</table></div>'


def company_page(env: dict) -> str:
    ev = {e["id"]: e for e in env["evidence"]}
    rows = []
    for c in sorted(env["claims"], key=lambda x: (x["field_family"], x["field"], x["claim_key"])):
        ok = c["availability"] == "available"
        evid = "".join(_evidence(ev[i]) for i in c["evidence_ids"] if i in ev)
        cands = "".join(f'<div class="cand">unverified candidate (not published as fact): {_v(a)}</div>' for a in c.get("alternatives") or [])
        link = c.get("entity_link") or {}
        rows.append(f'<tr data-state="{E(c["availability"])}"><td><small>{E(c["field_family"])}</small><br><b>{E(c["field"])}</b></td>'
                    f'<td>{_v(c["value"]) if ok else "<small>not published as a fact</small>"}'
                    f'{"<br><small>" + E(c["note"]) + "</small>" if c.get("note") else ""}{cands}</td>'
                    f'<td><span class="pill {E(c["availability"])}">{E(c["availability"])}</span>'
                    f'{"<br><small>identity: " + E(str(link.get("method"))) + "</small>" if ok and link else ""}</td>'
                    f"<td>{evid or '<small>no evidence (nothing published)</small>'}</td></tr>")
    s = env["synthesis"]
    ch = "".join(f"<li>{E(x)}</li>" for x in s["material_changes"])
    chg = env.get("changes") or []
    hist = "".join(f"<li>{E(x['change_type'])} {E(str(x.get('field')))}: {_v(x.get('previous_value'))} &rarr; {_v(x.get('current_value'))} "
                   f"<small>({E(x.get('materiality', ''))}, {E(x.get('observed_at', ''))})</small></li>" for x in chg[:50])
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{E(s["headline"])}</title><style>{CSS}</style><header><a href="index.html">&larr; All companies</a></header><main>'
            f'<div class="card"><h1>{E(s["headline"])}</h1><small>{E(env["run"]["terminal_status"])} &middot; refreshed {E(env["refresh"].get("last_refreshed_at", ""))}</small>'
            f'<h2>Summary</h2><ul>{"".join(f"<li>{E(x)}</li>" for x in s["summary"])}</ul><h2>Material changes</h2><ul>{ch}</ul>'
            f'{_revenue_table(env)}'
            f'{"<h2>Change history</h2><ul>" + hist + "</ul>" if hist else ""}'
            f'<h2>Not available</h2><ul>{"".join(f"<li>{E(x)}</li>" for x in s["unknowns"]) or "<li>None</li>"}</ul></div>'
            f'<div class="card"><div class="filters"><input type="search" id="q" placeholder="Filter fields"><select id="st">'
            f'<option value="">All states</option>{"".join(f"<option>{x}</option>" for x in ("available", "not_available", "ambiguous", "blocked", "failed", "not_applicable"))}</select></div>'
            f'<div class="wrap"><table><tr><th>Field</th><th>Value</th><th>State</th><th>Evidence</th></tr>{"".join(rows)}</table></div></div></main>'
            "<script>(function(){var q=document.getElementById('q'),s=document.getElementById('st');function f(){var v=q.value.toLowerCase();"
            "document.querySelectorAll('tr[data-state]').forEach(function(r){r.style.display=(!s.value||r.dataset.state===s.value)&&r.textContent.toLowerCase().indexOf(v)>-1?'':'none'})}"
            "q.addEventListener('input',f);s.addEventListener('change',f)})()</script></html>")


def _facts(e: dict) -> dict:
    """Compact, published-only facts for the index and the compare view (no unverified candidates)."""
    got: dict = {}
    for c in e["claims"]:
        if c["availability"] == "available":
            got.setdefault(c["field"], c)
    hist = (got.get("history") or {}).get("value") or []
    news = [c for c in e["claims"] if c["field"] == "news_item" and c["availability"] == "available"]
    soc = sorted(c["field"].replace("_url", "") for c in e["claims"] if c["field_family"] == "social_profiles" and c["availability"] == "available")
    val = lambda k: (got.get(k) or {}).get("value")
    return {"revenue": val("latest_revenue"), "period": val("latest_period"), "profit": val("latest_profit_for_year"),
            "employees": val("registered_employees"), "form": val("organisation_form"), "website": val("official_website"),
            "social": soc, "news": len(news), "newest": max((c["value"]["date_published"] for c in news), default=None),
            "history": [[h.get("period"), h.get("revenue")] for h in hist if isinstance(h, dict)][-3:]}


def render(envs: list[dict], out: str | Path) -> int:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows, data = [], {}
    for e in envs:
        org = e["organisation_number"]
        (out / f"{org}.html").write_text(company_page(e), "utf-8")
        name = e["legal_identity"].get("legal_name") or org
        fx = _facts(e)
        data[org] = {"name": name, **fx}
        cov = e["coverage"]
        rows.append(f'<tr data-q="{E(name.lower())} {E(org)}" data-st="{E(e["run"]["terminal_status"])}" data-n="{E(name.lower())}" '
                    f'data-f="{cov["families_with_data"]}" data-c="{cov["claims_available"]}" data-w="{1 if fx["website"] else 0}" data-r="{fx["revenue"] or 0}">'
                    f'<td><input type="checkbox" class="cmp" value="{E(org)}" aria-label="compare {E(name)}"></td>'
                    f'<td><a href="{E(org)}.html">{E(name)}</a><br><small>{E(org)}</small></td>'
                    f'<td><span class="pill">{E(e["run"]["terminal_status"])}</span></td>'
                    f'<td>{cov["families_with_data"]}/{cov["families_expected"]}</td><td>{cov["claims_available"]}</td></tr>')
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Signalpost</title><style>{CSS}.bar{{display:grid;gap:8px;grid-template-columns:1fr 160px 180px}}'
        f'#cmpbox table{{margin-top:8px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#1d4ed8;color:#fff;font-size:1rem}}'
        f'@media(max-width:640px){{.bar{{grid-template-columns:1fr}}}}</style>'
        f'<header><b>Signalpost</b> - evidence-backed Norwegian company profiles</header><main>'
        f'<div class="card"><div class="bar"><input type="search" id="q" placeholder="Search name or organisation number" aria-label="search">'
        f'<select id="st" aria-label="filter by run state"><option value="">All run states</option><option>completed</option><option>partial</option><option>failed</option></select>'
        f'<select id="so" aria-label="sort"><option value="n">Sort: name</option><option value="f">Sort: most families</option>'
        f'<option value="c">Sort: most claims</option><option value="w">Sort: has website first</option><option value="r">Sort: highest revenue</option></select></div>'
        f'<p><small><span id="cnt">{len(envs)}</span> of {len(envs)} profiles &middot; tick two companies to compare</small> '
        f'<button id="go" type="button">Compare selected</button></p><div id="cmpbox" class="wrap"></div>'
        f'<div class="wrap"><table id="t"><tr><th></th><th>Company</th><th>Run</th><th>Families</th><th>Claims</th></tr>{"".join(rows)}</table></div></div></main>'
        f'<script id="d" type="application/json">{js}</script><script>(function(){{var D=JSON.parse(document.getElementById("d").textContent);'
        "var t=document.getElementById('t'),q=document.getElementById('q'),st=document.getElementById('st'),so=document.getElementById('so'),cnt=document.getElementById('cnt');"
        "function rows(){return Array.prototype.slice.call(t.querySelectorAll('tr[data-q]'))}"
        "function flt(){var v=q.value.toLowerCase(),n=0;rows().forEach(function(r){var ok=r.dataset.q.indexOf(v)>-1&&(!st.value||r.dataset.st===st.value);r.style.display=ok?'':'none';if(ok)n++});cnt.textContent=n}"
        "function srt(){var k=so.value,rs=rows();rs.sort(function(a,b){return k==='n'?(a.dataset.n<b.dataset.n?-1:1):(+b.dataset[k]-+a.dataset[k])});rs.forEach(function(r){t.appendChild(r.parentNode===t||1?r:r)})}"
        "q.addEventListener('input',flt);st.addEventListener('change',flt);so.addEventListener('change',srt);"
        "function esc(x){return String(x==null?'-':x).replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]})}"
        "function num(x){return x==null?'-':String(Math.round(x)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')+' NOK'}"
        "document.getElementById('go').addEventListener('click',function(){var sel=Array.prototype.slice.call(document.querySelectorAll('.cmp:checked')).map(function(c){return c.value}).slice(0,2),b=document.getElementById('cmpbox');"
        "if(sel.length!==2){b.innerHTML='<small>Tick exactly two companies.</small>';return}var A=D[sel[0]],B=D[sel[1]];"
        "function hist(x){return x.history.length?x.history.map(function(h){return esc(h[0])+': '+num(h[1])}).join('<br>'):'-'}"
        "var R=[['Legal form','form'],['Employees (register)','employees'],['Latest revenue',null,function(x){return num(x.revenue)}],['Latest period','period'],"
        "['Profit for the year',null,function(x){return num(x.profit)}],['Revenue history',null,hist],['Website',null,function(x){return x.website?'<a href=\"'+esc(x.website)+'\" rel=\"noopener noreferrer\">'+esc(x.website)+'</a>':'not verified'}],"
        "['Social accounts',null,function(x){return x.social.length?esc(x.social.join(', ')):'-'}],['Dated news items',null,function(x){return x.news+(x.newest?' (newest '+esc(x.newest)+')':'')}]];"
        "var h='<table><tr><th></th><th><a href=\"'+sel[0]+'.html\">'+esc(A.name)+'</a></th><th><a href=\"'+sel[1]+'.html\">'+esc(B.name)+'</a></th></tr>';"
        "R.forEach(function(r){var f=r[2]||function(x){return esc(x[r[1]])};h+='<tr><td><b>'+r[0]+'</b></td><td>'+f(A)+'</td><td>'+f(B)+'</td></tr>'});"
        "b.innerHTML=h+'</table><small>Each value links to its evidence on the company page.</small>'});srt()}})()</script></html>", "utf-8")
    return len(envs)
