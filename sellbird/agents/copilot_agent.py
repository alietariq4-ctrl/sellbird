"""Interactive Agentic Copilot — ReAct tool-using assistant over Sellbird's pipeline.

Answers operator questions from live SQLite state, funnel analytics, company
profiles, enrichment values, and conversation threads using ``Brain.run_agent``,
while honoring Sellbird's human-in-the-loop safety boundary (it can pause
sending or draft/suggest copy, never approve/send emails or unpause live
sending autonomously).
"""

from __future__ import annotations

import logging
from typing import Any

from sellbird.agents.analyst import Analyst
from sellbird.brain import AgentTool
from sellbird.site_facts import site_facts
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.copilot_agent")


class SellbirdCopilotAgent:
    """Tool-using conversational copilot shared by Dashboard & Telegram."""

    def __init__(self, brain, state, config=None, env=None):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    def build_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name="pipeline_summary",
                description="Get current counts of prospects by status, draft campaigns, open conversations, outbox waiting, and sending pause state.",
                parameters={"type": "object", "properties": {}},
                handler=self._tool_pipeline_summary,
            ),
            AgentTool(
                name="analytics_report",
                description="Run funnel diagnostics: campaign reply rates, intent distribution, conversation stages, Bandit framework performance, and prioritized insights.",
                parameters={"type": "object", "properties": {}},
                handler=self._tool_analytics_report,
            ),
            AgentTool(
                name="search_leads",
                description="Search prospects and companies by keyword, status (new/queued/contacted/replied/meeting), or minimum lead score.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Company name, domain, person, or title keyword"},
                        "status": {"type": "string", "description": "Optional prospect status filter"},
                        "min_score": {"type": "integer", "description": "Optional minimum score (0-100)"},
                        "limit": {"type": "integer", "description": "Max items to return (default 10)"},
                    },
                },
                handler=self._tool_search_leads,
            ),
            AgentTool(
                name="inspect_company",
                description="Inspect a company's website facts, tech stack, buying signals, enrichment values, and contacts by company_id or domain.",
                parameters={
                    "type": "object",
                    "properties": {
                        "company_id": {"type": "string"},
                        "domain": {"type": "string"},
                    },
                },
                handler=self._tool_inspect_company,
            ),
            AgentTool(
                name="inspect_conversation",
                description="Inspect full reply thread, intent, and stage for a prospect email or conversation_id.",
                parameters={
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "conversation_id": {"type": "string"},
                    },
                },
                handler=self._tool_inspect_conversation,
            ),
            AgentTool(
                name="draft_angle",
                description="Generate 2 grounded cold-email or reply angle ideas for a specific company domain or topic.",
                parameters={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "goal": {"type": "string"},
                    },
                },
                handler=self._tool_draft_angle,
            ),
            AgentTool(
                name="pause_sending",
                description="Activate the safety kill-switch to pause outbound sending immediately. Use only when the operator explicitly asks to pause.",
                parameters={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Short reason to record"},
                    },
                },
                handler=self._tool_pause_sending,
            ),
        ]

    async def ask(
        self,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        max_steps: int = 5,
    ) -> dict[str, Any]:
        """Answer ``question`` with multi-step tool execution via ``Brain.run_agent``."""
        text = str(question or "").strip()
        if not text:
            return {"ok": False, "answer": "Ask a question about your pipeline, leads, campaigns, or replies.", "steps": [], "tool_calls": 0}

        if self.brain is None:
            return {"ok": False, "answer": "AI brain is not configured.", "steps": [], "tool_calls": 0}

        run_agent = getattr(self.brain, "run_agent", None)
        if not callable(run_agent):
            ans = await self.brain.think(text, session_id="sellbird-copilot", agent="copilot", task="chat")
            return {"ok": bool(ans), "answer": str(ans or ""), "steps": [], "tool_calls": 0}

        prior_lines = []
        for turn in (history or [])[-6:]:
            if isinstance(turn, dict) and turn.get("content"):
                role = str(turn.get("role") or "user").upper()
                prior_lines.append(f"{role}: {str(turn['content'])[:400]}")
        prior_block = f"RECENT CHAT:\n" + "\n".join(prior_lines) + "\n\n" if prior_lines else ""

        product = getattr(self.config, "product", None)
        product_name = getattr(product, "name", "") or "your product"
        goal = f"{prior_block}OPERATOR QUESTION:\n{fence(text, 'operator_message')}\n\n{UNTRUSTED_RULE}"

        system_ctx = (
            f"You are Sellbird's Sales Operations Copilot for {product_name}. "
            "Always inspect real data with tools before quoting numbers, leads, reply rates, or drafts. "
            "Be crisp, direct, and actionable. Note that email approvals and resuming live sending happen in the Dashboard Outbox/Settings."
        )
        return await run_agent(
            goal,
            self.build_tools(),
            system_context=system_ctx,
            max_steps=max_steps,
            session_id="sellbird-copilot",
            agent="copilot",
            task="copilot_chat",
        )

    # ── Tool implementations ──

    async def _tool_pipeline_summary(self) -> dict[str, Any]:
        summary = await self.state.get_state_summary()
        waiting = len(await self.state.get_outbox(status="pending_review", limit=200))
        approved = len(await self.state.get_outbox(status="approved", limit=200))
        return {
            "summary": summary,
            "outbox_pending_review": waiting,
            "outbox_approved_queued": approved,
        }

    async def _tool_analytics_report(self) -> dict[str, Any]:
        analyst = Analyst(self.state, brain=None, config=self.config)
        pipeline = await analyst._pipeline_summary()
        campaigns = await analyst._campaign_performance()
        intents = await analyst._intent_breakdown()
        stages = await analyst._stage_breakdown()
        report = {
            "pipeline": pipeline,
            "campaigns": campaigns[:10],
            "intents": intents,
            "stages": stages,
        }
        report["insights"] = analyst._generate_insights(report)
        try:
            report["framework_stats"] = await self.state.framework_stats()
        except Exception:
            report["framework_stats"] = {}
        return report

    async def _tool_search_leads(
        self,
        query: str = "",
        status: str = "",
        min_score: int = 0,
        limit: int = 10,
    ) -> dict[str, Any]:
        q = str(query or "").strip().lower()
        st = str(status or "").strip().lower()
        cap = max(1, min(25, int(limit or 10)))

        prospects_out = []
        target_statuses = (st,) if st else ("replied", "meeting", "queued", "new", "contacted")
        for s in target_statuses:
            try:
                rows = await self.state.get_prospects_by_status(s)
            except Exception:
                rows = []
            for p in rows:
                if (p.score or 0) < int(min_score or 0):
                    continue
                hay = f"{p.full_name()} {p.email} {p.title} {p.company} {p.industry}".lower()
                if q and q not in hay:
                    continue
                prospects_out.append({
                    "id": p.id,
                    "name": p.full_name(),
                    "email": p.email,
                    "email_status": p.email_status,
                    "title": p.title,
                    "company": p.company,
                    "company_id": p.company_id,
                    "status": p.status,
                    "score": p.score,
                })
        prospects_out.sort(key=lambda item: item.get("score", 0), reverse=True)

        companies_out = []
        try:
            companies = await self.state.get_companies_with_domains(limit=200)
        except Exception:
            companies = []
        for c in companies:
            hay = f"{c.name} {c.domain} {c.industry} {c.location}".lower()
            if q and q not in hay:
                continue
            companies_out.append({
                "id": c.id,
                "name": c.name,
                "domain": c.domain,
                "industry": c.industry,
                "location": c.location,
                "fit": getattr(c, "fit", ""),
                "tech_stack": list(c.tech_stack or [])[:6],
            })
            if len(companies_out) >= cap:
                break

        return {
            "prospects": prospects_out[:cap],
            "companies": companies_out[:cap],
        }

    async def _tool_inspect_company(
        self, company_id: str = "", domain: str = ""
    ) -> dict[str, Any]:
        company = None
        if company_id:
            company = await self.state.get_company(company_id)
        if company is None and domain:
            company = await self.state.get_company_by_domain(domain.strip().lower())
        if company is None:
            return {"error": "Company not found."}

        facts = await site_facts(self.state, company.id, include_booking=True)
        contacts = await self.state.get_contacts_for_company(company.id)
        enrichments = []
        try:
            enrichments = await self.state.get_enrichment_values(company_id=company.id)
        except Exception:
            enrichments = []

        return {
            "id": company.id,
            "name": company.name,
            "domain": company.domain,
            "industry": company.industry,
            "location": company.location,
            "description": company.description,
            "tech_stack": list(company.tech_stack or [])[:12],
            "signals": list(company.signals or [])[:8],
            "site_facts": facts[:10],
            "enrichments": [
                {"column": e.get("column_name") or e.get("column_id"), "value": e.get("value")}
                for e in (enrichments or []) if e.get("value")
            ][:10],
            "contacts": [
                {
                    "id": p.id,
                    "name": p.full_name(),
                    "title": p.title,
                    "email": p.email,
                    "email_status": p.email_status,
                    "status": p.status,
                    "score": p.score,
                }
                for p in contacts[:12]
            ],
        }

    async def _tool_inspect_conversation(
        self, email: str = "", conversation_id: str = ""
    ) -> dict[str, Any]:
        convo = None
        prospect = None
        if conversation_id:
            convo = await self.state.get_conversation(conversation_id)
        if convo is None and email:
            prospect = await self.state.get_prospect_by_email(email.strip().lower())
            if prospect:
                for st in ("open", "needs_human", "closed"):
                    convos = await self.state.get_conversations_by_status(st)
                    convo = next((c for c in convos if c.prospect_id == prospect.id), None)
                    if convo:
                        break
        if convo is None:
            return {"error": "Conversation not found."}
        if prospect is None and convo.prospect_id:
            prospect = await self.state.get_prospect(convo.prospect_id)
        return {
            "conversation_id": convo.id,
            "status": convo.status,
            "stage": convo.stage,
            "intent": convo.intent,
            "prospect": {
                "name": prospect.full_name() if prospect else "",
                "email": prospect.email if prospect else "",
                "company": prospect.company if prospect else "",
            },
            "messages": [
                {"sender": m.sender, "content": m.content[:600]}
                for m in (convo.thread or [])[-10:]
            ],
        }

    async def _tool_draft_angle(self, domain: str = "", goal: str = "") -> dict[str, Any]:
        comp_info: dict[str, Any] = {}
        if domain:
            comp_info = await self._tool_inspect_company(domain=domain)
        product = getattr(self.config, "product", None)
        offer_entry = getattr(getattr(product, "offer", None), "entry", "") or ""
        return {
            "product_name": getattr(product, "name", "") or "",
            "offer": offer_entry,
            "company_context": comp_info,
            "requested_goal": goal,
        }

    async def _tool_pause_sending(self, reason: str = "") -> dict[str, Any]:
        note = (reason or "Paused via Copilot").strip()[:200]
        await self.state.set_setting("sending_paused", note)
        return {"paused": True, "reason": note}
