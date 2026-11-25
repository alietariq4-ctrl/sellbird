"""Stealth Web & Markdown Waterfall — 3-tier SSRF-guarded page fetcher.

Tier 1: curl_cffi (Chrome TLS/JA3 impersonation) or public_http_client,
        with mandatory pre-flight and per-redirect SSRF validation.
Tier 2: Local headless Playwright Chromium JS rendering when render_js is
        enabled and Tier 1 hit a bot challenge or an empty SPA shell.
Tier 3: Cloud Firecrawl v2 (/v2/scrape) when FIRECRAWL_API_KEY is configured
        and earlier tiers were blocked or backend == "firecrawl".

Clean Markdown is extracted via trafilatura and publication dates via htmldate.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from sellbird.collectors.profile import looks_like_challenge
from sellbird.public_http import _public_address, public_http_client

logger = logging.getLogger("sellbird.stealth_fetch")

_NAT64_NETS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

_SPA_ROOT_RE = re.compile(
    r"""<(?:div|main)\b[^>]*\bid=["'](?:root|app|__next|__nuxt|svelte|ankr-app)["'][^>]*>\s*</(?:div|main)>""",
    re.IGNORECASE,
)
_SPA_NOSCRIPT_RE = re.compile(
    r"""<noscript\b[^>]*>[\s\S]{0,400}?(?:need\s+to\s+enable\s+javascript|enable\s+javascript|javascript\s+is\s+required|without\s+javascript\s+enabled|doesn['’]?t\s+work\s+properly\s+without\s+javascript)""",
    re.IGNORECASE,
)
_SPA_BUNDLE_RE = re.compile(
    r"""(?:<script\b[^>]+(?:type=["']module["']|src=["'][^"']*(?:/_next/static/|/assets/index[-.][\w]+|\.chunk\.js|bundle\.js|vite)))""",
    re.IGNORECASE,
)
_SPA_MOUNT_ID_RE = re.compile(
    r"""\bid=["'](?:root|app|__next|__nuxt|svelte)["']""",
    re.IGNORECASE,
)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
FIRECRAWL_SCRAPE_COST_USD = 0.001


@dataclass
class FetchPageResult:
    """Normalized result from the 3-tier stealth web & Markdown waterfall."""

    url: str
    final_url: str
    status_code: int
    html: str
    markdown: str
    published_date: str = ""
    source: str = "http"
    challenged: bool = False
    cost_usd: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.challenged
            and 200 <= self.status_code < 400
            and bool((self.html or "").strip() or (self.markdown or "").strip())
        )


def _check_ip_string(raw_ip: str) -> None:
    """Validate an IP string via sellbird.public_http._public_address plus NAT64/mapped checks."""
    cleaned = raw_ip.split("%", 1)[0].strip("[]")
    # Delegate first to sellbird.public_http._public_address so upstream SSRF rules
    # and test monkeypatches are always honored.
    _public_address(cleaned)

    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {cleaned}") from exc

    if (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        raise ValueError(f"Address is not public: {cleaned}")

    if isinstance(addr, ipaddress.IPv6Address):
        for net in _NAT64_NETS:
            if addr in net:
                raise ValueError(f"NAT64 address is not public: {cleaned}")
        if addr.ipv4_mapped is not None:
            _check_ip_string(str(addr.ipv4_mapped))
        if addr.sixtofour is not None:
            _check_ip_string(str(addr.sixtofour))
        if addr.teredo is not None:
            _check_ip_string(str(addr.teredo[0]))
            _check_ip_string(str(addr.teredo[1]))


async def validate_public_url(url: str) -> None:
    """Validate that ``url`` uses HTTP(S) and resolves strictly to public IPs.

    Raises ``ValueError`` if the scheme, hostname, or any resolved A/AAAA record
    points to loopback, RFC1918 private networks, link-local / cloud metadata
    (169.254.169.254), IPv6 private/ULA/link-local, or NAT64 ranges.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")

    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parts.scheme!r}")
    if parts.username or parts.password:
        raise ValueError("Credentials in URL are not allowed")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError("URL has no hostname")
    if host in _LOCAL_HOSTS or any(host.endswith(suf) for suf in _LOCAL_SUFFIXES):
        raise ValueError(f"Blocked local hostname: {host}")

    # Fast-fail literal IPs before touching DNS.
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True

    if is_ip_literal:
        _check_ip_string(host)
        return

    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"DNS resolution failed for {host}: {exc}") from exc

    if not infos:
        raise ValueError(f"DNS returned no addresses for {host}")

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            raise ValueError(f"Empty sockaddr resolved for {host}")
        _check_ip_string(str(sockaddr[0]))


def _visible_body_text(html: str) -> str:
    """Extract visible body text with script/style/head/noscript/comments removed."""
    if not html:
        return ""
    body_match = re.search(r"(?is)<body\b[^>]*>([\s\S]*?)</body>", html)
    chunk = body_match.group(1) if body_match else html
    chunk = re.sub(r"(?is)<(?:script|style|noscript|svg|template|head)\b[\s\S]*?</(?:script|style|noscript|svg|template|head)>", " ", chunk)
    chunk = re.sub(r"(?s)<!--[\s\S]*?-->", " ", chunk)
    chunk = re.sub(r"(?s)<[^>]+>", " ", chunk)
    return re.sub(r"\s+", " ", chunk).strip()


def is_spa_shell(html: str) -> bool:
    """Return True if ``html`` looks like an unrendered client-side SPA shell."""
    if not html or not html.strip():
        return False
    visible = _visible_body_text(html)
    if len(visible) >= 200:
        return False
    if _SPA_ROOT_RE.search(html):
        return True
    if _SPA_NOSCRIPT_RE.search(html):
        return True
    if _SPA_MOUNT_ID_RE.search(html) and _SPA_BUNDLE_RE.search(html):
        return True
    return False


def extract_markdown(html: str, url: str = "") -> str:
    """Extract clean Markdown with links from HTML using trafilatura."""
    if not html or not html.strip():
        return ""
    try:
        import trafilatura

        md = trafilatura.extract(
            html,
            url=url or None,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
        if md and md.strip():
            return md.strip()
    except Exception as exc:
        logger.debug("trafilatura extraction failed for %s: %s", url, exc)

    # Fallback for ultra-minimal pages where readability heuristics prune everything.
    visible = _visible_body_text(html)
    return visible[:20_000] if visible else ""


def extract_published_date(html: str, url: str = "") -> str:
    """Extract ISO YYYY-MM-DD publication date from HTML via htmldate."""
    if not html or not html.strip():
        return ""
    try:
        import htmldate

        found = htmldate.find_date(html, url=url or None)
        return str(found).strip() if found else ""
    except Exception as exc:
        logger.debug("htmldate extraction failed for %s: %s", url, exc)
        return ""


async def _fetch_tier1_curl_cffi(
    url: str, *, timeout: float, max_redirects: int
) -> tuple[str, int, str, str] | None:
    """Fetch with curl_cffi Chrome TLS impersonation + manual SSRF-checked redirects."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None

    current_url = url
    async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
        for _ in range(max_redirects + 1):
            await validate_public_url(current_url)
            resp = await session.get(
                current_url,
                allow_redirects=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            status = int(resp.status_code)
            if status in REDIRECT_STATUSES:
                location = (resp.headers.get("location") or resp.headers.get("Location") or "").strip()
                if not location:
                    break
                current_url = urljoin(current_url, location)
                await validate_public_url(current_url)
                continue
            ctype = (resp.headers.get("content-type") or resp.headers.get("Content-Type") or "").lower()
            text = resp.text if (not ctype or any(t in ctype for t in ("html", "xml", "text", "json"))) else ""
            return current_url, status, text or "", "curl_cffi"
        raise ValueError(f"Too many redirects fetching {url}")


async def _fetch_tier1_public_http(
    url: str, *, timeout: float, max_redirects: int
) -> tuple[str, int, str, str]:
    """Fetch via sellbird.public_http.public_http_client with SSRF-checked redirects."""
    await validate_public_url(url)
    async with public_http_client(
        timeout=httpx.Timeout(timeout),
        max_redirects=max_redirects,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as client:
        resp = await client.get(url)
        final_url = str(resp.url or url)
        await validate_public_url(final_url)
        ctype = (resp.headers.get("content-type") or "").lower()
        text = resp.text if (not ctype or any(t in ctype for t in ("html", "xml", "text", "json"))) else ""
        return final_url, int(resp.status_code), text or "", "http"


async def _fetch_tier2_playwright(url: str, *, timeout: float) -> FetchPageResult | None:
    """Render JS locally via headless Playwright Chromium, guarding every request against SSRF."""
    await validate_public_url(url)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("Playwright package not installed; skipping Tier 2 JS render.")
        return None

    ssrf_Abort: list[str] = []

    async def _guard_route(route):
        req_url = route.request.url
        scheme = urlsplit(req_url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            await route.continue_()
            return
        try:
            await validate_public_url(req_url)
        except ValueError as exc:
            if route.request.is_navigation_request():
                ssrf_Abort.append(str(exc))
            await route.abort("accessdenied")
            return
        await route.continue_()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                page = await context.new_page()
                await page.route("**/*", _guard_route)
                ms = max(3000, int(timeout * 1000))
                try:
                    resp = await page.goto(url, wait_until="networkidle", timeout=ms)
                except Exception:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=ms)
                if ssrf_Abort:
                    raise ValueError(ssrf_Abort[0])
                final_url = page.url or url
                await validate_public_url(final_url)
                html = await page.content()
                status = int(resp.status) if resp is not None else 200
            finally:
                await browser.close()
    except ValueError:
        raise
    except Exception as exc:
        logger.debug("Playwright Tier 2 unavailable or failed for %s: %s", url, exc)
        return None

    challenged = looks_like_challenge(html, status)
    return FetchPageResult(
        url=url,
        final_url=final_url,
        status_code=status,
        html=html,
        markdown="" if challenged else extract_markdown(html, final_url),
        published_date="" if challenged else extract_published_date(html, final_url),
        source="playwright",
        challenged=challenged,
    )


async def _fetch_tier3_firecrawl(
    url: str, *, api_key: str, base_url: str, timeout: float
) -> FetchPageResult | None:
    """Scrape via Firecrawl v2 API (/v2/scrape) when configured."""
    await validate_public_url(url)
    endpoint = f"{base_url.rstrip('/')}/v2/scrape"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(max(timeout, 30.0))) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["rawHtml", "markdown"]},
            )
    except httpx.HTTPError as exc:
        logger.debug("Firecrawl v2 scrape failed for %s: %s", url, exc)
        return None

    if resp.status_code >= 400:
        logger.debug("Firecrawl v2 returned HTTP %d for %s", resp.status_code, url)
        return None

    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_html = str(data.get("rawHtml") or data.get("html") or "")
    md = str(data.get("markdown") or "").strip()
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    final_url = str(meta.get("sourceURL") or meta.get("url") or url)
    # Guard against an external scraper returning a redirect to an internal address.
    await validate_public_url(final_url)

    try:
        status_code = int(meta.get("statusCode") or 200)
    except (TypeError, ValueError):
        status_code = 200

    if not md and raw_html:
        md = extract_markdown(raw_html, final_url)
    pub_date = str(meta.get("publishedTime") or "")[:10] or extract_published_date(raw_html, final_url)

    return FetchPageResult(
        url=url,
        final_url=final_url,
        status_code=status_code,
        html=raw_html,
        markdown=md,
        published_date=pub_date,
        source="firecrawl",
        challenged=False,
        cost_usd=FIRECRAWL_SCRAPE_COST_USD,
    )


def _resolve_fetch_settings(
    config=None, env=None, *, render_js: bool | None = None, backend: str = ""
) -> tuple[str, bool, str, str]:
    fetch_cfg = getattr(config, "fetch", None)
    resolved_backend = (
        backend
        or str(getattr(fetch_cfg, "backend", "") or "")
        or os.getenv("FETCH_BACKEND", "auto")
    ).strip().lower()
    if render_js is None:
        cfg_js = getattr(fetch_cfg, "render_js", None)
        if cfg_js is None:
            render_js = os.getenv("FETCH_RENDER_JS", "true").strip().lower() not in ("0", "false", "no")
        else:
            render_js = bool(cfg_js)
    firecrawl_key = (
        (str(getattr(env, "firecrawl_api_key", "") or "") if env is not None else "")
        or os.getenv("FIRECRAWL_API_KEY", "")
    ).strip()
    firecrawl_url = (
        (str(getattr(env, "firecrawl_api_url", "") or "") if env is not None else "")
        or os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    ).strip()
    return resolved_backend, bool(render_js), firecrawl_key, firecrawl_url


async def fetch_page(
    url: str,
    *,
    config=None,
    env=None,
    render_js: bool | None = None,
    backend: str = "",
    timeout: float = 15.0,
    max_redirects: int = 5,
) -> FetchPageResult:
    """Fetch a public web page via the 3-tier SSRF-guarded stealth waterfall.

    Always runs ``validate_public_url(url)`` first — private/loopback/link-local
    and NAT64 targets raise ``ValueError`` before any network tier executes.
    """
    await validate_public_url(url)
    resolved_backend, js_enabled, firecrawl_key, firecrawl_url = _resolve_fetch_settings(
        config, env, render_js=render_js, backend=backend
    )

    t1_result: FetchPageResult | None = None
    last_error = ""

    # ── Tier 1: curl_cffi / public_http_client ──
    if resolved_backend not in ("playwright", "firecrawl"):
        t1_tuple = None
        if resolved_backend in ("auto", "", "curl_cffi", "stealth"):
            try:
                t1_tuple = await _fetch_tier1_curl_cffi(
                    url, timeout=timeout, max_redirects=max_redirects
                )
            except ValueError:
                # Re-raise SSRF blocks immediately!
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.debug("curl_cffi Tier 1 failed for %s: %s", url, exc)

        if t1_tuple is None:
            try:
                t1_tuple = await _fetch_tier1_public_http(
                    url, timeout=timeout, max_redirects=max_redirects
                )
            except ValueError:
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.debug("public_http Tier 1 failed for %s: %s", url, exc)

        if t1_tuple is not None:
            final_url, status, html, source = t1_tuple
            challenged = status in (200, 202, 403, 429, 503) and looks_like_challenge(html, status)
            spa = not challenged and is_spa_shell(html)
            t1_result = FetchPageResult(
                url=url,
                final_url=final_url,
                status_code=status,
                html=html,
                markdown="" if challenged else extract_markdown(html, final_url),
                published_date="" if challenged else extract_published_date(html, final_url),
                source=source,
                challenged=challenged,
            )
            if 200 <= status < 400 and not challenged and not (js_enabled and spa):
                return t1_result

    # ── Tier 2: Local Headless Playwright JS Render ──
    need_tier2 = resolved_backend == "playwright" or (
        js_enabled
        and resolved_backend in ("auto", "", "curl_cffi", "stealth")
        and (
            t1_result is None
            or t1_result.challenged
            or is_spa_shell(t1_result.html)
            or t1_result.status_code in (403, 429, 503)
        )
    )
    if need_tier2:
        t2_result = await _fetch_tier2_playwright(url, timeout=timeout)
        if t2_result is not None and t2_result.ok and not is_spa_shell(t2_result.html):
            return t2_result
        if t2_result is not None and (t1_result is None or not t2_result.challenged):
            t1_result = t2_result

    # ── Tier 3: Cloud Firecrawl v2 Fallback ──
    need_tier3 = bool(firecrawl_key) and (
        resolved_backend == "firecrawl"
        or t1_result is None
        or t1_result.challenged
        or not t1_result.ok
        or (js_enabled and is_spa_shell(t1_result.html))
    )
    if need_tier3:
        t3_result = await _fetch_tier3_firecrawl(
            url, api_key=firecrawl_key, base_url=firecrawl_url, timeout=timeout
        )
        if t3_result is not None and t3_result.ok:
            return t3_result
        if t3_result is not None and t1_result is None:
            t1_result = t3_result

    if t1_result is not None:
        return t1_result

    return FetchPageResult(
        url=url,
        final_url=url,
        status_code=0,
        html="",
        markdown="",
        source="http",
        challenged=False,
        error=last_error or "fetch failed across all tiers",
    )


stealth_fetch = fetch_page


class StealthFetcher:
    """Stateful wrapper around ``fetch_page`` for repeated multi-page runs."""

    def __init__(self, config=None, env=None, *, render_js: bool | None = None, backend: str = ""):
        self.config = config
        self.env = env
        self.render_js = render_js
        self.backend = backend
        self.total_cost_usd: float = 0.0

    async def fetch(self, url: str, *, timeout: float = 15.0) -> FetchPageResult:
        res = await fetch_page(
            url,
            config=self.config,
            env=self.env,
            render_js=self.render_js,
            backend=self.backend,
            timeout=timeout,
        )
        self.total_cost_usd += res.cost_usd
        return res
