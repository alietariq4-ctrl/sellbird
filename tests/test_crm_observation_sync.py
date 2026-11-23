"""Outbound Twenty CRM observation sync: what Sellbird knows about a
company lands in its Twenty record's custom fields.

The operator names the mapping (crm.observation_fields: signal code →
Twenty field) because custom fields only exist once someone creates them
in Twenty. This covers the contract: off unless enabled, keyed and mapped,
matched by domain, latest value wins, never raises. All fixtures are
fictional.
"""

from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio

from sellbird.config import CrmConfig
from sellbird.integrations.crm_sync import TwentyObservationSync
from sellbird.models.company import Company
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager


def _config(**crm):
    return SimpleNamespace(crm=CrmConfig(**crm))


def _env():
    return SimpleNamespace(twenty_api_key="tw_key", twenty_base_url="https://crm.example")


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "crm.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


async def _seed_company(state, cid, domain):
    await state.add_company(Company(id=cid, name=f"Co {cid}", domain=domain))


# ── the off switch ──


def test_off_unless_enabled_keyed_and_mapped():
    # No config at all.
    assert TwentyObservationSync(None, config=None, env=None).is_configured() is False
    # Enabled + key but no mapping: nothing to write.
    assert TwentyObservationSync(
        None, config=_config(observation_sync_enabled=True), env=_env()
    ).is_configured() is False
    # Mapping but not enabled.
    assert TwentyObservationSync(
        None,
        config=_config(observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(),
    ).is_configured() is False
    # All three.
    assert TwentyObservationSync(
        None,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(),
    ).is_configured() is True


def test_a_bad_mapping_pair_is_dropped_not_fatal():
    cfg = CrmConfig(observation_fields={
        "RUNNING_GOOGLE_ADS": "runsGoogleAds",   # good
        "": "nope",                              # empty code
        "SERP_RANK": "",                         # empty field
        "WEIRD!": "field",                       # bad code shape
        "NO_ONLINE_BOOKING": "has space",         # bad field shape
    })
    assert cfg.observation_fields == {"RUNNING_GOOGLE_ADS": "runsGoogleAds"}


# ── the sync itself ──


@pytest.mark.asyncio
async def test_latest_findings_land_in_the_mapped_fields(state):
    await _seed_company(state, "c1", "acme.example")
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id="c1",
                                value_num=1, collector="profile")
    await state.add_observation("NO_ONLINE_BOOKING", company_id="c1",
                                value_num=1, collector="profile")
    # Unmapped signal: must not be sent.
    await state.add_observation("INCUMBENT_AGENCY", company_id="c1",
                                value_text="Some Agency Group", collector="profile")

    patches = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = unquote(str(req.url))
        if req.method == "GET":
            # Company lookup by exact domain.
            assert "domainName[eq]:acme.example" in url
            return httpx.Response(200, json={
                "data": {"companies": [{"id": "twenty-c1"}]}, "totalCount": 1})
        assert req.method == "PATCH"
        assert url.endswith("/rest/companies/twenty-c1")
        patches.append(json.loads(req.content) if (json := __import__("json")) else {})
        return httpx.Response(200, json={"data": {"company": {"id": "twenty-c1"}}})

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds",
                                           "NO_ONLINE_BOOKING": "noOnlineBooking"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 1
    assert patches == [{"runsGoogleAds": True, "noOnlineBooking": True}]


@pytest.mark.asyncio
async def test_a_text_value_and_a_rank_number_go_through(state):
    await _seed_company(state, "c2", "beta.example")
    await state.add_observation("INCUMBENT_AGENCY", company_id="c2",
                                value_text="Some Agency Group", collector="profile")
    await state.add_observation("SERP_RANK", company_id="c2",
                                value_num=14, value_text="roof repair",
                                collector="serp")

    patches = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={
                "data": {"companies": [{"id": "twenty-c2"}]}, "totalCount": 1})
        patches.append(__import__("json").loads(req.content))
        return httpx.Response(200, json={})

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"INCUMBENT_AGENCY": "currentAgency",
                                           "SERP_RANK": "organicRank"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 1
    # Text wins over number for the agency; the rank is an int.
    assert patches == [{"currentAgency": "Some Agency Group", "organicRank": 14}]


@pytest.mark.asyncio
async def test_a_company_missing_in_twenty_is_skipped(state):
    await _seed_company(state, "c3", "ghost.example")
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id="c3",
                                value_num=1, collector="profile")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"companies": []}, "totalCount": 0})
        raise AssertionError("PATCH must not happen when the lookup found nothing")

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 0


@pytest.mark.asyncio
async def test_a_company_with_no_findings_is_not_patched(state):
    await _seed_company(state, "c4", "quiet.example")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no company has findings; nothing should be called")

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 0


@pytest.mark.asyncio
async def test_a_crash_in_the_crm_never_raises(state):
    await _seed_company(state, "c5", "down.example")
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id="c5",
                                value_num=1, collector="profile")

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("crm is down")

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 0  # never raises


@pytest.mark.asyncio
async def test_companies_without_a_domain_are_never_considered(state):
    await state.add_company(Company(id="c6", name="No Domain Co", domain=""))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id="c6",
                                value_num=1, collector="profile")

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("a company with no domain must not be looked up")

    sync = TwentyObservationSync(
        state,
        config=_config(observation_sync_enabled=True,
                       observation_fields={"RUNNING_GOOGLE_ADS": "runsGoogleAds"}),
        env=_env(), transport=httpx.MockTransport(handler))
    assert await sync.run() == 0
