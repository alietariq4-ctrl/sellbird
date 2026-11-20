"""What Sellbird has already read off a company's pages, search results, and registries, for the writer.

Turns confirmed observations into grounded fact lines for Writer prompts and
``_has_tip_basis``. Signals checked and found absent (0) are omitted so Writer
only sees true, positive evidence.

``NO_ONLINE_BOOKING`` remains excluded by default (``include_booking=False``)
because late-loading JS booking widgets can be invisible to static HTML scans;
callers checking ``_has_tip_basis`` or explicit booking-audit campaigns can opt
in with ``include_booking=True``.
"""

from __future__ import annotations

# A site nobody has changed for this long reads as neglected; a few weeks doesn't.
STALE_DAYS = 180
# Pages in sitemap.xml at or below this make a very small site.
THIN_PAGES = 5

HEADER = (
    "- Site checks (an automated read of their public pages; it can miss things). "
    "Use at most ONE, only if it fits this kind of business, put it as what you "
    "would do in their place, and never call it an audit or say you went through "
    "the site:"
)


async def site_facts(state, company_id: str, *, include_booking: bool = False) -> list[str]:
    """Fact lines for the writer's prompt, or [] when nothing true is known.

    A failed read is just "nothing known", never a reason not to write.
    """
    if not company_id or state is None:
        return []
    lookup = getattr(state, "get_observations", None)
    if lookup is None:
        return []
    try:
        rows = await lookup(company_id=company_id, latest_only=True, limit=500)
    except Exception:
        return []
    if not rows:
        return []

    by_code: dict[str, dict] = {}
    multi_by_code: dict[str, list[dict]] = {}
    for r in rows:
        code = r.get("signal_code")
        if not code:
            continue
        if code not in by_code:
            by_code[code] = r
        multi_by_code.setdefault(code, []).append(r)

    facts: list[str] = []

    # ── Discovery / Search visibility signals ──
    serp = by_code.get("SERP_RANK")
    if serp and serp.get("value_num") and serp["value_num"] > 0:
        rank = int(serp["value_num"])
        phrase = (serp.get("value_text") or "").strip()
        where = f" for '{phrase}'" if phrase else ""
        facts.append(f"  - They rank #{rank} organically on Google{where}")

    pack = by_code.get("IN_LOCAL_PACK")
    if pack and pack.get("value_num") and pack["value_num"] > 0:
        slot = int(pack["value_num"])
        phrase = (pack.get("value_text") or "").strip()
        where = f" for '{phrase}'" if phrase else ""
        facts.append(f"  - They hold slot #{slot} in Google's local 3-pack{where}")

    maps = by_code.get("MAPS_RANK")
    if maps and maps.get("value_num") and maps["value_num"] > 0:
        mrank = int(maps["value_num"])
        phrase = (maps.get("value_text") or "").strip()
        where = f" for '{phrase}'" if phrase else ""
        facts.append(f"  - They appear at position #{mrank} on Google Maps{where}")

    rating_row = by_code.get("REVIEW_RATING")
    count_row = by_code.get("REVIEW_COUNT")
    rating_val = rating_row.get("value_num") if rating_row else None
    count_val = count_row.get("value_num") if count_row else None
    if rating_val is not None or count_val is not None:
        parts = []
        if rating_val is not None:
            parts.append(f"{rating_val:g}-star rating")
        if count_val is not None:
            parts.append(f"{int(count_val)} review(s)")
        facts.append(f"  - Their business listing shows {', '.join(parts)}")

    unclaimed = by_code.get("UNCLAIMED_LISTING")
    if unclaimed and unclaimed.get("value_num") == 1:
        facts.append("  - Their Google Business Profile appears unclaimed")

    no_site = by_code.get("NO_WEBSITE")
    if no_site and no_site.get("value_num") == 1:
        facts.append("  - Their business listing has no website URL attached")

    # ── Profile / Ad spend / Agency / Hiring signals ──
    gads = by_code.get("RUNNING_GOOGLE_ADS")
    if gads and gads.get("value_num") == 1:
        facts.append("  - Their homepage carries a Google Ads tag (actively running paid search)")

    mads = by_code.get("RUNNING_META_ADS")
    if mads and mads.get("value_num") == 1:
        facts.append("  - Their homepage carries a Meta Pixel (actively running paid social)")

    agency = by_code.get("INCUMBENT_AGENCY")
    if agency and (agency.get("value_text") or "").strip():
        facts.append(f"  - Their site footer credits '{agency['value_text'].strip()}'")

    hiring_rows = [
        r for r in multi_by_code.get("HIRING_ROLE", [])
        if (r.get("value_text") or "").strip() and r.get("value_num") != 0
    ]
    if hiring_rows:
        roles = ", ".join((r.get("value_text") or "").strip() for r in hiring_rows[:3])
        facts.append(f"  - They are hiring for: {roles}")

    open_roles = by_code.get("OPEN_ROLES")
    if open_roles and open_roles.get("value_num") and open_roles["value_num"] > 0:
        n_roles = int(open_roles["value_num"])
        board = (open_roles.get("value_text") or "").strip()
        on_board = f" on {board}" if board else ""
        facts.append(f"  - Their job board lists {n_roles} open role(s){on_board}")

    # ── Technical / SEO / Content signals ──
    no_schema = by_code.get("NO_SCHEMA_MARKUP")
    if no_schema and no_schema.get("value_num") == 1:
        facts.append(
            "  - Their homepage has no JSON-LD structured data (schema.org markup), "
            "so search engines get less to show about them, such as opening hours, "
            "contact details or reviews"
        )

    blog_stale = by_code.get("BLOG_STALE")
    age = blog_stale.get("value_num") if blog_stale else None
    if age is not None and age >= STALE_DAYS:
        facts.append(f"  - The newest date in their sitemap.xml is about {int(age)} days old")

    page_count = by_code.get("SITE_PAGE_COUNT")
    pages = page_count.get("value_num") if page_count else None
    if pages is not None and 0 < pages <= THIN_PAGES:
        facts.append(f"  - Their sitemap.xml lists only {int(pages)} page(s)")

    ai_block = by_code.get("BLOCKS_AI_CRAWLERS")
    if ai_block and ai_block.get("value_num") == 1:
        facts.append(
            "  - Their robots.txt blocks AI crawlers, so AI search answers can't "
            "read their site"
        )

    if include_booking:
        no_booking = by_code.get("NO_ONLINE_BOOKING")
        if no_booking and no_booking.get("value_num") == 1:
            facts.append(
                "  - No embedded online booking widget was detected on their homepage "
                "(a late-loading or subpage scheduler can be missed)"
            )

    # ── Verified OSINT / Registry facts ──
    for reg in multi_by_code.get("REGISTRY_VERIFIED", [])[:4]:
        reg_text = (reg.get("value_text") or "").strip()
        if reg_text:
            facts.append(f"  - Verified public record: {reg_text}")

    return [HEADER] + facts if facts else []
