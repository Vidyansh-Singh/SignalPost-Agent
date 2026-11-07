"""Polite, cached, robots-respecting HTTP fetcher (stdlib only). Failures are values."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .core import utcnow

# Documented open-data / machine APIs. robots.txt governs crawling of web pages; these hosts publish APIs meant for
# programmatic access under open terms (Brreg: NLOD; GLEIF: CC0; Norid RDAP: protocol service), so a missing or
# failing robots.txt must not switch the registry off. Company websites and Wikidata always obey robots.txt.
OPEN_API_HOSTS = frozenset({"data.brreg.no", "api.gleif.org", "rdap.norid.no"})
UA = "SignalpostResearchBot/1.0 (evidence-backed company profiles; respects robots.txt)"
MAX_BYTES = 3_000_000
ROBOTS_MAX_BYTES = 512_000
READ_DEADLINE_S = 60.0   # wall-clock cap per response, so a slow-drip server cannot hold a worker


class ResponseTooLarge(Exception):
    pass


def read_limited(resp, limit: int, deadline_s: float = READ_DEADLINE_S) -> bytes:
    """Read at most `limit` bytes. Raises ResponseTooLarge instead of silently truncating (a truncated document
    would be hashed and parsed as if complete), and OSError if the body takes longer than `deadline_s`."""
    declared = resp.headers.get("content-length") if getattr(resp, "headers", None) is not None else None
    try:
        if declared is not None and int(declared) > limit:
            raise ResponseTooLarge(f"content-length {declared} > {limit}")
    except ValueError:
        pass
    end, chunks, total = time.monotonic() + deadline_s, [], 0
    while True:
        chunk = resp.read(min(65_536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(f"body exceeds {limit} bytes")
        chunks.append(chunk)
        if time.monotonic() > end:
            raise OSError("read_deadline_exceeded")


def _forbidden_ip(raw: str) -> bool:
    """True for any address that is not a plain public unicast address."""
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return (not ip.is_global) or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local


def unsafe_reason(url: str) -> str | None:
    """Outbound URL policy (SSRF guard). URLs come from untrusted data (registry fields, web pages), so we only
    fetch public http(s) hosts on standard ports, never IP literals, localhost/internal names, or hosts that
    resolve to non-public addresses. This is the early, readable check; the connection itself is re-checked in
    `_safe_create_connection`, which closes the DNS-rebinding gap between this lookup and the connect."""
    pr = urlparse(url or "")
    host = (pr.hostname or "").lower()
    if pr.scheme not in ("http", "https") or not host:
        return "bad_scheme_or_host"
    if pr.username or pr.password:
        return "credentials_in_url"
    try:
        if pr.port not in (None, 80, 443):
            return "non_standard_port"
    except ValueError:
        return "bad_port"
    try:
        ipaddress.ip_address(host)
        return "ip_literal"
    except ValueError:
        pass
    if "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp")):
        return "non_public_hostname"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None  # unresolvable: the normal fetch path will report it as a dead domain
    for info in infos:
        if _forbidden_ip(info[4][0]):
            return "private_address"
    return None


def _proxy_hosts() -> set[str]:
    try:
        return {(urlparse(v if "//" in v else "//" + v).hostname or "").lower() for v in urllib.request.getproxies().values()}
    except Exception:  # noqa: BLE001
        return set()


def _safe_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once, validate every address, then connect to the validated address itself. Nothing is sent to a
    non-public peer and a second DNS answer cannot swap in a different one (DNS rebinding)."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if str(host).lower() not in _proxy_hosts():
        for info in infos:
            if _forbidden_ip(info[4][0]):
                raise OSError(f"unsafe_address:{info[4][0]}")
    err: OSError | None = None
    for fam, typ, proto, _, sa in infos:
        sock = None
        try:
            sock = socket.socket(fam, typ, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
    raise err or OSError("no_address")


class _SafeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _safe_create_connection


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _safe_create_connection


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = unsafe_reason(newurl)
        if why:
            raise urllib.error.URLError(f"unsafe_redirect:{why}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    ok: bool = False
    body: bytes = b""
    text: str = ""
    content_type: str = ""
    content_sha256: str = ""
    error: str | None = None
    blocked_by_robots: bool = False
    retrieved_at: str = field(default_factory=utcnow)
    from_cache: bool = False

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "").lower()


class Fetcher:
    def __init__(self, cache_dir: str | Path | None = None, *, timeout: float = 15.0,
                 delay: float = 1.0, obey_robots: bool = True, retries: int = 2,
                 api_hosts: frozenset[str] = OPEN_API_HOSTS):
        # private opener: nothing here touches urllib's process-global opener, so other code in the process is unaffected
        self._opener = urllib.request.build_opener(_SafeRedirect, _SafeHTTPHandler, _SafeHTTPSHandler)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout, self.delay = timeout, delay
        # open government API tolerates fast access; third-party and company hosts stay polite
        self.host_delay = {"data.brreg.no": min(delay, 0.05), "api.gleif.org": min(delay, 0.3)}
        self.obey_robots, self.retries, self.api_hosts = obey_robots, retries, frozenset(api_hosts)
        self.robots_detail: dict[str, str] = {}   # host -> why robots.txt was judged unreachable/denied (diagnostics)
        self.requests = self.cache_hits = self.robots_denied = 0
        self.hosts: set[str] = set()
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[str, RobotFileParser | None, float]] = {}  # host -> (decision, parser, expires)

    def _open(self, req, timeout):
        return self._opener.open(req, timeout=timeout)

    # ---- cache ----
    def _cp(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _cache_get(self, url: str, ttl: int) -> FetchResult | None:
        p = self._cp(url)
        if ttl <= 0 or not p or not p.exists():
            return None
        try:
            d = json.loads(p.read_text("utf-8"))
            if time.time() - d.pop("_t", 0) > ttl:
                return None
            body = bytes.fromhex(d.pop("body_hex", ""))
            names = {f.name for f in fields(FetchResult)}
            r = FetchResult(**{k: v for k, v in d.items() if k in names and k not in ("body", "text", "from_cache")})
            r.body, r.text, r.from_cache = body, body.decode("utf-8", "replace"), True
            return r
        except Exception:
            return None

    def _cache_put(self, r: FetchResult) -> None:
        p = self._cp(r.url)
        if not p:
            return
        d = {f.name: getattr(r, f.name) for f in fields(r)
             if f.name not in ("body", "text", "from_cache")}
        d["body_hex"], d["_t"] = r.body.hex(), time.time()
        try:
            p.write_text(json.dumps(d), "utf-8")
        except Exception:
            pass

    # ---- politeness ----
    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait(self, host: str) -> None:
        with self._host_lock(host):
            gap = self.host_delay.get(host, self.delay) - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            self._last[host] = time.monotonic()

    ROBOTS_RETRY_S = 300.0

    def _robots_decision(self, scheme: str, host: str) -> tuple[str, RobotFileParser | None, float]:
        """RFC 9309 handling: 200 -> parse; 404/410 and other 4xx -> allow; 401/403 -> disallow; 5xx, 429,
        timeouts and network errors -> treat as unreachable, i.e. disallow, and retry after ROBOTS_RETRY_S.
        A host that does not resolve or refuses connections is 'dead': nothing can be fetched from it anyway."""
        try:
            self._wait(host)
            self.requests += 1
            req = urllib.request.Request(f"{scheme}://{host}/robots.txt", headers={"User-Agent": UA})
            with self._open(req, 8) as resp:
                rp = RobotFileParser()
                raw = resp.read(ROBOTS_MAX_BYTES)  # RFC 9309: rules beyond the first 500 KiB may be ignored
                rp.parse(raw.decode("utf-8", "replace").splitlines())
                return "rules", rp, float("inf")
        except urllib.error.HTTPError as e:
            self.robots_detail[host] = f"HTTP {e.code}"
            if e.code in (401, 403):
                return "deny", None, float("inf")
            if e.code == 429 or e.code >= 500:
                return "unreachable", None, time.monotonic() + self.ROBOTS_RETRY_S
            return "allow", None, float("inf")
        except Exception as e:  # noqa: BLE001
            reason = getattr(e, "reason", e)
            self.robots_detail[host] = f"{type(e).__name__}: {str(reason)[:120]}"
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)) or "certificate" in str(reason).lower() \
                    or "unsafe" in str(reason).lower():
                return "dead", None, float("inf")
            return "unreachable", None, time.monotonic() + self.ROBOTS_RETRY_S

    def _robots_for(self, url: str) -> tuple[str, bool]:
        """Returns (decision, allowed)."""
        if not self.obey_robots:
            return "allow", True
        pr = urlparse(url)
        host = pr.netloc
        if (pr.hostname or "").lower() in self.api_hosts:
            return "api", True
        with self._host_lock(host + "#robots"):
            with self._lock:
                cur = self._robots.get(host)
            if cur is None or time.monotonic() >= cur[2]:
                cur = self._robots_decision(pr.scheme, host)
                with self._lock:
                    self._robots[host] = cur
        kind, rp, _ = cur
        if kind == "rules":
            return kind, rp.can_fetch(UA, url)
        return kind, kind == "allow"

    def _allowed(self, url: str) -> bool:
        return self._robots_for(url)[1]

    # ---- public ----
    def get(self, url: str, *, ttl: int = 0, accept: str | None = None) -> FetchResult:
        if not url or not url.startswith(("http://", "https://")):
            return FetchResult(url=url or "", error="invalid_url")
        why = unsafe_reason(url)
        if why:
            return FetchResult(url=url, error=f"unsafe_url:{why}")
        hit = self._cache_get(url, ttl)
        if hit:
            self.cache_hits += 1
            return hit
        kind, allowed = self._robots_for(url)
        if not allowed:
            if kind == "dead":
                return FetchResult(url=url, error=f"dead_host:{self.robots_detail.get(urlparse(url).netloc, '?')}")
            self.robots_denied += 1
            if kind == "unreachable":  # cannot prove permission: fail closed, retry after the negative-cache TTL
                return FetchResult(url=url, error=f"robots_unreachable:{self.robots_detail.get(urlparse(url).netloc, '?')}",
                                   blocked_by_robots=True)
            return FetchResult(url=url, error="robots_disallow", blocked_by_robots=True)
        host = urlparse(url).netloc
        self.hosts.add(host)
        last = FetchResult(url=url, error="unattempted")
        for attempt in range(self.retries + 1):
            self._wait(host)
            self.requests += 1
            headers = {"User-Agent": UA, "Accept-Language": "nb,no,en;q=0.7"}
            if accept:
                headers["Accept"] = accept
            try:
                req = urllib.request.Request(url, headers=headers)
                with self._open(req, self.timeout) as resp:
                    body = read_limited(resp, MAX_BYTES)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    out = FetchResult(
                        url=url, final_url=resp.geturl(), status=resp.status, ok=True,
                        body=body, text=body.decode(charset, "replace"),
                        content_type=resp.headers.get("content-type", ""),
                        content_sha256=hashlib.sha256(body).hexdigest())
                    self._cache_put(out) if ttl > 0 else None
                    return out
            except ResponseTooLarge:
                return FetchResult(url=url, error="response_too_large")  # never publish from a truncated body
            except urllib.error.HTTPError as e:
                last = FetchResult(url=url, status=e.code, error=f"http_{e.code}")
                if e.code not in (429, 500, 502, 503, 504):
                    return last
            except Exception as e:  # noqa: BLE001 - failures are values
                reason = getattr(e, "reason", e)
                last = FetchResult(url=url, error=type(e).__name__)
                if isinstance(reason, (socket.gaierror, ConnectionRefusedError)) or "certificate" in str(reason).lower():
                    return last  # dead/unresolvable host: retrying only burns the time budget
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        return last

    def get_json(self, url: str, *, ttl: int = 0):
        r = self.get(url, ttl=ttl, accept="application/json")
        if not r.ok:
            return None, r
        try:
            return json.loads(r.text), r
        except Exception:
            r.error, r.ok = "json_decode_error", False
            return None, r
