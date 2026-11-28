"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import asyncio
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sellbird.agents.critic import CopyCritic
from sellbird.brain import Brain
from sellbird.config import EmailRulesError, SellbirdConfig, compliance_of, load_email_rules
from sellbird.email_style import (
    BREAKUP_MAX_WORDS, FIRST_EMAIL_MAX_WORDS, FOLLOW_UP_MAX_WORDS,
    style_problems, tidy, with_sign_off,
)
from sellbird.gate import MAX_SUBJECT_LEN
from sellbird.humanize import humanize, humanizer_enabled
from sellbird.integrations.mail_provider import NATIVE_PROVIDERS
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.state import WRITABLE_EMAIL_STATUSES, StateManager
from sellbird.sending_mode import sync_sending_mode
from sellbird.site_facts import site_facts
from sellbird.startups import startup_facts
from sellbird.untrusted import UNTRUSTED_RULE, fence

# A prospect with nothing to build a tip on (writer.require_tip) is parked here.
NO_TIP_STATUS = "no_tip"

logger = logging.getLogger("sellbird.writer")

# How many times the model may fail to write a usable sequence for a
# prospect before Sellbird stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Which signal_playbook sections a prospect's facts call for, by heading
# prefix and the words in the facts that point to them.
_SIGNAL_SECTIONS = (
    ("### Hiring signal", ("hiring", "job post", "is hiring", "open role", "careers")),
    ("#### Using a job posting", ("hiring", "job post", "open role")),
    ("### Tech-stack signal", ("tools detected", "uses ", "built on", "shopify", "hubspot", "wordpress")),
    ("### Funding signal", ("raised", "funding", "seed", "series a", "series b", "y combinator", "accelerator")),
    ("### New-domain", ("new domain", "registered", "just launched", "new business")),
    ("### Search-rank signal", ("searched", "search", "came up", "ranks", "google", "maps", "local pack")),
)


def _sections(markdown: str) -> dict[str, str]:
    """Each heading line of ``markdown`` mapped to the text under it."""
    out, head, lines = {}, "", []
    for line in markdown.split("\n"):
        if line.startswith("#"):
            if head:
                out[head] = "\n".join(lines).strip()
            head, lines = line.strip(), []
        else:
            lines.append(line)
    if head:
        out[head] = "\n".join(lines).strip()
    return out


# Any merge variable, for checking a rewrite kept the draft's own.
MERGE_VAR = re.compile(r"\{\{\s*\w+\s*\}\}")


def _safe_int(value, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _unwrap_email_dict(result) -> dict | None:
    """Unwrap common nested LLM wrappers like {'email': {'subject': ..., 'body': ...}}."""
    if not isinstance(result, dict):
        return None
    if "subject" in result or "body" in result:
        return result
    for key in ("email", "draft", "result", "data", "message"):
        nested = result.get(key)
        if isinstance(nested, dict) and ("subject" in nested or "body" in nested):
            return nested
    for val in result.values():
        if isinstance(val, dict) and ("subject" in val or "body" in val):
            return val
    return None


def _fill_personal_merge_leaks(text: str, prospect) -> str:
    """Replace any accidentally emitted merge variables in a 1-to-1 personal email."""
    if "{{" not in text:
        return text
    first = getattr(prospect, "first_name", "") or ("there" if getattr(prospect, "is_company_inbox", False) else "")
    replacements = {
        "first_name": first,
        "firstname": first,
        "last_name": getattr(prospect, "last_name", "") or "",
        "lastname": getattr(prospect, "last_name", "") or "",
        "company": getattr(prospect, "company", "") or "",
        "title": getattr(prospect, "title", "") or "",
    }
    for k, v in replacements.items():
        text = re.sub(rf"\{{\{{\s*{k}\s*\}}\}}", v, text, flags=re.IGNORECASE)
    return text


def _names(prospect) -> list[str]:
    """Text that belongs to the prospect (their name, their company), so the
    style rules never read "Elevate Fitness" as an AI tell."""
    return [n for n in (getattr(prospect, "company", ""), getattr(prospect, "first_name", ""),
                        getattr(prospect, "last_name", ""), getattr(prospect, "title", "")) if n]


# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50

# The built-in "how" lines each prompt ends with: email 1 is an observation
# and a question, with no pitch. Operator email rules replace exactly these.
PERSONAL_SHAPE = f"""- 60 words at most, and aim for 30-50. Every word you cut raises the reply rate.
- One specific finding from the FACTS, what it likely costs them in one plain
  sentence, then YOUR OFFER below as the one question. Don't name the product.
- Write the actual text (no merge variables: you know their name/company).
- No sign-off and no name at the end: Sellbird adds the sender's first name.
- Subject: lowercase, 2-5 words."""
SEQUENCE_SHAPE = f"""- Email 1: One specific finding about them, what it costs them, then YOUR OFFER below as
  the one question. {FIRST_EMAIL_MAX_WORDS} words at most, aim for 30-50. Don't name the product.
- Email 2: 3 days later. A different angle: one concrete proof point or result from the
  product info, then the offer again or a short call as the ask. {FOLLOW_UP_MAX_WORDS} words at most, aim for 20-40.
- Email 3: 4 days after that. Close the loop in one or two plain sentences, and leave
  the offer open. {BREAKUP_MAX_WORDS} words at most, aim for 10-20. No guilt.
- No sign-off and no name at the end of any email: Sellbird adds the sender's first name."""
SEQUENCE_STYLE = """- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words."""
# Why the Writer stopped: Today and `sellbird status` show it.
EMAIL_RULES_PROBLEM_KEY = "email_rules_problem"
BUILT_IN_FOOTER = "- No postal address, unsubscribe or opt-out line: Sellbird adds them when it sends."


def operator_rules_block(rules: str) -> str:
    """The operator's email rules as a prompt section ("" when there are none).

    Trusted text from the person running Sellbird, so it isn't fenced. It wins
    over the built-in style and shape rules, never over the FACTS rule."""
    if not rules:
        return ""
    return (
        "\n\nOPERATOR'S EMAIL RULES, written by the person running Sellbird. For\n"
        "length, structure, what to say about the product, links, subject lines\n"
        "and sign-off, these replace the STRICT EMAIL RULES and skills above\n"
        "wherever the two disagree. They never override the FACTS rule: what you\n"
        "say about the prospect comes only from the facts given, never invented.\n"
        "Nor do they allow a fake \"Re:\" or \"Fwd:\" subject line, or a claim of a\n"
        "conversation that never happened.\n"
        + rules
    )


def offer_block(config) -> str:
    """What email 1 offers: ``product.offer.entry``, the free, useful thing you
    can really deliver (an audit, a mockup, a sample report).

    Without one, email 1 can only ask a question, and a question alone gives
    the reader nothing; so the writer falls back to that and says so."""
    offer = getattr(getattr(config, "product", None), "offer", None)
    entry = str(getattr(offer, "entry", "") or "").strip()
    if not entry:
        return ("\n\nYOUR OFFER: none set (product.offer.entry). End email 1 with one easy\n"
                "question about the problem you found instead.")
    return (f"\n\nYOUR OFFER for a first email: {entry}\n"
            "Something free and useful you can really deliver. Email 1 offers it as the one\n"
            "question, tied to what you found about them, in a few plain words. Offer only\n"
            "this, as it's written here: don't say you already made or did something unless\n"
            "it says so, and never promise anything else.")


def footer_rule(config, rules: str) -> str:
    """What the prompt says about the lines Sellbird adds when it sends.

    With operator rules it names only what Sellbird really adds: an opt-out
    sentence the operator writes into the body must not sit on top of the
    configured opt_out_line, and must not be dropped when there is none."""
    if not rules:
        return BUILT_IN_FOOTER
    compliance = compliance_of(config)
    lines = []
    if compliance.postal_address.strip():
        lines.append("- No postal address: Sellbird adds it under the email when it sends.")
    opt_out = compliance.opt_out_line.strip()
    if opt_out:
        lines.append(
            f'- Sellbird adds this opt-out line under the email when it sends: "{opt_out}".\n'
            "  Don't write another way to opt out, even if the operator's rules give one.")
    else:
        lines.append(
            "- Sellbird adds no opt-out line. If the operator's rules give a sentence that\n"
            "  lets the reader say no, write it into the body as they say.")
    return "\n".join(lines)


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: SellbirdConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.skills = ""

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    def _load_writer_skills(self) -> str:
        """Load writer skills and guarantee email_frameworks is included when available."""
        skills = self.brain.load_skills_for_agent("writer") or ""
        load_one = getattr(self.brain, "load_skill", None)
        if callable(load_one):
            try:
                frameworks = load_one("email_frameworks") or ""
            except Exception:
                frameworks = ""
            if isinstance(frameworks, str) and frameworks.strip() and frameworks.strip()[:80] not in skills:
                skills = f"{skills}\n\n{frameworks.strip()}".strip()
        return skills

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")
        self._cached_learning_ctx = None

        # Load foundational skills for this agent
        self.skills = self._load_writer_skills()
        # The offer can change in the dashboard while Sellbird runs: use the
        # one saved now, not the one read at startup. A hosted workspace's
        # config comes from its own store, never a file, so it's left alone.
        from sellbird.workspace_home import current_home
        if not current_home().hosted:
            try:
                from sellbird.config import load_config
                self.config.product.offer = load_config().product.offer
            except Exception:
                pass
        try:
            self.email_rules = load_email_rules(self.config)
        except EmailRulesError as e:
            # The operator replaced the built-in shape on purpose: drafting in
            # it anyway would put emails they didn't want in the outbox.
            logger.warning(
                f"Writer: your email rules (writer.email_rules) can't be used: {e}. "
                "Writing nothing until that's fixed.")
            await self.state.set_setting(EMAIL_RULES_PROBLEM_KEY, str(e))
            return
        if await self.state.get_setting(EMAIL_RULES_PROBLEM_KEY):
            await self.state.delete_setting(EMAIL_RULES_PROBLEM_KEY)

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        # Skip DB-suppressed addresses and domains before any LLM call.
        suppressed_among = getattr(self.state, "suppressed_among", None)
        is_suppressed = getattr(self.state, "is_suppressed", None)
        if callable(suppressed_among) and prospects_with_email:
            try:
                res = suppressed_among([p.email for p in prospects_with_email])
                if inspect.isawaitable(res):
                    res = await res
                suppressed_map = res or {}
                if suppressed_map:
                    prospects_with_email = [
                        p for p in prospects_with_email
                        if p.email.strip().lower() not in suppressed_map
                    ]
            except Exception as e:
                logger.warning(f"Writer: suppressed_among check failed ({e}); holding send/draft batch fail-closed.")
                return
        elif callable(is_suppressed) and prospects_with_email:
            kept = []
            for p in prospects_with_email:
                try:
                    res = is_suppressed(p.email)
                    if inspect.isawaitable(res):
                        res = await res
                    if not res:
                        kept.append(p)
                except Exception as e:
                    logger.warning(f"Writer: is_suppressed failed for {p.email} ({e}); skipping fail-closed.")
            prospects_with_email = kept
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Sort verified-first & highest ICP lead score first so high-value prospects win NATIVE_BATCH_CAP.
        prospects_with_email.sort(
            key=lambda p: (
                0 if str(getattr(p, "email_status", "") or "").lower() == "verified" else 1,
                -int(getattr(p, "score", 0) or 0),
            )
        )

        # channels.email.min_write_score: a prospect scored below it stays
        # 'new' and is never written for. 0 (the default) keeps everyone.
        min_score = self._min_write_score()
        if min_score > 0:
            kept = [p for p in prospects_with_email if (p.score or 0) >= min_score]
            skipped = len(prospects_with_email) - len(kept)
            if skipped:
                logger.info(
                    f"Writer: Skipping {skipped} prospect(s) scored below "
                    f"channels.email.min_write_score ({min_score})."
                )
            prospects_with_email = kept
            if not prospects_with_email:
                logger.info("Writer: No prospects at or above the minimum score.")
                return

        # writer.require_tip: no draft for a prospect with nothing concrete to
        # build its tip on. Parked as "no_tip" rather than left "new", which
        # would make the heartbeat try to write for it every cycle.
        if self._require_tip():
            kept = []
            for p in prospects_with_email:
                if await self._has_tip_basis(p):
                    kept.append(p)
                else:
                    await self.state.update_prospect_status(p.id, NO_TIP_STATUS)
            parked = len(prospects_with_email) - len(kept)
            if parked:
                logger.info(
                    f"Writer: Not writing for {parked} prospect(s) with nothing concrete "
                    f"to build a tip on (writer.require_tip); marked {NO_TIP_STATUS}.")
            prospects_with_email = kept
            if not prospects_with_email:
                return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            self._no_reply = False
            # Phase 4 bandit: pick this batch's framework by Thompson
            # sampling over real reply outcomes, and tell the model to use it.
            framework = ""
            framework_line = ""
            try:
                from sellbird.bandit import (DEFAULT_FRAMEWORKS, choose_framework,
                                             instruction_for, known_frameworks)
                frameworks = known_frameworks(self.config)
                if frameworks != DEFAULT_FRAMEWORKS:
                    stats = await self.state.framework_stats()
                    framework = choose_framework(stats, frameworks)
                    framework_line = instruction_for(framework)
            except Exception as exc:
                logger.debug("Writer: framework selection skipped: %s", exc)
            sequence = await self._write_sequence(prospects, framework_line=framework_line)
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
                framework=framework,
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    def _min_write_score(self) -> int:
        """channels.email.min_write_score, or 0 when the config has none."""
        email = getattr(getattr(self.config, "channels", None), "email", None)
        try:
            return max(0, int(getattr(email, "min_write_score", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _require_tip(self) -> bool:
        """writer.require_tip, or False when the config has none."""
        return bool(getattr(getattr(self.config, "writer", None), "require_tip", False))

    async def _site_facts(self, company_id, *, include_booking: bool = False) -> list[str]:
        """Homepage findings for the prompt, unless writer.site_facts is off."""
        if not getattr(getattr(self.config, "writer", None), "site_facts", True):
            return []
        return await site_facts(getattr(self, "state", None), company_id, include_booking=include_booking)

    async def _has_tip_basis(self, prospect) -> bool:
        """Does the Writer have something concrete to build its one tip on?

        A homepage finding, a startup fact, a buying signal, tech stack, or the
        Scout's research notes. A failed read counts as "nothing known".
        """
        if (prospect.personalization_notes or "").strip():
            return True
        if await self._site_facts(prospect.company_id, include_booking=True):
            return True
        if await self._startup(prospect.company_id):
            return True
        try:
            company = await self.state.get_company(prospect.company_id) if prospect.company_id else None
        except Exception:
            company = None
        return bool(company and (company.signals or company.tech_stack))

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        self._personal_facts_cache = {}
        is_suppressed = getattr(self.state, "is_suppressed", None)
        eligible = []
        for prospect in prospects:
            if callable(is_suppressed) and prospect.email:
                try:
                    res = is_suppressed(prospect.email)
                    if inspect.isawaitable(res):
                        res = await res
                    if res:
                        continue
                except Exception as e:
                    logger.warning(f"Writer: is_suppressed check failed for {prospect.email} ({e}); skipping fail-closed.")
                    continue
            eligible.append(prospect)

        sem = asyncio.Semaphore(3)

        async def _draft_one(prospect):
            async with sem:
                try:
                    return await self._write_personal_email(prospect)
                except Exception as e:
                    logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                    return None

        drafts = await asyncio.gather(*(_draft_one(p) for p in eligible)) if eligible else []
        for prospect, draft in zip(eligible, drafts):
            if draft:
                facts = self._personal_facts_cache.pop(id(prospect), None)
                if facts is None:
                    facts, _ = await self._collect_personal_facts(prospect)
                pending.append((prospect, draft, facts))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d, _ in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        critic = CopyCritic(self.brain, self.config)
        drafted = 0
        for (prospect, _, facts), draft in zip(pending, polished):
            prospect_context = (
                f"Company: {prospect.company}"
                if getattr(prospect, "is_company_inbox", False)
                else f"Name: {prospect.full_name()}, Title: {prospect.title}, Company: {prospect.company}"
            )
            reviewed = await critic.review_and_refine(
                draft,
                prospect_context=prospect_context,
                evidence_facts=facts,
                step=1,
                names=_names(prospect),
                allow_merge_tags=False,
            )
            if not reviewed.get("approved"):
                remaining = reviewed.get("issues") or []
                if any(not str(issue).startswith("style violation:") for issue in remaining):
                    logger.warning(
                        f"Writer: CopyCritic blocked personal draft for {prospect.email}: "
                        + "; ".join(str(i) for i in remaining)
                    )
                    continue
            draft = {"subject": reviewed["subject"], "body": reviewed["body"]}
            draft = await self._keep_to_rules(draft, step=1, names=_names(prospect))
            if draft is None:
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see sellbird.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _signal_guide(self, facts: str) -> str:
        """The parts of skills/signal_playbook.md that fit these facts ("" when
        none do, or the playbook is missing).

        The whole playbook covers hiring, funding, tools, search rank and new
        domains; one prospect has one or two of those, and the rest is noise
        in a prompt for a 40-word email."""
        load = getattr(self.brain, "load_skill", None)
        text = load("signal_playbook") if load else ""
        if not isinstance(text, str) or not text.strip():
            return ""
        sections = _sections(text)
        low = facts.lower()
        wanted = ["## The core rule"]
        for heading, words in _SIGNAL_SECTIONS:
            if any(w in low for w in words):
                wanted += [h for h in sections if h.startswith(heading)]
        if len(wanted) == 1:
            wanted.append("## When you have no real signal")
        body = "\n\n".join(f"{h}\n{sections[h]}" for h in dict.fromkeys(wanted) if h in sections)
        body = body.replace(" \u2014 ", ", ").replace("\u2014", ", ")
        return "\n\nUSING THEIR SIGNAL\n" + body if body else ""

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK: notes the person running Sellbird left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _learning_context(self) -> str:
        """Closed-loop performance context from past campaign reply rates & Analyst insights."""
        cached = getattr(self, "_cached_learning_ctx", None)
        if isinstance(cached, str):
            return cached
        notes: list[str] = []
        try:
            get_stats = getattr(self.state, "get_campaign_stats", None)
            if callable(get_stats):
                campaigns = get_stats()
                if inspect.isawaitable(campaigns):
                    campaigns = await campaigns
                rated = [
                    c for c in (campaigns or [])
                    if isinstance(c, dict) and (c.get("reply_rate") or 0) > 0
                ]
                if rated:
                    best = max(rated, key=lambda c: float(c.get("reply_rate") or 0))
                    fw = f" [{best.get('framework')}]" if best.get("framework") else ""
                    notes.append(
                        f"- Highest-converting campaign so far: '{best.get('name', 'Campaign')}'{fw} "
                        f"at {best.get('reply_rate')}% reply rate. Lean into concise, concrete hooks."
                    )
        except Exception:
            pass

        try:
            db_path = getattr(self.state, "db_path", None)
            if db_path:
                report_path = Path(str(db_path)).parent / "analytics.json"
                if report_path.exists():
                    data = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        ai_rec = str(data.get("ai_recommendation") or "").strip()
                        if ai_rec:
                            notes.append(f"- Strategist guidance: {ai_rec[:260]}")
                        for ins in (data.get("insights") or [])[:2]:
                            s = str(ins or "").strip()
                            if s.startswith(("ACTION:", "KEEP:")) and len(notes) < 3:
                                notes.append(f"- {s[:240]}")
        except Exception:
            pass

        block = (
            "\n\nCLOSED-LOOP LEARNING FROM PAST CAMPAIGNS (optimize style/angle using this, "
            "never invent facts):\n" + "\n".join(notes)
            if notes
            else ""
        )
        self._cached_learning_ctx = block
        return block

    async def _collect_personal_facts(self, prospect) -> tuple[list[str], list[str] | None]:
        """Gather all verified research, site, startup, enrichment, and registry facts."""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        state = getattr(self, "state", None)
        company_id = getattr(prospect, "company_id", "") or ""

        company = None
        if company_id and state is not None:
            try:
                company = await state.get_company(company_id)
            except Exception:
                company = None
        if company:
            if company.location:
                facts.append(f"- Location: {company.location}")
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        facts.extend(await self._site_facts(company_id))
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(company_id)
        if startup is not None:
            facts.extend(startup)

        # Clay AI custom enrichment values for the company
        if state is not None and company_id:
            col_names: dict[str, str] = {}
            get_cols = getattr(state, "get_enrichment_columns", None)
            if callable(get_cols):
                try:
                    cols = get_cols()
                    if inspect.isawaitable(cols):
                        cols = await cols
                    for col in cols or []:
                        if isinstance(col, dict) and col.get("id") and col.get("name"):
                            col_names[str(col["id"])] = str(col["name"]).strip()
                except Exception:
                    pass

            get_vals = getattr(state, "get_enrichment_values", None)
            if callable(get_vals):
                try:
                    try:
                        vals = get_vals(company_id=company_id)
                    except TypeError:
                        vals = get_vals(company_id)
                    if inspect.isawaitable(vals):
                        vals = await vals
                    for row in vals or []:
                        if not isinstance(row, dict):
                            continue
                        row_entity = str(row.get("company_id") or row.get("entity_id") or "").strip()
                        if row_entity and row_entity != company_id:
                            continue
                        if str(row.get("status") or "").strip().lower() in ("failed", "error"):
                            continue
                        raw_val = row.get("value")
                        if raw_val is None:
                            continue
                        val_str = str(raw_val).strip()
                        if not val_str:
                            continue
                        col_name = (
                            str(row.get("column_name") or "").strip()
                            or col_names.get(str(row.get("column_id") or ""), "")
                            or str(row.get("col_key") or row.get("key") or row.get("column_id") or "Enrichment").strip()
                        )
                        line = f"- {col_name}: {val_str}"
                        if line not in facts:
                            facts.append(line)
                except Exception:
                    pass

            # DeepResearcher REGISTRY_VERIFIED observations (deduplicated against existing facts)
            get_obs = getattr(state, "get_observations", None)
            if callable(get_obs):
                try:
                    try:
                        obs_rows = get_obs(
                            company_id=company_id,
                            signal_code="REGISTRY_VERIFIED",
                            latest_only=False,
                            limit=20,
                        )
                    except TypeError:
                        try:
                            obs_rows = get_obs(company_id=company_id, signal_code="REGISTRY_VERIFIED")
                        except TypeError:
                            obs_rows = get_obs(company_id=company_id)
                    if inspect.isawaitable(obs_rows):
                        obs_rows = await obs_rows
                    existing_blob = "\n".join(facts).lower()
                    for obs in obs_rows or []:
                        if not isinstance(obs, dict):
                            continue
                        if str(obs.get("signal_code") or "").strip().upper() != "REGISTRY_VERIFIED":
                            continue
                        val_text = str(obs.get("value_text") or "").strip()
                        if not val_text:
                            continue
                        if val_text.lower() in existing_blob:
                            continue
                        line = f"- Verified public record: {val_text}"
                        if line not in facts:
                            facts.append(line)
                            existing_blob += "\n" + val_text.lower()
                except Exception:
                    pass

        return facts, startup

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        facts, startup = await self._collect_personal_facts(prospect)
        if not hasattr(self, "_personal_facts_cache") or not isinstance(self._personal_facts_cache, dict):
            self._personal_facts_cache = {}
        self._personal_facts_cache[id(prospect)] = facts

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += self._signal_guide("\n".join(facts))
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += await self._feedback_block([prospect.id])
        prompt += await self._learning_context()
        rules = getattr(self, "email_rules", "")
        prompt += offer_block(self.config)
        prompt += operator_rules_block(rules)
        if rules:
            shape = (
                "- Follow the OPERATOR'S EMAIL RULES above for this first email: its length,\n"
                "  structure, what it says about the product, links and subject line.\n"
                "- Write the actual text (no merge variables: you know their name/company).")
            strict = "- Follow every STRICT EMAIL RULE above that the operator's rules don't replace."
        else:
            shape = PERSONAL_SHAPE
            strict = "- Follow every STRICT EMAIL RULE above."

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS: everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it. Do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{footer_rule(self.config, rules)}
{strict}{inbox_rule}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = _unwrap_email_dict(
            await self.brain.think_json(
                prompt, session_id="sellbird-writer",
                agent="writer", task="personal_email",
            )
        )
        if not isinstance(result, dict):
            return None
        subject = _fill_personal_merge_leaks(str(result.get("subject") or "").strip(), prospect)
        body = _fill_personal_merge_leaks(str(result.get("body") or "").strip(), prospect)
        if not subject or not body:
            return None
        return {"subject": subject[:MAX_SUBJECT_LEN], "body": body[:2000]}

    async def _write_sequence(self, prospects: list, *, framework_line: str = "") -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables. Every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += self._signal_guide(prospect_summary)
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])
        prompt += await self._learning_context()
        rules = getattr(self, "email_rules", "")
        prompt += offer_block(self.config)
        prompt += operator_rules_block(rules)
        if rules:
            shape = (
                "- Email 1 is the first touch, email 2 a follow-up 3 days later, email 3 a\n"
                "  last one 4 days after that. Follow the OPERATOR'S EMAIL RULES above for\n"
                "  each one's length, structure, what it says about the product, links and\n"
                "  subject line.")
            style = ""
            strict = ("- Follow every rule in the STRICT EMAIL RULES above that the operator's\n"
                      "  rules don't replace.")
        else:
            shape, style = SEQUENCE_SHAPE, "\n" + SEQUENCE_STYLE
            strict = "- Follow every rule in the STRICT EMAIL RULES above. No exceptions."

        # Bandit: this batch's framework, when one was chosen. It shapes the
        # first email only, and never overrides the strict rules above.
        framework_note = f"\n{framework_line}" if framework_line else ""

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{merge_rule}{style}
{footer_rule(self.config, rules)}
{strict}{framework_note}

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="sellbird-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result)
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        critic = CopyCritic(self.brain, self.config)
        names = [n for p in prospects for n in _names(p)]
        for step, draft in zip(steps, polished):
            reviewed = await critic.review_and_refine(
                draft,
                step=step.step,
                names=names,
                allow_merge_tags=True,
            )
            if reviewed.get("refined"):
                before_vars = set(MERGE_VAR.findall(f"{draft.get('subject', '')}\n{draft.get('body', '')}"))
                after_text = f"{reviewed.get('subject', '')}\n{reviewed.get('body', '')}"
                after_vars = set(MERGE_VAR.findall(after_text))
                if (before_vars and not after_vars) or (inbox_batch and PERSON_VAR.search(after_text)):
                    logger.warning("Writer: CopyCritic repair altered merge variables improperly; keeping pre-critic draft.")
                else:
                    draft = {"subject": reviewed["subject"], "body": reviewed["body"]}
            if not reviewed.get("approved"):
                remaining = reviewed.get("issues") or []
                if any(not str(issue).startswith("style violation:") for issue in remaining):
                    logger.warning(
                        f"Writer: CopyCritic blocked sequence step {step.step}: "
                        + "; ".join(str(i) for i in remaining)
                    )
                    return []
            kept = await self._keep_to_rules(draft, step=step.step, names=names)
            if kept is None:
                # One template goes to the whole batch: a step that can't be
                # brought within the rules fails the sequence, and the batch
                # is tried again next cycle (counted, see MAX_WRITE_ATTEMPTS).
                return []
            step.subject, step.body = kept["subject"], kept["body"]
        return steps

    async def _keep_to_rules(self, draft: dict, *, step: int, names=()) -> dict | None:
        """``draft`` tidied, signed and within Sellbird's writing rules
        (sellbird.email_style), after at most one rewrite; None when even
        the rewrite breaks them.

        Operator email rules replace the built-in length and shape, and the
        sign-off (they may write their own); the always-rules (no dashes, no
        exclamation marks, no AI phrases, no formatting) still hold."""
        shape = not getattr(self, "email_rules", "")

        def finish(d: dict) -> dict:
            body = tidy(str(d.get("body") or ""))
            if shape:
                body = with_sign_off(body, self.config.persona.name)
            return {**d, "subject": tidy(str(d.get("subject") or ""))[:MAX_SUBJECT_LEN], "body": body}

        def problems(d: dict) -> list[str]:
            return style_problems(d["subject"], d["body"], kind="cold", step=step,
                                  names=names, shape=shape)

        draft = finish(draft)
        found = problems(draft)
        if not found:
            return draft
        rewrite = await self._rewrite_to_rules(draft, found, step, shape)
        if rewrite is not None:
            rewrite = finish(rewrite)
            found = problems(rewrite)
            if not found:
                return rewrite
        logger.warning(f"Writer: dropped a draft for email {step} that breaks the rules: "
                       + "; ".join(found))
        return None

    async def _rewrite_to_rules(self, draft: dict, found: list[str], step: int,
                                shape: bool) -> dict | None:
        """One model call that fixes exactly what ``style_problems`` found."""
        body = draft["body"]
        if shape:
            # The sign-off is Sellbird's; the model never sees or writes it.
            first = (self.config.persona.name or "").strip().split(" ")[0]
            if first and body.endswith("\n\n" + first):
                body = body[: -len(first) - 2].rstrip()
        payload = {"subject": draft["subject"], "body": body}
        problems = "\n".join(f"- It {p}." for p in found)
        prompt = f"""This cold email (email {step} of a sequence) breaks Sellbird's writing rules:
{problems}

Rewrite it so it breaks none of them. Keep its facts, its one question and every
merge variable such as {{{{first_name}}}} exactly as written; add no fact, name or claim.
Make it shorter, never longer: cut words instead of adding them. Plain text, no
dash between words (use a period or a comma), no exclamation marks, no sign-off
and no name at the end.

Draft:
{fence(json.dumps(payload, ensure_ascii=False, indent=2), "draft")}

{UNTRUSTED_RULE}

Return ONLY a JSON object: {{"subject": "...", "body": "..."}}"""
        try:
            result = _unwrap_email_dict(
                await self.brain.think_json(
                    prompt, session_id="sellbird-writer", agent="writer", task="fix_draft",
                )
            )
        except Exception as e:
            logger.warning(f"Writer: rewrite to rules failed: {e}")
            return None
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        new_body = str(result.get("body") or "").strip()
        if not subject or not new_body:
            return None
        before = set(MERGE_VAR.findall(payload["subject"] + payload["body"]))
        after = set(MERGE_VAR.findall(subject + new_body))
        if not after <= before or (before and not after):
            return None  # a merge variable appeared from nowhere, or all of them went
        return {**draft, "subject": subject[:MAX_SUBJECT_LEN], "body": new_body[:2000]}

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}, or any dict
        containing a list), missing or wrong-typed fields, and extra keys. Drops
        invalid steps rather than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys, else first list value.
            for key in ("emails", "sequence", "steps", "campaign", "results", "data"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
            else:
                result = next((v for v in result.values() if isinstance(v, list)), None)
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            default_delay = 3 if steps else 0
            delay = max(0, _safe_int(raw.get("delay_days", default_delay), default_delay))
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject[:MAX_SUBJECT_LEN],
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{industry}-{kind}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
