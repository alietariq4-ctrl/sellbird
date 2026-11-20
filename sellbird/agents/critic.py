"""Copywriting & Fact Critic Agent — deterministic + LLM draft verification & repair.

Runs fast zero-cost checks for spam triggers, leaked merge tags / placeholders,
style violations, and ungrounded factual hallucinations against the prospect's
evidence context, invoking an LLM repair pass only when issues are detected.
"""

from __future__ import annotations

import json
import logging
import re

from sellbird.email_style import style_problems, tidy
from sellbird.gate import BANNED_PHRASES, MERGE_TAG_RE, URL_RE, MAX_LINKS
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.critic")

PLACEHOLDER_BRACKET_RE = re.compile(
    r"\[(?:first\s*name|last\s*name|name|company|company\s*name|your\s*name|title|link|insert[^\]]*)\]",
    re.IGNORECASE,
)
DOLLAR_CLAIM_RE = re.compile(r"[$€£]\s?\d[\d,.]*\s?(?:k|m|mm|b|bn|million|billion)?\b", re.IGNORECASE)
FUNDING_CLAIM_RE = re.compile(r"\b(?:series\s+[a-f]|seed\s+round|pre-seed|raised\s+[$€£\d])\b", re.IGNORECASE)
RANK_CLAIM_RE = re.compile(
    r"(?:\b(?:\d+(?:st|nd|rd|th))\s+on\s+google\b|\bpage\s+(?:two|2|three|3)\s+of\s+google\b|\branks?\s+#?\d+\b)",
    re.IGNORECASE,
)
FABRICATED_RAPPORT_RE = re.compile(
    r"\b(?:saw\s+your\s+(?:recent\s+)?(?:linkedin\s+)?post|loved\s+your\s+(?:recent\s+)?(?:article|podcast|interview)|mutual\s+connection|friend\s+of\s+mine\s+mentioned)\b",
    re.IGNORECASE,
)


def _unwrap_email_dict(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if "subject" in payload or "body" in payload:
        return payload
    for key in ("email", "draft", "revised", "result", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict) and ("subject" in nested or "body" in nested):
            return nested
    return None


class CopyCritic:
    """Reviews outbound cold email drafts for spam, style, merge leaks, and hallucinations."""

    def __init__(self, brain=None, config=None):
        self.brain = brain
        self.config = config

    def inspect_draft(
        self,
        draft: dict,
        *,
        prospect_context: str = "",
        evidence_facts: list[str] | None = None,
        step: int = 1,
        names: list[str] | tuple[str, ...] = (),
        allow_merge_tags: bool = False,
    ) -> list[str]:
        """Run deterministic spam, placeholder, style, and fact-grounding checks."""
        issues: list[str] = []
        subject = str((draft or {}).get("subject") or "").strip()
        body = str((draft or {}).get("body") or "").strip()

        if not subject:
            issues.append("missing subject line")
        if not body:
            issues.append("missing email body")
        if not subject or not body:
            return issues

        combined = f"{subject}\n{body}"
        lowered = combined.lower()

        if not allow_merge_tags:
            tags = MERGE_TAG_RE.findall(combined)
            if tags:
                issues.append(f"contains unrendered merge tags: {sorted(set(tags))[:3]}")

        brackets = PLACEHOLDER_BRACKET_RE.findall(combined)
        if brackets:
            issues.append(f"contains unfilled bracket placeholders: {sorted(set(brackets))[:3]}")

        banned = [p for p in BANNED_PHRASES if p in lowered]
        if banned:
            issues.append(f"contains spam/AI-tell phrases: {banned[:3]}")

        links = URL_RE.findall(body)
        if len(links) > MAX_LINKS:
            issues.append(f"contains too many links ({len(links)} > {MAX_LINKS})")

        has_custom_rules = bool(getattr(getattr(self.config, "writer", None), "email_rules", ""))
        style = style_problems(
            subject,
            body,
            kind="cold",
            step=step,
            names=names,
            shape=not has_custom_rules,
        )
        for prob in style:
            issues.append(f"style violation: {prob}")

        # Fact-grounding checks when prospect evidence context is supplied
        has_evidence_scope = bool((prospect_context or "").strip() or evidence_facts)
        if has_evidence_scope:
            product_pricing = str(getattr(getattr(self.config, "product", None), "pricing", "") or "")
            product_desc = str(getattr(getattr(self.config, "product", None), "description", "") or "")
            offer_entry = str(getattr(getattr(getattr(self.config, "product", None), "offer", None), "entry", "") or "")
            evidence_blob = "\n".join([
                prospect_context or "",
                *(evidence_facts or []),
                product_pricing,
                product_desc,
                offer_entry,
            ]).lower()

            if FUNDING_CLAIM_RE.search(combined) and not any(
                w in evidence_blob for w in ("raised", "funding", "series ", "seed", "round", "combinator")
            ):
                issues.append("mentions a funding round not grounded in prospect facts")

            for money in DOLLAR_CLAIM_RE.findall(combined):
                norm_money = re.sub(r"\s+", "", money.lower())
                if norm_money not in re.sub(r"\s+", "", evidence_blob):
                    issues.append(f"mentions unverified monetary figure '{money}' not in facts")
                    break

            if RANK_CLAIM_RE.search(combined) and not any(
                w in evidence_blob for w in ("rank", "google", "maps", "local pack", "serp", "search")
            ):
                issues.append("claims a Google search rank not grounded in prospect facts")

            if FABRICATED_RAPPORT_RE.search(combined):
                m = FABRICATED_RAPPORT_RE.search(combined)
                phrase = m.group(0) if m else "social post"
                if phrase.lower() not in evidence_blob:
                    issues.append(f"fabricates personal rapport ('{phrase}') not in facts")

        return issues

    async def review_and_refine(
        self,
        draft: dict,
        *,
        prospect_context: str = "",
        evidence_facts: list[str] | None = None,
        step: int = 1,
        names: list[str] | tuple[str, ...] = (),
        allow_merge_tags: bool = False,
    ) -> dict:
        """Inspect ``draft`` and optionally run an LLM repair pass if issues are found."""
        base_subject = tidy(str((draft or {}).get("subject") or ""))
        base_body = tidy(str((draft or {}).get("body") or ""))
        normalized = {**(draft or {}), "subject": base_subject, "body": base_body}

        issues = self.inspect_draft(
            normalized,
            prospect_context=prospect_context,
            evidence_facts=evidence_facts,
            step=step,
            names=names,
            allow_merge_tags=allow_merge_tags,
        )
        if not issues:
            return {**normalized, "approved": True, "issues": [], "refined": False}

        if self.brain is None:
            return {**normalized, "approved": False, "issues": issues, "refined": False}

        facts_text = "\n".join([
            *([prospect_context.strip()] if (prospect_context or "").strip() else []),
            *(evidence_facts or []),
        ]).strip()

        prompt = f"""You are a strict cold-email copywriting & fact-checking critic.
Fix every issue listed below while keeping the email concise, natural, and 100% faithful to the verified facts.

DETECTED ISSUES:
{chr(10).join(f"- {i}" for i in issues)}

VERIFIED FACTS (never invent anything outside these):
{fence(facts_text or "Use only claims already grounded in the draft; remove any unverified numbers, posts, or merge tags.", "verified_facts")}

CURRENT DRAFT:
{fence(json.dumps({"subject": base_subject, "body": base_body}, ensure_ascii=False, indent=2), "draft")}

{UNTRUSTED_RULE}

RULES:
- Plain text only, no dashes between words, no exclamation marks, no spam hype, no unfilled placeholders.
- Keep under 55 words for email 1; lowercase 2-5 word subject line.
- Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        try:
            repaired_raw = await self.brain.think_json(
                prompt,
                session_id="sellbird-critic",
                agent="critic",
                task="review_draft",
            )
        except Exception as exc:
            logger.warning("CopyCritic LLM repair failed: %s", exc)
            return {**normalized, "approved": False, "issues": issues, "refined": False}

        unwrapped = _unwrap_email_dict(repaired_raw)
        if not unwrapped:
            return {**normalized, "approved": False, "issues": issues, "refined": False}

        new_subject = tidy(str(unwrapped.get("subject") or ""))
        new_body = tidy(str(unwrapped.get("body") or ""))
        if not new_subject or not new_body:
            return {**normalized, "approved": False, "issues": issues, "refined": False}

        candidate = {**normalized, "subject": new_subject[:120], "body": new_body[:2000]}
        remaining = self.inspect_draft(
            candidate,
            prospect_context=prospect_context,
            evidence_facts=evidence_facts,
            step=step,
            names=names,
            allow_merge_tags=allow_merge_tags,
        )
        return {
            **candidate,
            "approved": not remaining,
            "issues": remaining,
            "refined": True,
        }
