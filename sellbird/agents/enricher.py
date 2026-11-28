"""Clay-Style Custom AI Enrichment Engine.

Evaluates user-defined custom enrichment columns across companies (or prospects),
combining stored company profile data, observations, site facts, and optional live
web search into structured cell values with verified provenance URLs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from urllib.parse import urlparse

import httpx

from sellbird.site_facts import site_facts
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.enricher")


async def _maybe_await(val):
    return await val if inspect.isawaitable(val) else val


class ClayEnricher:
    """Custom AI column enrichment engine modeled after Clay tables."""

    def __init__(self, state, brain, config=None, env=None):
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
    def _ddgs_sync(query: str, max_results: int = 5) -> list[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            return [
                {
                    "url": str(r.get("href") or r.get("url") or "").strip(),
                    "title": str(r.get("title") or "").strip(),
                    "snippet": str(r.get("body") or "").strip(),
                }
                for r in (ddgs.text(query, max_results=max_results) or [])
                if isinstance(r, dict) and (r.get("href") or r.get("url"))
            ]

    async def _search_for_column(self, query: str) -> list[dict]:
        tavily_key = self._env_val("tavily_api_key", "TAVILY_API_KEY")
        if tavily_key:
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {tavily_key}"},
                        json={"api_key": tavily_key, "query": query, "max_results": 5},
                    )
                    if resp.status_code == 200:
                        hits = [
                            {
                                "url": str(r.get("url") or "").strip(),
                                "title": str(r.get("title") or "").strip(),
                                "snippet": str(r.get("content") or "").strip(),
                            }
                            for r in (resp.json() or {}).get("results") or []
                            if isinstance(r, dict) and r.get("url")
                        ]
                        if hits:
                            return hits
            except Exception as exc:
                logger.debug("ClayEnricher Tavily search failed: %s", exc)

        try:
            return await asyncio.to_thread(self._ddgs_sync, query, 5)
        except Exception as exc:
            logger.debug("ClayEnricher ddgs search failed: %s", exc)
            return []

    async def _get_columns(self) -> list[dict]:
        getter = getattr(self.state, "get_enrichment_columns", None)
        if getter is None:
            return []
        try:
            cols = await _maybe_await(getter())
        except Exception as exc:
            logger.debug("get_enrichment_columns failed: %s", exc)
            return []
        if not isinstance(cols, list):
            return []
        out = []
        for col in cols:
            if isinstance(col, dict):
                out.append(col)
            elif hasattr(col, "model_dump"):
                out.append(col.model_dump())
            elif hasattr(col, "__dict__"):
                out.append(dict(col.__dict__))
        return out

    async def _save_value(
        self,
        *,
        column_id: str,
        company_id: str,
        value: str,
        source_url: str = "",
        entity_type: str = "company",
    ) -> bool:
        setter = getattr(self.state, "set_enrichment_value", None)
        if setter is None:
            return False

        try:
            sig = inspect.signature(setter)
            params = list(sig.parameters.values())
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            param_names = [
                p.name
                for p in params
                if p.name != "self"
                and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            ]
        except (TypeError, ValueError):
            param_names, has_var_kw, has_var_pos = [], True, True

        # Mock / AsyncMock / (**kw) typically exposes **kwargs without named params:
        # call with clear keyword arguments, or positional if **kwargs is absent.
        if has_var_kw and not param_names:
            await _maybe_await(
                setter(
                    company_id=company_id,
                    column_id=column_id,
                    value=value,
                    source_url=source_url,
                )
            )
            return True

        kwargs = {}
        name_map = {
            "column_id": column_id,
            "col_id": column_id,
            "column": column_id,
            "company_id": company_id,
            "entity_id": company_id,
            "target_id": company_id,
            "prospect_id": company_id,
            "entity_type": entity_type,
            "value": value,
            "source_url": source_url,
            "evidence_url": source_url,
        }
        for pname in param_names:
            if pname in name_map:
                kwargs[pname] = name_map[pname]
        if kwargs:
            await _maybe_await(setter(**kwargs))
            return True

        await _maybe_await(setter(company_id, column_id, value, source_url))
        return True

    async def enrich_company_column(self, company, column: dict) -> dict | None:
        """Evaluate one custom column for one company and persist the result."""
        if isinstance(company, str):
            company = await self.state.get_company(company)
        if company is None:
            return None

        if isinstance(company, dict):
            company_id = str(company.get("id") or "")
            name = str(company.get("name") or "")
            domain = str(company.get("domain") or "")
            website = str(company.get("website") or (f"https://{domain}" if domain else ""))
            description = str(company.get("description") or "")
            industry = str(company.get("industry") or "")
            location = str(company.get("location") or "")
            tech_stack = list(company.get("tech_stack") or [])
            signals = list(company.get("signals") or [])
        else:
            company_id = str(getattr(company, "id", "") or "")
            name = str(getattr(company, "name", "") or "")
            domain = str(getattr(company, "domain", "") or "")
            website = str(getattr(company, "website", "") or (f"https://{domain}" if domain else ""))
            description = str(getattr(company, "description", "") or "")
            industry = str(getattr(company, "industry", "") or "")
            location = str(getattr(company, "location", "") or "")
            tech_stack = list(getattr(company, "tech_stack", []) or [])
            signals = list(getattr(company, "signals", []) or [])

        col_id = str(column.get("id") or column.get("column_id") or column.get("key") or column.get("name") or "").strip()
        col_name = str(column.get("name") or column.get("label") or col_id).strip()
        col_prompt = str(column.get("prompt") or column.get("instruction") or column.get("description") or col_name).strip()
        col_type = str(column.get("data_type") or column.get("value_type") or column.get("output_type") or "text").strip()
        use_web = bool(column.get("use_web_search", False))

        if not company_id or not col_id:
            return None

        context_lines = [
            f"Company: {name}",
            f"Domain: {domain}",
            f"Website: {website}",
        ]
        if industry:
            context_lines.append(f"Industry: {industry}")
        if location:
            context_lines.append(f"Location: {location}")
        if description:
            context_lines.append(f"Description: {description}")
        if tech_stack:
            context_lines.append(f"Tech stack: {', '.join(str(t) for t in tech_stack)}")
        for sig in signals[:8]:
            if isinstance(sig, dict):
                context_lines.append(f"Signal ({sig.get('type', 'signal')}): {sig.get('detail', '')}")

        known_urls: set[str] = set()
        if website:
            known_urls.add(website)

        obs_getter = getattr(self.state, "get_observations", None)
        if obs_getter is not None:
            try:
                obs_rows = await obs_getter(company_id=company_id, latest_only=True, limit=100)
                for row in obs_rows or []:
                    code = row.get("signal_code")
                    val_txt = row.get("value_text") or ""
                    val_num = row.get("value_num")
                    ev_url = str(row.get("evidence_url") or "").strip()
                    if ev_url:
                        known_urls.add(ev_url)
                    context_lines.append(f"Observation [{code}]: {val_txt or val_num}")
            except Exception:
                pass

        facts = await site_facts(self.state, company_id)
        if facts:
            context_lines.extend(facts)

        if use_web and (name or domain):
            search_q = f'"{name or domain}" {domain} {col_name}'.strip()
            web_hits = await self._search_for_column(search_q)
            for hit in web_hits:
                u = hit.get("url") or ""
                if u:
                    known_urls.add(u)
                    context_lines.append(f"Web result ({u}): {hit.get('title', '')} — {hit.get('snippet', '')}")

        prompt = f"""You are a precision B2B data enrichment engine (Clay-style column evaluator).
Compute the value for the custom column below using ONLY the provided company facts and search context.

Column Name: {col_name}
Expected Data Type: {col_type}
Column Instruction: {col_prompt}

Company Context:
{fence(chr(10).join(context_lines), "company_context")}

{UNTRUSTED_RULE}

If the answer cannot be determined from the context, return {{"value": "", "source_url": ""}}.
Only set ``source_url`` to an exact URL appearing in the context above.

Return ONLY valid JSON:
{{"value": "...", "source_url": "..."}}"""

        res = await self.brain.think_json(
            prompt,
            session_id="sellbird-enricher",
            agent="enricher",
            task="enrich_column",
        )
        if not isinstance(res, dict):
            return None

        raw_val = res.get("value")
        value = "" if raw_val is None else str(raw_val).strip()
        source_url = str(res.get("source_url") or "").strip()
        if source_url:
            if not known_urls:
                source_url = ""
            else:
                def _host_of(raw_u: str) -> str:
                    u = (raw_u or "").strip()
                    if not u:
                        return ""
                    if "://" not in u:
                        u = "https://" + u
                    return (urlparse(u).hostname or "").lower().removeprefix("www.")

                parsed_hosts = {h for h in (_host_of(u) for u in known_urls) if h}
                cand_host = _host_of(source_url)
                if source_url not in known_urls and (
                    not cand_host or not any(cand_host == h or cand_host.endswith("." + h) for h in parsed_hosts)
                ):
                    source_url = ""

        if not value:
            return {
                "company_id": company_id,
                "column_id": col_id,
                "value": "",
                "source_url": "",
                "saved": False,
            }

        saved = await self._save_value(
            column_id=col_id,
            company_id=company_id,
            value=value,
            source_url=source_url,
            entity_type=str(column.get("entity_type") or "company"),
        )
        return {
            "company_id": company_id,
            "column_id": col_id,
            "value": value,
            "source_url": source_url,
            "saved": saved,
        }

    @staticmethod
    def _is_active_company_column(col: dict, column_id: str | None = None) -> bool:
        if col.get("enabled") is False or col.get("enabled") == 0 or col.get("disabled") is True:
            return False
        etype = str(col.get("entity_type") or "company").strip().lower()
        if etype not in ("company", "all", ""):
            return False
        if column_id:
            target = str(column_id).strip()
            candidates = {
                str(col.get(k) or "").strip()
                for k in ("id", "column_id", "key", "name")
            }
            if target not in candidates:
                return False
        return True

    async def enrich_company(self, company_id: str, columns: list[dict] | None = None) -> int:
        cols = columns if columns is not None else await self._get_columns()
        cols = [c for c in cols if self._is_active_company_column(c)]
        if not cols:
            return 0
        company = await self.state.get_company(company_id)
        if company is None:
            return 0
        written = 0
        for col in cols:
            outcome = await self.enrich_company_column(company, col)
            if outcome and outcome.get("saved"):
                written += 1
        return written

    async def run(
        self,
        companies: list | None = None,
        limit: int = 10,
        column_id: str | None = None,
        company_ids: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> int:
        """Evaluate active custom company enrichment columns across target companies."""
        cols = [
            c for c in await self._get_columns()
            if self._is_active_company_column(c, column_id=column_id)
        ]
        if not cols:
            return 0

        wanted_ids: list[str] | None = None
        if company_ids is not None:
            wanted_ids = [str(cid).strip() for cid in company_ids if str(cid).strip()]
            if not wanted_ids:
                return 0

        if companies is None:
            if wanted_ids and callable(getattr(self.state, "get_company", None)):
                fetched = []
                for cid in wanted_ids:
                    try:
                        comp = await _maybe_await(self.state.get_company(cid))
                    except Exception:
                        comp = None
                    if comp is not None:
                        fetched.append(comp)
                if fetched:
                    companies = fetched

        if companies is None:
            fetch_limit = max(limit, len(wanted_ids)) if wanted_ids else limit
            for attr in ("list_companies", "get_companies", "get_companies_with_domains", "companies_needing_prospects"):
                fn = getattr(self.state, attr, None)
                if fn is not None:
                    try:
                        companies = await _maybe_await(fn(limit=fetch_limit))
                        break
                    except TypeError:
                        companies = await _maybe_await(fn())
                        break
                    except Exception:
                        continue
        if not companies:
            return 0

        target_list = list(companies)
        if wanted_ids is not None:
            wanted_set = set(wanted_ids)
            target_list = [
                c for c in target_list
                if str((c.get("id") if isinstance(c, dict) else getattr(c, "id", "")) or "") in wanted_set
            ]

        total = 0
        for comp in target_list[:limit]:
            cid = comp.get("id") if isinstance(comp, dict) else getattr(comp, "id", "")
            if not cid:
                continue
            for col in cols:
                try:
                    outcome = await self.enrich_company_column(comp, col)
                    if outcome and outcome.get("saved"):
                        total += 1
                except Exception as exc:
                    logger.warning("ClayEnricher failed for company %s col %r: %s", cid, col.get("name"), exc)
        return total
