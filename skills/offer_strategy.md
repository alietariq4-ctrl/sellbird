# Offer Strategy & Closing Mechanics

## Core Rule: Give Before You Ask (Entry Value vs. Paid Pitch)

Never pitch a paid contract, product feature list, or pricing table in a cold first email. Instead, earn the reply by pairing one concrete finding about the prospect with a **free, zero-risk entry offer** (`product.offer.entry`) that helps fix that finding, or a single plain question if no entry offer is configured.

Save the **primary commercial pitch** (paid plans, retainers, custom scopes, and booking links) until after a prospect replies with interest, asks a question, or requests pricing.

## The Offer Ladder

Match what you offer to where the prospect is in the conversation:

| Stage | Signal | Offer |
|-------|--------|-------|
| **Cold First Touch** | Grounded finding from their site, search rank, or job board | Free entry deliverable (`product.offer.entry`) as one yes/no question, or one easy question about the problem |
| **Curious Reply** | Asked a question about how it works | Clear answer + soft next step ("Happy to send an example or walk through it if useful.") |
| **Interested Reply** | Confirmed the pain point or said yes to the entry offer | Deliver the promised entry offer or suggest a short call |
| **Ready to Buy** | Asked about pricing, timeline, or onboarding | Direct pricing answer + clear closing mechanic |

## Crafting Offers from Product Knowledge

When Sellbird learns about a product (via website training or `sellbird.yaml`), distinguish three tiers:

1. **Entry offer (`product.offer.entry`)**: Something genuinely free and useful you can deliver right after a "yes" in cold outreach (a written page-one plan, a homepage mockup, a reorder forecast, a two-week trial). Never a discount or a generic "15-minute demo".
2. **Proof offer**: Concrete evidence for Email 2 or objection replies (a relevant customer result, sample output, or benchmark).
3. **Primary commercial offer**: Paid subscription, service retainer, or implementation package discussed once they engage.

### Offer Construction Framework

For each offer, define:
- **What they get**: A concrete deliverable or outcome, never vague "value"
- **What it costs**: Free for the entry offer; transparent pricing for the primary offer
- **What they risk**: Nothing upfront
- **What happens next**: A simple "yes, send it" reply or picking a time

### Example Offer Structures

**SaaS Product:**
- Entry: "Want me to set up a free 14-day trial with no card needed?"
- Primary: "Plans start at $X/mo. Want to grab 15 minutes to see which tier fits your team?"

**Service / Agency:**
- Entry: "I can send you a written plan for getting into the top three on Google. Want it?"
- Primary: "Retainers start at $X/mo. I can put together a concrete scope once you share your goals."

**Consulting / Operations:**
- Entry: "Want me to run a free reorder forecast on your top 20 SKUs while you hire?"
- Primary: "We run this as a fixed engagement. Would Thursday afternoon work for a 15-minute call?"

## Closing Mechanics: How to Book (Replies Only)

Never put scheduling links in cold Email 1. When an interested reply arrives, follow the configured booking instruction:

### Option 1: Calendar Link
Best when `product.offer.booking_url` is configured:
> "Here is my calendar if you want to grab 15 minutes: [LINK]"

### Option 2: Suggest Specific Times
Best for personal, high-touch conversations or live calendar slots:
> "Would Thursday at 2pm or Friday morning work for a 15-minute call?"

### Option 3: Ask for Their Preference
Best for senior executives:
> "What does your calendar look like this week for a quick 15 minutes?"

### Option 4: Deliver Without a Call
Best when they accepted `product.offer.entry` or a self-serve trial:
> "On it. I will send that over shortly."

## Offer Timing in the Sequence

### Cold Email Sequence (3 emails)
- **Email 1 (Observation + Entry Offer)**: Name one specific finding from the facts, state plainly what it costs them in one sentence, and offer `product.offer.entry` as one yes/no question (or ask one easy question about the problem when no entry offer is set). No product name, no links, 30 to 50 words.
- **Email 2 (Proof Point)**: 3 days later. Share one concrete proof point or customer result from the product info, then repeat the entry offer or suggest a short call. 20 to 40 words, never "following up".
- **Email 3 (Breakup)**: 4 days after Email 2. Close the loop in one or two plain sentences and leave the offer open. 10 to 20 words, no guilt.

### Reply Handling (After They Write Back)
- **Accepted the entry offer**: Thank them briefly, confirm what happens next, and include any handoff deliverable link if generated. Do not pivot to a hard sales pitch.
- **Interested reply**: Acknowledge warmly and propose the configured next step (calendar slots, booking link, or trial).

## Language Rules

**Do say:**
- "Want me to send it over?"
- "Happy to walk you through it."
- "Would Thursday at 2pm work for a 15-minute call?"

**Never say:**
- "Book a demo"
- "Schedule a call with our team"
- "Let me know if you're interested"
- "When are you free?"

## Handling "What's the Price?"

When a prospect asks about pricing in a reply:
1. Give a real number directly and never dodge the question.
2. Add brief context: "Plans start at $X/mo depending on volume."
3. Offer the free entry step or a short call if helpful.
