"""Sellbird's main heartbeat loop."""

import asyncio
import inspect
import logging
import signal
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pytz

from sellbird.brain import Brain
from sellbird.extensions import run_tick_steps
from sellbird.sending_mode import sync_sending_mode
from sellbird.config import (
    ConfigError, load_config, load_env, SellbirdConfig, legacy_sending_held, sending_notice,
    compliance_notice, send_window_of,
)
from sellbird.integrations.mail_provider import NATIVE_PROVIDERS
from sellbird.pidfile import (
    LOCK_FILE, hold_loop_lock, read_pid, release_loop_lock, remove_pid_file, write_pid_file,
)
from sellbird.pipeline import run_profile_stage
from sellbird.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sellbird")


def quiet_http_client_logs():
    """Keep request URLs out of the terminal.

    httpx logs every request at INFO ("HTTP Request: GET https://...") with
    the full query string. Reoon and ZeroBounce only accept their API key as
    a query parameter, so at INFO `sellbird run` would print those keys in
    clear text on every verification. Nothing in those lines is useful to a
    user, so both client loggers sit at WARNING.
    """
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


quiet_http_client_logs()

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900

# Hard ceiling on one cycle's parallel agents. return_exceptions=True on
# gather() only isolates a task that raises — a task that hangs (a socket or
# lock that never resolves) blocks gather() forever, taking the whole
# heartbeat loop down silently with it. This is the only thing standing
# between one stuck agent and Sellbird freezing for hours with no agent
# knowing it's dead — found in production after exactly that happened.
CYCLE_TIMEOUT_SECONDS = 600

# Cap on a single quiet-hours sleep chunk. See the quiet-hours block in
# heartbeat() for why this exists — a system sleep/wake cycle during one
# multi-hour asyncio.wait_for left Sellbird silently stalled well past when
# quiet hours actually ended.
QUIET_HOURS_POLL_SECONDS = 300

# Phase-1 tasks that read replies; sending waits for all of them to succeed.
REPLY_READERS = ("handle_replies", "read_replies")


def in_quiet_hours(config: SellbirdConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def sends_through_quiet_hours(config: SellbirdConfig) -> bool:
    """True when quiet hours pause Sellbird's own work but not cold email
    that is due in its recipient's window.

    With a send window in each recipient's time zone, the operator's night
    can be a recipient's morning. Prospecting and writing still sleep, and
    replies wait for the operator's hours as before. A heartbeat wakes only
    when a cold email can go now (Sender.cold_due_in_window): it reads and
    handles replies first (so stop-on-reply has run; reading a reply is what
    consumes it, so it can't be left for the morning), then runs the sender,
    which stages any draft campaign into the outbox (no model call, nothing
    sent) and sends only cold email. Otherwise it does nothing. Native mail
    providers only.
    """
    email = config.channels.email
    window = send_window_of(config)
    return bool(window is not None and window.recipient_timezone
                and email.enabled and email.provider in NATIVE_PROVIDERS)


def seconds_until_quiet_hours_end(config: SellbirdConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


# When nothing is writable, prospecting pauses for the rest of the day once
# this many unusable prospects (a guessed, invalid or missing email) were
# found in the last 24 hours. Without a working verifier every address is a
# guess, and prospecting every cycle would spend search and verifier credits
# on people Sellbird can never email. Nothing re-checks a guess, so the window
# is a day, never all time: an all-time count would stop Scout for good.
PROSPECT_BACKLOG = 20


def _new_counts(summary: dict) -> tuple[int, int]:
    """(all 'new' prospects, the ones the Writer can draft for)."""
    prospects = summary.get("prospects") or {}
    new = (prospects.get("new", 0) if isinstance(prospects, dict) else 0) or 0
    # A summary built without the writable count keeps the old rule.
    writable = summary.get("writable_prospects", new) or 0
    return new, writable


def unusable_backlog_notice(summary: dict) -> str:
    """Why Sellbird paused prospecting for the day, or ""."""
    _, writable = _new_counts(summary)
    recent = summary.get("recent_unusable_prospects", 0) or 0
    if writable or recent < PROSPECT_BACKLOG:
        return ""
    return (
        f"Sellbird found {recent} prospects in the last day, but none has a verified "
        "or catch-all email, so it paused prospecting for about a day to save search "
        "and verifier credits. Add an email verifier key (REOON_API_KEY, "
        "ZEROBOUNCE_API_KEY or HUNTER_API_KEY in .env) or check it still has credits. "
        "`sellbird export --all` exports the list as it is."
    )


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: SellbirdConfig,
    summary: dict | None = None,
) -> str:
    """Decide what Sellbird should do next based on current state.

    Uses deterministic priority rules (send_campaign > write_campaign >
    prospect > idle) instead of burning a Claude call on a decision that is
    fully derivable from pipeline counts. This saves budget every cycle and
    removes a fragile LLM string-parsing step.

    Replies are not an action here: the handler rides along every cycle
    there is a mailbox or an open conversation to read. Making an open
    conversation the top priority starved everything else for as long as it
    stayed open, and one waiting on the prospect can stay open for weeks.
    """
    if summary is None:
        summary = await state.get_state_summary()

    # Only prospects the Writer can draft for trigger writing: one with a
    # guessed email stays 'new' for good, and counting it starved Scout.
    new_prospects, writable = _new_counts(summary)
    draft_campaigns = summary.get("draft_campaigns", 0) or 0

    # Drafts that can't deploy must not win the cycle: choosing send_campaign
    # for them every cycle would starve the writer and scout forever. That's
    # held Instantly drafts, any draft with the email channel off (the
    # list-building setup), and native drafts while the kill switch is set
    # (the Sender stages nothing until sending resumes).
    email = getattr(getattr(config, "channels", None), "email", None)
    native_paused = (summary.get("sending_paused")
                     and getattr(email, "provider", "") != "instantly")
    held = (legacy_sending_held(config) or native_paused
            or not getattr(email, "enabled", True))
    deployable = 0 if held else draft_campaigns

    if deployable > 0:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif writable > 0:
        action, reason = "write_campaign", f"{writable} new prospect(s) with no drafts ready"
    elif unusable_backlog_notice(summary):
        action, reason = "idle", "a day's worth of prospects with no usable email"
        logger.warning(unusable_backlog_notice(summary))
    else:
        action, reason = "prospect", f"{new_prospects} new prospect(s), none ready to write for"

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _run_agents(tasks: list) -> dict:
    """Run (name, coroutine) pairs in parallel; return {name: outcome}.

    Errors are caught per task so one bad agent never takes down the cycle,
    and the whole batch is bounded so one HUNG agent (no exception, just
    never returns) can't either. A task's outcome is what it returned, or
    the exception it raised; a task cut off by the bound has no outcome.
    """
    if not tasks:
        return {}
    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
            timeout=CYCLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Cycle timed out after {CYCLE_TIMEOUT_SECONDS}s with "
            f"{[t[0] for t in tasks]} still running — cancelled, "
            "moving on to the next heartbeat instead of hanging forever."
        )
        results = []
    outcomes = {}
    for (name, _), result in zip(tasks, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.error(f"Agent {name} failed: {result}", exc_info=result)
        outcomes[name] = result
    return outcomes


def _stop_file() -> Path:
    """Path to the Windows/cross-process graceful stop sentinel."""
    from sellbird.workspace_home import current_home
    return Path(current_home().data_dir) / "sellbird.stop"


def _clear_stop_file() -> None:
    try:
        _stop_file().unlink(missing_ok=True)
    except OSError:
        pass


def _check_stop_sentinel(stop_event: asyncio.Event) -> bool:
    if stop_event.is_set():
        return True
    try:
        if _stop_file().exists():
            logger.info("Stop sentinel detected — finishing active work and shutting down...")
            stop_event.set()
            return True
    except OSError:
        pass
    return False


async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, polling sellbird.stop every 1s and waking on stop_event."""
    if _check_stop_sentinel(stop_event):
        return True
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        chunk = min(1.0, remaining)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=chunk)
            return True
        except asyncio.TimeoutError:
            pass
        if _check_stop_sentinel(stop_event):
            return True
        remaining -= chunk
    return _check_stop_sentinel(stop_event)


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, waking immediately on shutdown or stop sentinel.

    Returns True if a shutdown was requested during the sleep.
    """
    return await _sleep_or_stop(seconds, stop_event)


def _instantiate_optional_agent(cls, *, brain, state, config, env):
    """Instantiate an agent class matching whichever constructor parameters it accepts."""
    sig = inspect.signature(cls)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cls(brain=brain, state=state, config=config, env=env)
    kwargs = {}
    for name, val in (("brain", brain), ("state", state), ("config", config), ("env", env)):
        if name in params:
            kwargs[name] = val
    if kwargs:
        return cls(**kwargs)
    return cls(brain, state, config, env)


async def _has_enrichment_columns(state) -> bool:
    for attr in ("list_enrichment_columns", "get_enrichment_columns"):
        fn = getattr(state, attr, None)
        if callable(fn):
            try:
                res = fn()
                cols = await res if inspect.isawaitable(res) else res
                return bool(cols)
            except Exception:
                return False
    return False


async def _spend_today_usd(state, brain) -> float:
    for obj in (state, brain):
        fn = getattr(obj, "spend_today_usd", None)
        if callable(fn):
            try:
                res = fn()
                val = await res if inspect.isawaitable(res) else res
                return float(val or 0.0)
            except Exception:
                pass
    try:
        totals = await state.usage_totals()
        return float((totals.get("today") or {}).get("cost_usd") or 0.0)
    except Exception:
        return 0.0


async def run_linkedin_touches(state, config, env, limit: int = 10) -> int:
    """Drain due LinkedIn touchpoints (profile views / connection requests).

    Runs only when the channel is on with touchpoints enabled; the
    automation's own persisted daily caps decide how many actually happen.
    A failed touch is recorded and never retried by the loop (the operator
    can see it in the Activity log); a login failure stops the batch —
    retrying it all cycle would just hammer LinkedIn.
    """
    li_cfg = getattr(getattr(config, "channels", None), "linkedin", None)
    if (li_cfg is None or not getattr(li_cfg, "enabled", False)
            or not getattr(li_cfg, "touchpoints", False)):
        return 0
    if not (getattr(env, "linkedin_email", "") or "").strip():
        return 0
    try:
        due = await state.due_linkedin_touches(limit=limit)
    except Exception:
        return 0
    if not due:
        return 0

    from sellbird.integrations.linkedin import LinkedInAutomation

    linkedin = LinkedInAutomation(env.linkedin_email, env.linkedin_password)
    done = 0
    try:
        await linkedin.start()
        if not await linkedin.login():
            logger.warning("LinkedIn touchpoints: login failed; leaving touches queued.")
            return 0
        for touch in due:
            url = str(touch.get("linkedin_url") or "").strip()
            if not url:
                await state.finish_linkedin_touch(touch["id"], error="no profile url")
                continue
            try:
                if touch.get("action") == "connect":
                    ok = await linkedin.send_connection_request(url)
                else:
                    ok = await linkedin.extract_profile(url) is not None
                await state.finish_linkedin_touch(touch["id"], error="" if ok else "action failed")
                done += 1
            except Exception as exc:
                await state.finish_linkedin_touch(touch["id"], error=str(exc))
    finally:
        try:
            await linkedin.stop()
        except Exception:
            pass
    return done


async def _close_brain(brain) -> None:
    if brain is not None and hasattr(brain, "aclose"):
        try:
            res = brain.aclose()
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.debug("brain.aclose failed: %s", e)


def send_hold_reason(unread: list, paused: str, read_problem: str) -> str | None:
    """What to record about this cycle's send phase: why sending is held by
    unread replies, "" when it ran (clear the note), or None to leave the
    note alone (a paused brain has its own Today card)."""
    if paused:
        return None
    if unread:
        return read_problem or "replies couldn't all be read and handled."
    return ""


async def record_send_phase(state, held_reason: str):
    """Note why sending was held this cycle (Today and `sellbird status` show
    it), or clear the note when sending ran. Never stops the loop."""
    try:
        if held_reason:
            await state.note_sends_held(held_reason)
        else:
            await state.clear_sends_held()
    except Exception as e:
        logger.warning(f"Couldn't record the send hold: {e}")


async def heartbeat(stop_event: asyncio.Event | None = None):
    """Sellbird's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    _clear_stop_file()

    logger.info("=" * 60)
    logger.info("Sellbird is online.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return
    env = load_env()
    state = StateManager()
    brain = Brain(state, config.brain)
    try:
        logger.info(
            f"Thinking with {brain.provider_label}"
            + (f" ({brain.model})" if brain.model else "")
            + ". Change brain.provider in sellbird.yaml to switch."
        )
        for notice in (sending_notice(config), compliance_notice(config)):
            if notice:
                logger.warning("=" * 60)
                logger.warning(notice)
                logger.warning("=" * 60)

        await state.init_db()
        logger.info("Database initialized.")

        # Import agents here to avoid circular imports
        from sellbird.agents.scout import Scout
        from sellbird.agents.writer import Writer
        from sellbird.agents.sender import Sender
        from sellbird.agents.handler import Handler
        from sellbird.agents.analyst import Analyst

        scout = Scout(brain, state, config, env)
        writer = Writer(brain, state, config, env)
        sender = Sender(brain, state, config, env)
        handler = Handler(brain, state, config, env)

        analyst_sig = inspect.signature(Analyst)
        analyst_kwargs = {}
        if "config" in analyst_sig.parameters:
            analyst_kwargs["config"] = config
        if "brain" in analyst_sig.parameters:
            analyst_kwargs["brain"] = brain
        analyst = Analyst(state, **analyst_kwargs)

        researcher = None
        try:
            from sellbird.agents.researcher import DeepResearcher
            researcher = _instantiate_optional_agent(
                DeepResearcher, brain=brain, state=state, config=config, env=env
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug("DeepResearcher unavailable: %s", e)

        enricher = None
        try:
            from sellbird.agents.enricher import ClayEnricher
            enricher = _instantiate_optional_agent(
                ClayEnricher, brain=brain, state=state, config=config, env=env
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug("ClayEnricher unavailable: %s", e)

        interval = config.usage.heartbeat_interval_minutes * 60
        max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
        consecutive_errors = 0

        while not stop_event.is_set() and not _check_stop_sentinel(stop_event):
            try:
                # 0. Approval or autopilot, as the config file says now: the
                # dashboard can switch it while Sellbird runs.
                await sync_sending_mode(config, state)

                # 1. Check quiet hours. With send windows in recipients' own time
                # zones, quiet hours only stop prospecting and writing: a cycle
                # still reads replies and sends what's due (sends_through_quiet_hours).
                quiet = in_quiet_hours(config)
                send_only = quiet and sender.is_native and sends_through_quiet_hours(config)
                if quiet and not send_only:
                    sleep_for = seconds_until_quiet_hours_end(config)
                    logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                    shutting_down = False
                    while in_quiet_hours(config):
                        if await _interruptible_sleep(QUIET_HOURS_POLL_SECONDS, stop_event):
                            shutting_down = True
                            break
                    if shutting_down:
                        break
                    continue
                sender.cold_only = send_only
                if send_only and not await sender.cold_due_in_window():
                    logger.info(
                        f"Quiet hours. No cold email can go now; checking again in "
                        f"{config.usage.heartbeat_interval_minutes} minutes."
                    )
                    if await _interruptible_sleep(interval, stop_event):
                        break
                    continue

                # 2. Is the brain usable at all?
                problem = await brain.preflight()
                if problem:
                    logger.error(
                        f"Sellbird is paused: {problem} Nothing was sent or changed. "
                        f"Re-checking in {config.usage.heartbeat_interval_minutes} minutes."
                    )
                    if await _interruptible_sleep(interval, stop_event):
                        break
                    continue

                # 3. Check usage budget
                if not await brain.is_within_budget(
                    max_calls, max_percent=config.usage.max_daily_claude_percent,
                    max_usd=config.usage.max_daily_usd,
                ):
                    if getattr(brain, "transport", None) is not None:
                        spent = await _spend_today_usd(state, brain)
                        logger.info(
                            f"{brain.provider_label} daily USD budget reached "
                            f"(${spent:.4f} / ${config.usage.max_daily_usd:.2f}). "
                            "Sleeping 1h, then re-checking."
                        )
                    else:
                        logger.info(
                            f"{brain.provider_label} usage limit reached "
                            f"({config.usage.max_daily_claude_percent}% of quota or "
                            f"{max_calls} calls). Sleeping 1h, then re-checking."
                        )
                    if await _interruptible_sleep(3600, stop_event):
                        break
                    continue

                if _check_stop_sentinel(stop_event):
                    break

                # 4. Decide what to do (in quiet hours: only read replies and send)
                if send_only:
                    logger.info("Quiet hours: reading replies, then sending what's due "
                                "in recipients' own time zones.")
                    summary = {}
                    action = "quiet_hours_send"
                else:
                    logger.info("Checking pipeline state...")
                    summary = await state.get_state_summary()
                    action = await decide_next_action(brain, state, config, summary=summary)

                if _check_stop_sentinel(stop_event):
                    break

                # 5. Execute — run independent agents in parallel where possible
                tasks = []
                has_open_convos = (summary.get("open_conversations", 0)
                                   + summary.get("auto_reply_handoffs", 0)) > 0

                if action == "prospect":
                    tasks.append(("prospect", scout.run()))
                    tasks.append(("analyze", analyst.run()))
                elif action == "write_campaign":
                    tasks.append(("write_campaign", writer.run()))
                elif action == "idle":
                    tasks.append(("analyze", analyst.run()))

                if action in ("prospect", "idle"):
                    research_cfg = getattr(config, "research", None)
                    research_enabled = getattr(research_cfg, "enabled", True) is not False
                    if researcher is not None and research_enabled:
                        is_cfg = getattr(researcher, "is_configured", None)
                        if not callable(is_cfg) or is_cfg():
                            tasks.append(("deep_research", researcher.run()))
                    if enricher is not None and await _has_enrichment_columns(state):
                        tasks.append(("clay_enrich", enricher.run()))
                    # Competitive watch: idle cycles only, no model calls —
                    # the diff is plain text comparison.
                    from sellbird.collectors.competitor_watch import run_competitor_watch
                    tasks.append(("competitor_watch",
                                  run_competitor_watch(state, config=config, env=env)))

                send_tasks = []
                if action == "send_campaign":
                    send_tasks.append(("send_campaign", sender.run))
                elif sender.is_native:
                    send_tasks.append(("send_outbox", sender.run))
                if sender.is_native or has_open_convos:
                    tasks.append(("handle_replies", handler.run()))

                if summary.get("unprofiled", 0):
                    tasks.append(("profile", run_profile_stage(state, limit=25, config=config)))

                # LinkedIn touchpoints queued by the Sender: a few per cycle,
                # inside the automation's own daily caps.
                tasks.append(("linkedin_touches",
                              run_linkedin_touches(state, config, env)))

                # Two-way Telegram copilot: one poll per cycle for operator
                # commands and button taps (loopback dashboard, so no webhook).
                from sellbird.integrations.copilot import poll_telegram_commands
                tasks.append(("copilot_poll",
                              poll_telegram_commands(state, config=config, env=env)))

                # Inbound CRM sync: pull people marked do-not-contact in
                # Twenty into suppressions (opt-in, Twenty only).
                from sellbird.integrations.crm_sync import TwentyInboundSync
                inbound = TwentyInboundSync(state, config=config, env=env)
                if inbound.is_configured():
                    tasks.append(("crm_inbound", inbound.run()))

                # Outbound CRM sync: push each company's latest signal
                # findings into its Twenty Company record's custom fields
                # (opt-in, Twenty only, operator-named mapping).
                from sellbird.integrations.crm_sync import TwentyObservationSync
                obs_sync = TwentyObservationSync(state, config=config, env=env)
                if obs_sync.is_configured():
                    tasks.append(("crm_observations", obs_sync.run()))

                outcomes = await _run_agents(tasks)

                if _check_stop_sentinel(stop_event):
                    break

                # Phase 2: send — once started, finishes active send cleanly before stopping.
                if send_tasks:
                    unread = [n for n, _ in tasks
                              if n in REPLY_READERS and outcomes.get(n) is not True]
                    paused = brain.paused_reason
                    if paused or unread:
                        reason = paused or "replies couldn't all be read and handled."
                        logger.error(
                            f"Not sending this cycle: {reason} Queued emails wait "
                            "until replies can be read again."
                        )
                        held = send_hold_reason(unread, paused, getattr(handler, "last_read_problem", ""))
                        if held is not None:
                            await record_send_phase(state, held)
                    else:
                        await record_send_phase(state, send_hold_reason([], "", ""))
                        await _run_agents([(name, run()) for name, run in send_tasks])

                # 6. Log the action (best-effort; never kills the loop)
                try:
                    await state.log_action(action_type=action, agent="main")
                except Exception as e:
                    logger.warning(f"Failed to log action '{action}': {e}")

                # 7. Extensions' tick steps (sellbird.extensions).
                await run_tick_steps(config, state)

                consecutive_errors = 0

                if _check_stop_sentinel(stop_event):
                    break

                # 8. Sleep until next heartbeat
                logger.info(
                    f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break

            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(
                    ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                    ERROR_BACKOFF_CAP,
                )
                logger.error(
                    f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                    exc_info=True,
                )
                logger.info(f"Recovering... sleeping {backoff}s before retry.")
                if await _interruptible_sleep(backoff, stop_event):
                    break
    finally:
        await _close_brain(brain)
        _clear_stop_file()

    logger.info("Sellbird shutting down. Deals don't close themselves, but I need a break.")


def _needs_setup() -> bool:
    """Check if Sellbird needs first-time setup."""
    from sellbird.paths import PROJECT_ROOT
    from sellbird.config import _find_config_file, ConfigFileNotFoundError

    env_file = PROJECT_ROOT / ".env"

    # If .env doesn't exist, definitely needs setup
    if not env_file.exists():
        return True

    # sellbird.local.yaml wins over the tracked sellbird.yaml template, same
    # resolution order the real config loader uses (see config.py).
    try:
        config_file = _find_config_file()
    except ConfigFileNotFoundError:
        return True

    try:
        import yaml
        from sellbird.textio import read_user_text
        config = yaml.safe_load(read_user_text(config_file))
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Company", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM/SIGBREAK wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    signals_to_register = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signals_to_register.append(signal.SIGBREAK)

    for sig in signals_to_register:
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback (SIGBREAK runs on a separate OS thread)
            def _make_handler(sig_obj):
                def _handler(s, f):
                    name = signal.Signals(s).name if isinstance(s, int) else sig_obj.name
                    if stop_event.is_set():
                        logger.info("Second shutdown signal — exiting immediately.")
                        raise SystemExit(1)
                    try:
                        loop.call_soon_threadsafe(_request_shutdown, name)
                    except RuntimeError:
                        stop_event.set()
                return _handler

            try:
                signal.signal(sig, _make_handler(sig))
            except (ValueError, OSError):
                pass

    await heartbeat(stop_event)


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        if not sys.stdin.isatty():
            # Started by the dashboard or a service: nobody can answer the wizard.
            print("Sellbird isn't set up yet. Run `sellbird setup` in a terminal first.")
            sys.exit(1)
        print("\n  First time running Sellbird? Let's get you set up.\n")
        from sellbird.setup import run_setup
        asyncio.run(run_setup())
        return

    # One loop at a time: two would both drain the same Outbox. The lock
    # also tells the dashboard the PID file names a live loop.
    lock = hold_loop_lock(LOCK_FILE)
    if lock is None:
        running = read_pid()
        print(f"Sellbird is already running{f' (pid {running})' if running else ''}. "
              "Stop it first, from the dashboard or with Ctrl+C in its terminal.")
        sys.exit(1)
    _clear_stop_file()
    write_pid_file()
    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")
    finally:
        _clear_stop_file()
        remove_pid_file()
        release_loop_lock(lock)


if __name__ == "__main__":
    from sellbird.textio import utf8_stdio
    utf8_stdio()
    main()
