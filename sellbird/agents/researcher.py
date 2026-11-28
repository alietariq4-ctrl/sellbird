"""Autonomous Deep Research Agent — OSINT + multi-source web research with strict citation verification.

Searches web & news (Tavily / SearXNG / Serper / keyless DuckDuckGo fallback),
queries public registries & APIs (GitHub orgs, Wikipedia, UK Companies House,
SEC mentions), fetches top cited pages via the SSRF-guarded stealth waterfall,
and synthesizes verified facts, decision-makers, recent triggers, and ICP fit.

Hallucination firewall: ONLY observations whose ``evidence_url`` matches a real
URL retrieved in the current research batch are persisted as ``REGISTRY_VERIFIED``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx

from sellbird.collectors.discover import is_junk, normalize_domain
from sellbird.integrations.stealth_fetch import fetch_page, validate_public_url
from sellbird.public_http import public_http_client
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.researcher")

MAX_SEARCH_RESULTS = 8
MAX_PAGES_TO_FETCH = 3
PAGE_MD_CHARS = 3500


def _canonical_url(url: str) -> str:
    """Normalize a URL for strict citation membership checks."""
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), host, path, "", parsed.query, ""))


class DeepResearcher:
    """Autonomous OSINT & web research agent for B2B companies."""

    def __init__(self, state, brain, config=None, env=None):
        # Defensively handle swapped (brain, state) argument order if a caller passes Brain first.
        if hasattr(state, "think_json") and not hasattr(state, "get_company") and hasattr(brain, "get_company"):
            state, brain = brain, state
        self.state = state
        self.brain = brain
        self.config = config
        self.env = env

    def _env_val(self, field_name: str, env_var: str = "") -> str:
        val = str(getattr(self.env, field_name, "") or "") if self.env is not None else ""
        if not val:
            val = os.getenv(env_var or field_name.upper(), "")
        return val.strip()

    @staticmethod
    def _ddgs_search_sync(query: str, max_results: int = MAX_SEARCH_RESULTS) -> list[dict]:
        from ddgs import DDGS

        rows: list[dict] = []
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=max_results) or []:
                if isinstance(item, dict):
                    rows.append({
                        "url": str(item.get("href") or item.get("url") or "").strip(),
                        "title": str(item.get("title") or "").strip(),
                        "snippet": str(item.get("body") or item.get("snippet") or "").strip(),
                    })
            try:
                for item in ddgs.news(query, max_results=min(4, max_results)) or []:
                    if isinstance(item, dict):
                        rows.append({
                            "url": str(item.get("url") or item.get("href") or "").strip(),
                            "title": str(item.get("title") or "").strip(),
                            "snippet": str(item.get("body") or "").strip(),
                        })
            except Exception:
                pass
        return rows

    async def _search_web(self, query: str) -> list[dict]:
        """Search web & news via Tavily -> SearXNG -> Serper -> keyless ddgs fallback."""
        tavily_key = self._env_val("tavily_api_key", "TAVILY_API_KEY")
        if tavily_key:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {tavily_key}"},
                        json={
                            "api_key": tavily_key,
                            "query": query,
                            "max_results": MAX_SEARCH_RESULTS,
                            "search_depth": "basic",
                        },
                    )
                    if resp.status_code == 200:
                        items = (resp.json() or {}).get("results") or []
                        out = [
                            {
                                "url": str(i.get("url") or "").strip(),
                                "title": str(i.get("title") or "").strip(),
                                "snippet": str(i.get("content") or i.get("snippet") or "").strip(),
                            }
                            for i in items
                            if isinstance(i, dict) and i.get("url")
                        ]
                        if out:
                            return out
            except Exception as exc:
                logger.debug("Tavily research search failed: %s", exc)

        searxng_url = self._env_val("searxng_url", "SEARXNG_URL").rstrip("/")
        if searxng_url:
            try:
                if not searxng_url.startswith(("http://", "https://")):
                    searxng_url = "http://" + searxng_url
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"{searxng_url}/search",
                        params={"q": query, "format": "json"},
                    )
                    if resp.status_code == 200:
                        items = (resp.json() or {}).get("results") or []
                        out = [
                            {
                                "url": str(i.get("url") or "").strip(),
                                "title": str(i.get("title") or "").strip(),
                                "snippet": str(i.get("content") or "").strip(),
                            }
                            for i in items[:MAX_SEARCH_RESULTS]
                            if isinstance(i, dict) and i.get("url")
                        ]
                        if out:
                            return out
            except Exception as exc:
                logger.debug("SearXNG research search failed: %s", exc)

        serper_key = self._env_val("serper_api_key", "SERPER_API_KEY")
        if serper_key:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://google.serper.dev/search",
                        headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                        json={"q": query, "num": MAX_SEARCH_RESULTS},
                    )
                    if resp.status_code == 200:
                        items = (resp.json() or {}).get("organic") or []
                        out = [
                            {
                                "url": str(i.get("link") or "").strip(),
                                "title": str(i.get("title") or "").strip(),
                                "snippet": str(i.get("snippet") or "").strip(),
                            }
                            for i in items
                            if isinstance(i, dict) and i.get("link")
                        ]
                        if out:
                            return out
            except Exception as exc:
                logger.debug("Serper research search failed: %s", exc)

        try:
            return await asyncio.to_thread(self._ddgs_search_sync, query, MAX_SEARCH_RESULTS)
        except Exception as exc:
            logger.debug("ddgs research search failed: %s", exc)
            return []

    async def _check_public_osint(self, name: str, domain: str) -> list[dict]:
        """Check free public registries (GitHub orgs, Wikipedia, UK Companies House)."""
        findings: list[dict] = []
        clean_dom = normalize_domain(domain)
        dom_parts = [p for p in clean_dom.split(".") if p and p not in ("www", "app", "mail", "api", "portal")]
        stem = dom_parts[0] if dom_parts else (clean_dom.split(".")[0] if clean_dom else "")

        async with public_http_client(timeout=httpx.Timeout(8.0)) as client:
            # 1. GitHub Organization API
            if stem and len(stem) >= 3:
                gh_api = f"https://api.github.com/orgs/{quote(stem)}"
                try:
                    resp = await client.get(
                        gh_api,
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "Sellbird-Researcher/1.0"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        blog_domain = normalize_domain(str(data.get("blog") or ""))
                        if not domain or blog_domain == normalize_domain(domain):
                            html_url = str(data.get("html_url") or f"https://github.com/{stem}")
                            repos = int(data.get("public_repos") or 0)
                            desc = str(data.get("description") or "").strip()
                            findings.append({
                                "url": html_url,
                                "title": f"GitHub Org: {data.get('name') or stem}",
                                "snippet": f"Verified GitHub organization ({repos} public repos). {desc}".strip(),
                            })
                except Exception as exc:
                    logger.debug("GitHub OSINT lookup skipped for %s: %s", stem, exc)

            # 2. Wikipedia REST Summary
            if name and len(name) >= 3:
                wiki_slug = quote(name.replace(" ", "_"))
                wiki_api = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_slug}"
                try:
                    resp = await client.get(
                        wiki_api,
                        headers={"Accept": "application/json", "User-Agent": "Sellbird-Researcher/1.0"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        extract = str(data.get("extract") or "").strip()
                        page_url = str(
                            ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
                            or f"https://en.wikipedia.org/wiki/{wiki_slug}"
                        )
                        if extract and (not domain or stem.lower() in extract.lower() or name.lower() in extract.lower()):
                            findings.append({
                                "url": page_url,
                                "title": f"Wikipedia: {data.get('title') or name}",
                                "snippet": extract[:500],
                            })
                except Exception as exc:
                    logger.debug("Wikipedia OSINT lookup skipped for %s: %s", name, exc)

            # 3. UK Companies House (optional key)
            ch_key = self._env_val("companies_house_api_key", "COMPANIES_HOUSE_API_KEY")
            if ch_key and name:
                try:
                    resp = await client.get(
                        "https://api.company-information.service.gov.uk/search/companies",
                        params={"q": name, "items_per_page": 2},
                        auth=(ch_key, ""),
                    )
                    if resp.status_code == 200:
                        for item in (resp.json() or {}).get("items") or []:
                            cnum = str(item.get("company_number") or "").strip()
                            ctitle = str(item.get("title") or "").strip()
                            cstatus = str(item.get("company_status") or "").strip()
                            if cnum and ctitle:
                                ch_url = f"https://find-and-update.company-information.service.gov.uk/company/{cnum}"
                                findings.append({
                                    "url": ch_url,
                                    "title": f"Companies House: {ctitle}",
                                    "snippet": f"Company #{cnum}, status: {cstatus}, address: {item.get('address_snippet', '')}",
                                })
                except Exception as exc:
                    logger.debug("Companies House lookup skipped for %s: %s", name, exc)

        return findings

    def _build_icp_summary(self) -> str:
        icp = getattr(self.config, "icp", None)
        product = getattr(self.config, "product", None)
        parts = []
        if product:
            parts.append(f"Our product: {getattr(product, 'name', '')} — {getattr(product, 'description', '')}")
        if icp:
            if getattr(icp, "industries", None):
                parts.append(f"Target industries: {', '.join(icp.industries)}")
            if getattr(icp, "titles", None):
                parts.append(f"Target titles: {', '.join(icp.titles)}")
            if getattr(icp, "company_size", None):
                parts.append(f"Target size: {icp.company_size}")
        return "\n".join(parts) or "B2B prospects"

    async def research_company(self, company_or_id) -> dict:
        """Run deep OSINT + web research on a single company and persist verified citations."""
        if isinstance(company_or_id, str):
            company = await self.state.get_company(company_or_id)
            if company is None:
                return {"ok": False, "error": f"Company {company_or_id!r} not found"}
        elif isinstance(company_or_id, dict):
            company_id = str(company_or_id.get("id") or "")
            company = await self.state.get_company(company_id) if company_id else None
            if company is None:
                from types import SimpleNamespace
                company = SimpleNamespace(**company_or_id)
        else:
            company = company_or_id

        company_id = str(getattr(company, "id", "") or "")
        name = str(getattr(company, "name", "") or "").strip()
        domain = normalize_domain(str(getattr(company, "domain", "") or getattr(company, "website", "") or ""))
        website = str(getattr(company, "website", "") or (f"https://{domain}" if domain else "")).strip()

        query = f'"{name}" {domain} news OR funding OR leadership OR product'.strip() if name else domain
        if not query:
            return {"ok": False, "error": "Company has neither name nor domain"}

        search_hits, osint_hits = await asyncio.gather(
            self._search_web(query),
            self._check_public_osint(name, domain),
        )

        all_sources: list[dict] = []
        allowed_exact: set[str] = set()
        allowed_canonical: dict[str, str] = {}

        def _register_url(raw_url: str) -> str:
            canon = _canonical_url(raw_url)
            if not canon:
                return ""
            allowed_exact.add(raw_url.strip())
            allowed_canonical.setdefault(canon, raw_url.strip())
            return raw_url.strip()

        if website:
            _register_url(website)
            all_sources.append({"url": website, "title": f"{name or domain} Homepage", "snippet": getattr(company, "description", "") or ""})

        seen_canon: set[str] = {_canonical_url(website)} if website else set()
        for hit in [*osint_hits, *search_hits]:
            u = str(hit.get("url") or "").strip()
            canon = _canonical_url(u)
            if not canon or canon in seen_canon:
                continue
            seen_canon.add(canon)
            _register_url(u)
            all_sources.append(hit)

        # Fetch top cited pages concurrently via SSRF-guarded stealth_fetch;
        # when external search hits are sparse, also inspect /about and /careers on their domain.
        urls_to_fetch: list[str] = []
        if website:
            urls_to_fetch.append(website)
        for hit in all_sources:
            u = hit["url"]
            if u not in urls_to_fetch and len(urls_to_fetch) < MAX_PAGES_TO_FETCH:
                host = normalize_domain(u)
                if host and (host == domain or not is_junk(host)):
                    urls_to_fetch.append(u)
        if website and len(urls_to_fetch) < MAX_PAGES_TO_FETCH + 1:
            for sub in ("/about", "/careers"):
                sub_url = urljoin(website.rstrip("/") + "/", sub.lstrip("/"))
                sub_canon = _canonical_url(sub_url)
                if sub_canon and sub_canon not in seen_canon and sub_url not in urls_to_fetch:
                    seen_canon.add(sub_canon)
                    _register_url(sub_url)
                    urls_to_fetch.append(sub_url)
                if len(urls_to_fetch) >= MAX_PAGES_TO_FETCH + 1:
                    break

        async def _fetch_one(target_url: str) -> str:
            try:
                await validate_public_url(target_url)
                fetched = await fetch_page(target_url, config=self.config, env=self.env, timeout=12.0)
            except Exception as exc:
                logger.debug("Researcher stealth_fetch skipped %s: %s", target_url, exc)
                return ""
            if fetched.final_url:
                _register_url(fetched.final_url)
            if fetched.markdown:
                date_note = f" (published {fetched.published_date})" if fetched.published_date else ""
                return (
                    f"SOURCE URL: {fetched.final_url or target_url}{date_note}\n"
                    f"{fetched.markdown[:PAGE_MD_CHARS]}"
                )
            return ""

        fetched_blocks = await asyncio.gather(*(_fetch_one(u) for u in urls_to_fetch), return_exceptions=True)
        page_extracts: list[str] = [
            blk for blk in fetched_blocks if isinstance(blk, str) and blk
        ]

        sources_text = "\n\n".join(
            f"[{idx + 1}] URL: {s['url']}\nTitle: {s.get('title', '')}\nSnippet: {s.get('snippet', '')}"
            for idx, s in enumerate(all_sources[:12])
        )
        pages_text = "\n\n---\n\n".join(page_extracts)

        prompt = f"""You are an investigative B2B OSINT researcher. Synthesize verified facts, leadership/decision-makers, recent triggers, and ICP fit for {name or domain} ({domain}).

{self._build_icp_summary()}

SEARCH & REGISTRY SOURCES:
{fence(sources_text or "No external search hits.", "search_sources")}

EXTRACTED PAGE MARKDOWN:
{fence(pages_text or "No page markdown extracted.", "page_markdown")}

{UNTRUSTED_RULE}

CRITICAL CITATION RULE:
- Every fact, decision-maker, and trigger MUST cite an exact ``evidence_url`` copied verbatim from the SOURCE URLs above.
- NEVER invent, guess, or modify URLs. Omit any claim that lacks a direct URL from the batch above.

Return ONLY valid JSON matching this schema:
{{
  "summary": "1-2 sentence factual summary of what the company does",
  "icp_fit": "1 sentence on why they fit or don't fit our ICP",
  "opening_hook": "1 concise, verifiable cold-email opening observation grounded in a source above (or empty string)",
  "verified_facts": [
    {{"fact": "Concrete factual detail", "evidence_url": "https://..."}}
  ],
  "decision_makers": [
    {{"name": "Full Name", "title": "Role/Title", "evidence_url": "https://..."}}
  ],
  "recent_triggers": [
    {{"type": "funding|hiring|product_launch|expansion|press", "detail": "Specific trigger with date/numbers if stated", "evidence_url": "https://..."}}
  ]
}}"""

        raw_synthesis = await self.brain.think_json(
            prompt,
            session_id="sellbird-researcher",
            agent="researcher",
            task="company_research",
        )
        synthesis_failed = not isinstance(raw_synthesis, dict)
        if synthesis_failed:
            raw_synthesis = {}

        def _verify_citation(candidate_url: str) -> str:
            u = str(candidate_url or "").strip()
            if not u:
                return ""
            if u in allowed_exact:
                return u
            canon = _canonical_url(u)
            return allowed_canonical.get(canon, "")

        dropped_unverified = 0
        verified_facts: list[dict] = []
        for item in raw_synthesis.get("verified_facts") or []:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact") or "").strip()
            verified_url = _verify_citation(str(item.get("evidence_url") or ""))
            if not fact:
                continue
            if not verified_url:
                dropped_unverified += 1
                continue
            verified_facts.append({"fact": fact[:300], "evidence_url": verified_url})

        decision_makers: list[dict] = []
        for item in raw_synthesis.get("decision_makers") or []:
            if not isinstance(item, dict):
                continue
            dm_name = str(item.get("name") or "").strip()
            dm_title = str(item.get("title") or "").strip()
            verified_url = _verify_citation(str(item.get("evidence_url") or ""))
            if not dm_name:
                continue
            if not verified_url:
                dropped_unverified += 1
                continue
            decision_makers.append({"name": dm_name[:120], "title": dm_title[:120], "evidence_url": verified_url})

        recent_triggers: list[dict] = []
        for item in raw_synthesis.get("recent_triggers") or []:
            if not isinstance(item, dict):
                continue
            t_type = str(item.get("type") or "research").strip().lower()[:40]
            detail = str(item.get("detail") or "").strip()
            verified_url = _verify_citation(str(item.get("evidence_url") or ""))
            if not detail:
                continue
            if not verified_url:
                dropped_unverified += 1
                continue
            recent_triggers.append({"type": t_type, "detail": detail[:300], "evidence_url": verified_url})

        obs_to_write: list[dict] = []
        if company_id:
            for vf in verified_facts:
                obs_to_write.append({
                    "company_id": company_id,
                    "signal_code": "REGISTRY_VERIFIED",
                    "collector": "researcher",
                    "value_num": 1.0,
                    "value_text": vf["fact"],
                    "confidence": 0.9,
                    "evidence_url": vf["evidence_url"],
                })
            for dm in decision_makers:
                label = f"{dm['name']} — {dm['title']}" if dm["title"] else dm["name"]
                obs_to_write.append({
                    "company_id": company_id,
                    "signal_code": "REGISTRY_VERIFIED",
                    "collector": "researcher",
                    "value_num": 1.0,
                    "value_text": f"Decision-maker: {label}"[:300],
                    "confidence": 0.9,
                    "evidence_url": dm["evidence_url"],
                })
            for trig in recent_triggers:
                obs_to_write.append({
                    "company_id": company_id,
                    "signal_code": "REGISTRY_VERIFIED",
                    "collector": "researcher",
                    "value_num": 1.0,
                    "value_text": f"{trig['type']}: {trig['detail']}"[:300],
                    "confidence": 0.9,
                    "evidence_url": trig["evidence_url"],
                })

        observations_written = 0
        if obs_to_write and hasattr(self.state, "add_observations"):
            if hasattr(self.state, "get_signal_codes") and hasattr(self.state, "upsert_signal_code"):
                from sellbird.signals import seed_signal_catalog
                await seed_signal_catalog(self.state)
            observations_written = await self.state.add_observations(obs_to_write)

        today = datetime.now(timezone.utc).date().isoformat()
        new_signals: list[dict] = []
        for trig in recent_triggers:
            new_signals.append({
                "type": trig["type"],
                "detail": trig["detail"],
                "evidence_url": trig["evidence_url"],
                "found_at": today,
            })
        for vf in verified_facts[:3]:
            new_signals.append({
                "type": "research",
                "detail": vf["fact"],
                "evidence_url": vf["evidence_url"],
                "found_at": today,
            })

        if company_id and new_signals and hasattr(self.state, "update_company_signals"):
            await self.state.update_company_signals(company_id, new_signals=new_signals)

        return {
            "ok": not synthesis_failed,
            **({"error": "AI synthesis failed"} if synthesis_failed else {}),
            "company_id": company_id,
            "domain": domain,
            "summary": str(raw_synthesis.get("summary") or "").strip(),
            "icp_fit": str(raw_synthesis.get("icp_fit") or "").strip(),
            "opening_hook": str(raw_synthesis.get("opening_hook") or "").strip(),
            "verified_facts": verified_facts,
            "decision_makers": decision_makers,
            "recent_triggers": recent_triggers,
            "observations_written": observations_written,
            "dropped_unverified": dropped_unverified,
        }

    async def research_domain(self, domain: str) -> dict:
        """Research a company by domain, reusing its DB record if present."""
        clean_domain = normalize_domain(domain)
        if not clean_domain:
            return {"ok": False, "error": "Invalid or empty domain"}
        company = None
        getter = getattr(self.state, "get_company_by_domain", None)
        if callable(getter):
            company = await getter(clean_domain)
        if company is None:
            company = {
                "id": "",
                "name": "",
                "domain": clean_domain,
                "website": f"https://{clean_domain}",
            }
        return await self.research_company(company)

    async def run(
        self,
        companies: list | None = None,
        limit: int = 5,
        domain: str | None = None,
        company_id: str | None = None,
    ) -> list[dict]:
        """Research a domain, a company_id, or a batch of companies."""
        if domain:
            try:
                return [await self.research_domain(domain)]
            except Exception as exc:
                logger.warning("DeepResearcher failed on domain %r: %s", domain, exc)
                return []
        if company_id:
            try:
                return [await self.research_company(company_id)]
            except Exception as exc:
                logger.warning("DeepResearcher failed on company_id %r: %s", company_id, exc)
                return []
        if companies is None:
            getter = getattr(self.state, "companies_needing_profile", None) or getattr(self.state, "companies_needing_prospects", None)
            companies = await getter(limit=limit) if getter else []
        results = []
        for comp in list(companies)[:limit]:
            try:
                results.append(await self.research_company(comp))
            except Exception as exc:
                logger.warning("DeepResearcher failed on %r: %s", comp, exc)
        return results
