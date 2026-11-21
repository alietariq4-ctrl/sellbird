"""Analyst — tracks performance, diagnoses funnel bottlenecks, and recommends strategic changes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sellbird.state import StateManager

logger = logging.getLogger("sellbird.analyst")

POST_SEND_STATUSES = ("contacted", "replied", "meeting_booked", "meeting", "opted_out", "lost", "closed")


class Analyst:
    """Analyzes campaign performance, reply rates, and ICP segment conversion.

    Runs during idle cycles and writes a report to data/analytics.json.
    """

    def __init__(self, state: StateManager, brain=None, config=None):
        self.state = state
        self.brain = brain
        self.config = config

    @staticmethod
    def _total_contacted(pipeline: dict) -> int:
        return sum(int(pipeline.get(s, 0) or 0) for s in POST_SEND_STATUSES)

    async def run(self):
        """Generate analytics report from current pipeline data."""
        logger.info("Analyst: Running performance analysis...")

        report = {
            "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "pipeline": await self._pipeline_summary(),
            "campaigns": await self._campaign_performance(),
            "intents": await self._intent_breakdown(),
            "stages": await self._stage_breakdown(),
            "insights": [],
        }

        # Generate deterministic insights
        report["insights"] = self._generate_insights(report)

        # Optional AI strategic synthesis when Brain is supplied
        ai_rec = await self._generate_ai_recommendation(report)
        if ai_rec:
            report["ai_recommendation"] = ai_rec
            report["insights"].append(f"AI STRATEGY: {ai_rec}")

        # Write report atomically (temp file + rename) so a crash mid-write
        # never leaves a corrupt analytics.json for the dashboard to choke on.
        data_dir = Path(self.state.db_path).parent
        report_path = data_dir / "analytics.json"
        try:
            tmp_path = report_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            tmp_path.replace(report_path)
            logger.info(f"Analyst: Report written to {report_path}")
        except OSError as e:
            logger.error(f"Analyst: Failed to write report: {e}")

        # Log key metrics
        pipeline = report["pipeline"]
        total = sum(pipeline.values())
        total_contacted = self._total_contacted(pipeline)
        total_replied = int(pipeline.get("replied", 0) or 0) + int(pipeline.get("meeting_booked", 0) or 0) + int(pipeline.get("meeting", 0) or 0)
        if total > 0:
            reply_rate = total_replied / max(total_contacted, 1) * 100
            logger.info(
                f"Analyst: {total} prospects | "
                f"{total_contacted} contacted | "
                f"Reply rate: {reply_rate:.1f}%"
            )

        for insight in report["insights"]:
            logger.info(f"Analyst: Insight — {insight}")

        return report

    async def _pipeline_summary(self) -> dict:
        """Count prospects by status."""
        return await self.state.count_prospects_by_status()

    async def _campaign_performance(self) -> list[dict]:
        """Get stats for each campaign."""
        return await self.state.get_campaign_stats()

    async def _intent_breakdown(self) -> dict:
        """Count replies by intent classification."""
        return await self.state.get_intent_distribution()

    async def _stage_breakdown(self) -> dict:
        """Count conversations by sales stage."""
        return await self.state.get_stage_distribution()

    def _email_provider(self) -> str:
        email_cfg = getattr(getattr(self.config, "channels", None), "email", None)
        return str(getattr(email_cfg, "provider", "") or "").strip().lower()

    async def _generate_ai_recommendation(self, report: dict) -> str:
        """Ask Brain for one concise strategic recommendation when data exists."""
        if self.brain is None:
            return ""
        # Opt-in: skip the model call unless the user enabled AI insights.
        analyst_cfg = getattr(self.config, "analyst", None)
        if not getattr(analyst_cfg, "ai_insights", False):
            return ""
        pipeline = report.get("pipeline") or {}
        if sum(pipeline.values()) == 0:
            return ""
        think = getattr(self.brain, "think", None)
        if not callable(think):
            return ""

        prompt = (
            "You are a B2B outbound sales strategist. Given the funnel metrics below, "
            "provide ONE concise, actionable 1-2 sentence strategic recommendation "
            "(no markdown bullets, no preamble).\n\n"
            f"Pipeline: {json.dumps(pipeline)}\n"
            f"Intents: {json.dumps(report.get('intents') or {})}\n"
            f"Stages: {json.dumps(report.get('stages') or {})}\n"
            f"Campaigns: {json.dumps((report.get('campaigns') or [])[:5])}"
        )
        try:
            ans = await think(
                prompt,
                session_id="sellbird-analyst",
                agent="analyst",
                task="strategic_insights",
            )
            cleaned = " ".join(str(ans or "").strip().split())
            return cleaned[:400] if cleaned else ""
        except Exception as exc:
            logger.debug("Analyst AI recommendation skipped: %s", exc)
            return ""

    def _generate_insights(self, report: dict) -> list[str]:
        """Derive actionable insights from the data.

        Every insight names the metric, the threshold it crossed, and the
        specific file or config to change — so a human (or Sellbird itself)
        can act on it without further analysis.
        """
        insights = []
        pipeline = report.get("pipeline") or {}
        intents = report.get("intents") or {}
        campaigns = report.get("campaigns") or []
        stages = report.get("stages") or {}

        # Pipeline health — compute total_contacted across all post-send statuses
        total_new = int(pipeline.get("new", 0) or 0)
        total_queued = int(pipeline.get("queued", 0) or 0)
        total_contacted = self._total_contacted(pipeline)
        total_replied = (
            int(pipeline.get("replied", 0) or 0)
            + int(pipeline.get("meeting_booked", 0) or 0)
            + int(pipeline.get("meeting", 0) or 0)
        )
        total_opted_out = int(pipeline.get("opted_out", 0) or 0)
        total_interested = int(intents.get("interested", 0) or 0) + int(intents.get("accepted_offer", 0) or 0)
        total_objections = int(intents.get("objection", 0) or 0)
        # Avoid double-counting: an opt-out sets BOTH intent='unsubscribe' and status='opted_out'
        total_unsubscribes = max(int(intents.get("unsubscribe", 0) or 0), total_opted_out)
        total_escalations = int(intents.get("escalate", 0) or 0)

        if total_contacted > 10:
            reply_rate = total_replied / total_contacted * 100
            if reply_rate < 5:
                insights.append(
                    f"ACTION: Reply rate is low ({reply_rate:.1f}%, benchmark 5-10%). "
                    "Rewrite email 1 in prompts/writer.md with a sharper observation, "
                    "and test 2-3 new subject lines via skills/email_frameworks.md."
                )
            elif reply_rate > 15:
                insights.append(
                    f"KEEP: Reply rate is strong ({reply_rate:.1f}%). Don't change the "
                    "current messaging. Scale prospecting to feed it more leads."
                )

        # Deliverability / reputation warning — the most urgent signal
        if total_contacted > 10:
            optout_rate = total_unsubscribes / total_contacted * 100
            if optout_rate > 2:
                insights.append(
                    f"URGENT: Opt-out rate is {optout_rate:.1f}% (>2% risks blacklisting). "
                    "Pause sending, tighten ICP targeting in sellbird.yaml, and reduce "
                    "max_daily_sends until the list quality improves."
                )

        if total_escalations > 0:
            insights.append(
                f"HUMAN NEEDED: {total_escalations} conversation(s) escalated (angry/legal). "
                "Review conversations with status 'needs_human' in the dashboard before "
                "any further sending to those domains."
            )

        # Objection patterns
        total_replies = sum(intents.values()) if intents else 0
        if total_replies > 5:
            objection_pct = total_objections / total_replies * 100
            if objection_pct > 40:
                insights.append(
                    f"ACTION: {objection_pct:.0f}% of replies are objections. Add the "
                    "recurring objections to objection_responses in sellbird.yaml and "
                    "skills/objection_handling.md, or narrow targeting to better-fit accounts."
                )

        # Interest conversion
        if total_interested > 0 and total_contacted > 0:
            interest_rate = total_interested / total_contacted * 100
            insights.append(
                f"Interest rate: {interest_rate:.1f}% of contacted prospects show interest "
                f"({total_interested} of {total_contacted})."
            )

        # Campaign comparison — name winner AND loser so the fix is obvious
        rated = [c for c in campaigns if c.get("reply_rate") is not None]
        if len(rated) >= 2:
            best = max(rated, key=lambda c: c.get("reply_rate", 0))
            worst = min(rated, key=lambda c: c.get("reply_rate", 0))
            if best.get("reply_rate", 0) > 0 and best is not worst:
                insights.append(
                    f"ACTION: '{best.get('name')}' ({best.get('reply_rate')}% replies) beats "
                    f"'{worst.get('name')}' ({worst.get('reply_rate', 0)}%). Reuse the winner's "
                    "angle and subject-line style for the next campaign; pause or rewrite the loser."
                )

        # Pipeline bottlenecks
        if stages.get("engaged", 0) > 3 and stages.get("closing", 0) == 0:
            insights.append(
                "ACTION: Prospects engage but none reach closing. Update prompts/handler.md "
                "CLOSING guidance to propose specific meeting times earlier (by message 2)."
            )
        if total_queued > 0 and total_contacted == 0:
            if self._email_provider() == "instantly":
                insights.append(
                    f"BLOCKED: {total_queued} prospects are queued but nothing has been sent. "
                    "Check the Instantly API key and Sender logs — campaigns may be failing to deploy."
                )
            else:
                insights.append(
                    f"BLOCKED: {total_queued} prospects are queued in the outbox but nothing has been sent yet. "
                    "Approve pending drafts in the Outbox tab or verify your mailbox connection and send window."
                )
        if total_new < 10 and total_queued == 0:
            insights.append(
                f"ACTION: Prospect pool is nearly empty ({total_new} new). Scout should run "
                "next cycle; if it keeps coming up dry, broaden the ICP in sellbird.yaml."
            )

        if not insights:
            insights.append("Not enough data yet. Keep prospecting and sending campaigns.")

        return insights
