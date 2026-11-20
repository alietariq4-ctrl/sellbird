"""Unified tech-stack detection — 100+ SaaS, e-commerce, analytics, ad-pixel, booking & SMB signatures.

Matched case-insensitively against HTML Sellbird already downloads while profiling or
researching a company. Zero extra network requests. Patterns target distinctive script
hosts, DOM markers, and generator tags — anchored on hostname boundaries so lookalike
domains (e.g. ``workforce.com`` vs ``force.com``) never false-positive.
"""

from __future__ import annotations

import re

# name -> list of regexes; any match counts.
FINGERPRINTS: dict[str, list[str]] = {
    # ── CMS, Site Builders & Frontend Frameworks ──
    "WordPress": [r"wp-content/", r"wp-includes/", r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']WordPress'],
    "Elementor": [r"wp-content/plugins/elementor/", r"elementor-frontend"],
    "Divi": [r"wp-content/themes/Divi/", r"et-pb-section"],
    "WP Engine": [r"wpenginepowered\.com", r"wpengine\.com"],
    "Shopify": [r"cdn\.shopify\.com", r"myshopify\.com", r"Shopify\.theme"],
    "Shopify Plus": [r"shopify-plus", r"checkout\.shopifycs\.com"],
    "Wix": [r"static\.parastorage\.com", r"wix\.com/website-builder", r"wixstatic\.com"],
    "Squarespace": [r"static1\.squarespace\.com", r"<!-- This is Squarespace", r"squarespace-cdn\.com"],
    "Webflow": [r"assets\.website-files\.com", r"cdn\.prod\.website-files\.com", r"data-wf-domain"],
    "Framer": [r"framerusercontent\.com", r'name=["\']generator["\'][^>]+content=["\']Framer'],
    "Duda": [r"irp\.cdn-website\.com", r"lirp\.cdn-website\.com", r"multiscreensite\.com"],
    "GoDaddy Website Builder": [r"img1\.wsimg\.com", r"secureservercdn\.net"],
    "Weebly": [r"editmysite\.com", r"weeblycloud\.com"],
    "Drupal": [r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']Drupal', r"/sites/default/files/"],
    "Joomla": [r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']Joomla'],
    "Ghost": [r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']Ghost', r"ghost-sdk"],
    "HubSpot CMS": [r"hs-sites\.com", r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']HubSpot'],
    "Contentful": [r"ctfassets\.net"],
    "Sanity": [r"cdn\.sanity\.io"],
    "Storyblok": [r"a\.storyblok\.com"],
    "Prismic": [r"prismic\.io", r"cdn\.prismic\.io"],
    "Next.js": [r"/_next/static/", r"id=[\"']__NEXT_DATA__[\"']"],
    "Nuxt.js": [r"/_nuxt/", r"window\.__NUXT__"],
    "Gatsby": [r"/page-data/app-data\.json", r'id=["\']___gatsby["\']'],
    "Astro": [r'<astro-island\b', r'name=["\']generator["\'][^>]+content=["\']Astro'],
    "SvelteKit": [r"__sveltekit"],
    "Remix": [r"window\.__remixContext"],
    "Vue.js": [r"vue(?:\.runtime)?(?:\.global)?(?:\.prod)?\.js", r"data-v-[0-9a-f]{6,}"],
    "Angular": [r"ng-version=[\"']\d+"],

    # ── E-Commerce & Payments ──
    "WooCommerce": [r"woocommerce", r"wc-ajax="],
    "BigCommerce": [r"cdn\d*\.bigcommerce\.com"],
    "Magento": [r"mage/cookies", r"Magento_Ui"],
    "PrestaShop": [r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']PrestaShop'],
    "Ecwid": [r"app\.ecwid\.com/script\.js"],
    "Recharge": [r"rechargeassets-bootstrapheroes", r"cdn\.rechargeapps\.com"],
    "Stripe": [r"js\.stripe\.com", r"checkout\.stripe\.com"],
    "PayPal": [r"paypal\.com/sdk/js", r"paypalobjects\.com"],
    "Paddle": [r"cdn\.paddle\.com"],
    "Lemon Squeezy": [r"assets\.lemonsqueezy\.com"],
    "Klarna": [r"x\.klarnacdn\.net", r"js\.klarna\.com"],
    "Afterpay": [r"js\.afterpay\.com", r"static\.afterpay\.com"],
    "Affirm": [r"cdn1\.affirm\.com"],
    "Square": [r"squareup\.com/appointments", r"js\.squareup\.com", r"squarecdn\.com"],

    # ── Analytics, Session Replay & Experimentation ──
    "Google Analytics": [r"googletagmanager\.com/gtag", r"google-analytics\.com/(?:analytics|ga)\.js"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js"],
    "Segment": [r"cdn\.segment\.com/analytics\.js"],
    "PostHog": [r"app\.posthog\.com", r"us\.i\.posthog\.com", r"eu\.i\.posthog\.com", r"posthog-js"],
    "Mixpanel": [r"cdn\.mxpnl\.com"],
    "Amplitude": [r"cdn\.amplitude\.com"],
    "Heap": [r"cdn\.heapanalytics\.com"],
    "FullStory": [r"edge\.fullstory\.com", r"fullstory\.com/s/fs\.js"],
    "LogRocket": [r"cdn\.lr-ingest\.io", r"cdn\.logrocket\.io"],
    "Hotjar": [r"static\.hotjar\.com"],
    "Microsoft Clarity": [r"clarity\.ms/tag/"],
    "Lucky Orange": [r"d10lpsik1i8c69\.cloudfront\.net", r"luckyorange\.com"],
    "Crazy Egg": [r"script\.crazyegg\.com"],
    "Plausible": [r"plausible\.io/js"],
    "Fathom Analytics": [r"cdn\.usefathom\.com"],
    "Matomo": [r"matomo\.js", r"piwik\.js"],
    "VWO": [r"dev\.visualwebsiteoptimizer\.com"],
    "Optimizely": [r"cdn\.optimizely\.com"],
    "Sentry": [r"browser\.sentry-cdn\.com", r"js\.sentry-cdn\.com"],
    "Datadog RUM": [r"datadoghq-browser-agent\.com"],

    # ── Ad Pixels & Paid Acquisition ──
    "Google Ads": [r"googleadservices\.com/pagead/conversion", r"googletagmanager\.com/gtag/js\?id=AW-"],
    "Meta Pixel": [r"connect\.facebook\.net/[^\"']*fbevents\.js"],
    "LinkedIn Insight Tag": [r"snap\.licdn\.com/li\.lms-analytics"],
    "TikTok Pixel": [r"analytics\.tiktok\.com/i18n/pixel"],
    "Pinterest Tag": [r"s\.pinimg\.com/ct/core\.js"],
    "Reddit Pixel": [r"alb\.reddit\.com/snooze\.gif", r"rdt\(['\"]init"],
    "Microsoft Advertising": [r"bat\.bing\.com/bat\.js"],

    # ── CRM, Marketing Automation & Reviews ──
    "HubSpot": [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"js\.hscollectedforms\.net", r"hubspot\.com/api"],
    "Salesforce": [
        r"(?:^|[\s\"'/:.])(?:my\.)?salesforce\.com",
        r"(?:^|[\s\"'/:.])force\.com",
    ],
    "Pardot": [r"pi\.pardot\.com", r"go\.pardot\.com"],
    "Marketo": [r"munchkin\.marketo\.net", r"app-[a-z0-9]+\.marketo\.com"],
    "Mailchimp": [r"chimpstatic\.com", r"list-manage\.com/subscribe"],
    "Klaviyo": [r"static\.klaviyo\.com"],
    "ActiveCampaign": [r"trackcmp\.net", r"activehosted\.com"],
    "Customer.io": [r"assets\.customer\.io", r"track\.customer\.io"],
    "Braze": [r"js\.appboycdn\.com"],
    "Iterable": [r"js\.iterable\.com"],
    "Brevo": [r"sibautomation\.com", r"sibforms\.com"],
    "ConvertKit": [r"f\.convertkit\.com", r"ck\.page"],
    "Drip": [r"tag\.getdrip\.com"],
    "Omnisend": [r"omnisnippet1\.com"],
    "Attentive": [r"cdn\.attn\.tv"],
    "Yotpo": [r"staticw2\.yotpo\.com"],
    "Judge.me": [r"cdn\.judge\.me"],
    "Trustpilot": [r"widget\.trustpilot\.com"],

    # ── Live Chat, Support & Conversational Sales ──
    "Intercom": [r"widget\.intercom\.io", r"js\.intercomcdn\.com"],
    "Drift": [r"js\.driftt\.com"],
    "Zendesk": [r"static\.zdassets\.com", r"zendesk\.com/embeddable"],
    "Freshchat": [r"wchat\.freshchat\.com"],
    "Crisp": [r"client\.crisp\.chat"],
    "Tidio": [r"code\.tidio\.co"],
    "LiveChat": [r"cdn\.livechatinc\.com"],
    "Gorgias": [r"config\.gorgias\.chat"],
    "Help Scout": [r"beacon-v2\.helpscout\.net"],
    "Front Chat": [r"chat-assets\.frontapp\.com"],
    "tawk.to": [r"embed\.tawk\.to"],
    "Qualified": [r"js\.qualified\.com"],

    # ── Scheduling, Forms & Conversion ──
    "Calendly": [r"assets\.calendly\.com", r"calendly\.com/[\w\-]+/"],
    "Cal.com": [r"app\.cal\.com/embed", r"(?:^|[\s\"'/:.])cal\.com/[\w\-]+/"],
    "Chili Piper": [r"js\.chilipiper\.com"],
    "Acuity Scheduling": [r"acuityscheduling\.com", r"squarespacescheduling\.com"],
    "Typeform": [r"embed\.typeform\.com"],
    "Tally": [r"tally\.so/widgets/embed\.js"],
    "Jotform": [r"form\.jotform\.com", r"cdn\.jotfor\.ms"],

    # ── Local SMB, Home Services, Healthcare & Hospitality ──
    "ServiceTitan": [r"(?:^|[\s\"'/:.])servicetitan\.com", r"go\.servicetitan\.com", r"webscheduler\.servicetitan\.com"],
    "Jobber": [r"clienthub\.getjobber\.com", r"(?:^|[\s\"'/:.])getjobber\.com"],
    "Housecall Pro": [r"book\.housecallpro\.com", r"(?:^|[\s\"'/:.])housecallpro\.com"],
    "Workiz": [r"booking\.workiz\.com", r"(?:^|[\s\"'/:.])workiz\.com"],
    "Podium": [r"connect\.podium\.com"],
    "Birdeye": [r"(?:^|[\s\"'/:.])birdeye\.com"],
    "CallRail": [r"cdn\.callrail\.com"],
    "WhatConverts": [r"s\.ksrndkehqnwntyxlhgto\.com", r"whatconverts\.com"],
    "Mindbody": [r"widgets\.mindbodyonline\.com", r"clients\.mindbodyonline\.com"],
    "Vagaro": [r"(?:^|[\s\"'/:.])vagaro\.com"],
    "Toast": [r"toasttab\.com"],
    "OpenTable": [r"opentable\.com/widget", r"opentable\.com/r/"],
    "Jane App": [r"\.janeapp\.com"],
    "SimplePractice": [r"clientportal\.simplepractice\.com"],
    "NexHealth": [r"nexhealth\.com"],
    "Zocdoc": [r"zocdoc\.com/practice", r"offsite\.zocdoc\.com"],
    "AppFolio": [r"\.appfolio\.com"],
    "Buildium": [r"\.managebuilding\.com"],

    # ── Infrastructure, CDN, Security & Consent ──
    "Cloudflare": [r"/cdn-cgi/"],
    "Vercel": [r"\.vercel\.app", r"x-vercel", r"_vercel/insights"],
    "Netlify": [r"\.netlify\.app", r"netlify-identity-widget"],
    "Cloudflare Turnstile": [r"challenges\.cloudflare\.com/turnstile"],
    "OneTrust": [r"cdn\.cookielaw\.org"],
    "Cookiebot": [r"consent\.cookiebot\.com"],
}

_compiled: dict[str, list[re.Pattern]] | None = None


def _get_compiled() -> dict[str, list[re.Pattern]]:
    global _compiled
    if _compiled is None:
        _compiled = {
            name: [re.compile(p, re.IGNORECASE) for p in patterns]
            for name, patterns in FINGERPRINTS.items()
        }
    return _compiled


def detect_tech(html: str, max_bytes: int = 300_000) -> list[str]:
    """Return the tools detectable in a page's HTML, alphabetized."""
    if not html:
        return []
    sample = html[:max_bytes]
    found = []
    for name, patterns in _get_compiled().items():
        if any(p.search(sample) for p in patterns):
            found.append(name)
    return sorted(found)
