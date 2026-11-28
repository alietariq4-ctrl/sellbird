"""The prospecting pipeline: DISCOVER → PROFILE → (people, verification).

Each stage is independently runnable and independently re-runnable. That is
the point of the design, not an accident of it: profiling can re-run without
re-buying discovery, and discovery can widen without re-profiling everyone.

What chains them is a *query*, not a hand-off. PROFILE asks the database
"which companies have I not looked at, or not looked at lately?" rather than
being handed a list by DISCOVER. So a profile run started for any reason
picks up whatever discovery left behind, a crashed run resumes by itself, and
re-observing on a schedule turns the same signal into a time series.

Segment before you enrich: enrichment cost scales with list size and
conversion does not, so profile the businesses you would actually contact,
not every row you hold.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("sellbird.pipeline")

# How long a profile observation stays fresh. Re-reading a site every quarter
# is what makes "they dropped their agency" a visible event.
PROFILE_STALE_DAYS = 90


@dataclass
class PipelineReport:
    """What a full pass did, stage by stage."""

    discover: dict | None = None
    profiled_companies: int = 0
    profile_observations: int = 0
    profile_run_id: str = ""
    contacts_added: int = 0
    companies_with_contacts: int = 0
    skipped: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discover": self.discover,
            "profiled_companies": self.profiled_companies,
            "profile_observations": self.profile_observations,
            "profile_run_id": self.profile_run_id,
            "contacts_added": self.contacts_added,
            "companies_with_contacts": self.companies_with_contacts,
            "skipped": self.skipped,
            "errors": self.errors,
        }

    def summary_line(self) -> str:
        parts = []
        if self.discover:
            parts.append(f"{self.discover.get('new_companies', 0)} new companies")
        if self.profiled_companies:
            parts.append(f"{self.profiled_companies} profiled "
                         f"({self.profile_observations} observations)")
        if self.contacts_added:
            parts.append(f"{self.contacts_added} contacts extracted")
        return ", ".join(parts) or "nothing to do"


async def run_contact_extraction_stage(
    state,
    config,
    *,
    limit: int = 25,
    newest_first: bool = True,
    concurrency: int = 3,
    timeout_per_company: float = 18.0,
    progress_cb=None,
) -> tuple[int, int]:
    """Extract decision-maker contacts & published inbox emails for uncontacted companies.

    Runs strictly on companies that already have a website/domain in the DB,
    using Scout's $0 team/about/contact page scraper and email verifier.
    Returns (contacts_added, companies_with_contacts).
    """
    import asyncio
    from sellbird.agents.scout import MAX_PROSPECTS_PER_CYCLE, Scout
    from sellbird.config import load_env

    try:
        companies = await state.companies_needing_prospects(
            limit=limit, stale_days=14, newest_first=newest_first,
        )
    except TypeError:
        companies = await state.companies_needing_prospects(limit=limit, stale_days=14)
    if not companies:
        return 0, 0

    scout = Scout(None, state, config, load_env())
    scout._failed_search_engines = set()
    default_industry = config.icp.industries[0] if getattr(config, "icp", None) and config.icp.industries else ""
    total = len(companies)
    contacts_added = 0
    companies_with_contacts = 0
    done = 0
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _extract_one(company: dict) -> None:
        nonlocal contacts_added, companies_with_contacts, done
        async with sem:
            if contacts_added >= MAX_PROSPECTS_PER_CYCLE or await state.get_setting("discovery_paused"):
                return
            domain = (company.get("domain") or "").strip()
            if not domain:
                done += 1
                return
            if await scout._skip_listed_company(company, domain):
                done += 1
                return
            try:
                contacts = await asyncio.wait_for(
                    scout._contacts_from_company(
                        company_id=company["id"],
                        domain=domain,
                        company_name=company.get("name") or scout._domain_to_name(domain),
                        industry=company.get("industry") or default_industry,
                        source="discover_website_extract",
                    ),
                    timeout=timeout_per_company,
                )
            except Exception as exc:
                logger.debug("contact extraction failed/timed out for %s: %s", domain, exc)
                contacts = []
            try:
                await state.mark_prospects_checked(company["id"])
            except Exception as exc:
                logger.debug("mark_prospects_checked failed for %s: %s", domain, exc)

            if contacts and contacts_added < MAX_PROSPECTS_PER_CYCLE:
                added_here = 0
                for prospect in contacts:
                    if contacts_added >= MAX_PROSPECTS_PER_CYCLE:
                        break
                    await state.add_prospect(prospect)
                    contacts_added += 1
                    added_here += 1
                if added_here:
                    companies_with_contacts += 1
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total, contacts_added)
                except Exception:
                    pass

    await asyncio.gather(*(_extract_one(c) for c in companies), return_exceptions=True)
    return contacts_added, companies_with_contacts


async def run_profile_stage(
    state,
    limit: int = 100,
    stale_days: int = PROFILE_STALE_DAYS,
    config=None,
    *,
    newest_first: bool = False,
    concurrency: int = 1,
    progress_cb=None,
) -> tuple[int, int, str]:
    """Profile whatever currently needs it.

    Returns (companies, observations, run_id). Free — three HTTP requests per
    business, no browser, no model call — so it is safe to run on every
    heartbeat without touching the Claude budget. ``config`` says which open
    roles count as hiring intent (``icp.hiring_signals``, else ``icp.titles``).
    """
    from sellbird.collectors.profile import profile_companies

    try:
        pending = await state.companies_needing_profile(
            limit=limit, stale_days=stale_days, newest_first=newest_first,
        )
    except TypeError:
        pending = await state.companies_needing_profile(limit=limit, stale_days=stale_days)
    if not pending:
        return 0, 0, ""

    logger.info("profile: %d companies need a look", len(pending))
    if concurrency > 1 or progress_cb is not None:
        observations, run_id = await profile_companies(
            state, pending, config=config, progress_cb=progress_cb, concurrency=concurrency,
        )
    else:
        observations, run_id = await profile_companies(state, pending, config=config)
    return len(pending), observations, run_id


async def run_prospecting(
    state,
    config,
    provider: str | None = None,
    queries=None,
    max_spend: float = 1.0,
    profile: bool = True,
    profile_limit: int = 200,
    dry_run: bool = False,
) -> PipelineReport:
    """Discover businesses, then read what their websites say about them.

    The two stages are deliberately not coupled through a list: profiling
    queries the database for what needs attention, so it also sweeps up
    anything an earlier run left unfinished.
    """
    from sellbird.collectors.discover import DEFAULT_PROVIDER, run_discovery

    report = PipelineReport()

    discovery = await run_discovery(
        state, config, provider or DEFAULT_PROVIDER, queries,
        max_spend=max_spend, dry_run=dry_run,
    )
    report.discover = discovery.as_dict()
    report.errors.extend(discovery.errors)

    if dry_run:
        report.skipped = "dry run"
        return report
    if not profile:
        report.skipped = "profiling skipped"
        return report

    # Profile runs even when discovery found nothing new: a previous run may
    # have been interrupted, and observations go stale on a schedule.
    try:
        companies, observations, run_id = await run_profile_stage(
            state, limit=profile_limit, config=config)
        report.profiled_companies = companies
        report.profile_observations = observations
        report.profile_run_id = run_id
    except Exception as e:
        logger.exception("profile stage failed")
        report.errors.append(f"profile: {e}")

    return report
