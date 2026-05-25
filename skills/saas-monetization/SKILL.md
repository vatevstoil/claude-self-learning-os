---
name: saas-monetization
description: "Monetization playbook for the user's SaaS platforms (Facturka.bg, StroyOffice, Cinemind, CasinoScore, Autoagency). Use when the user wants to monetize, grow revenue, set/optimize pricing, improve free→paid conversion, reduce churn, add plans/tiers, design a referral/partner channel, or write outcome-driven pricing copy. Triggers on: 'монетизирам', 'монетизация', 'цени', 'pricing', 'план', 'абонамент', 'conversion', 'churn', 'приходи', 'продай', 'upgrade', 'referral'. Adapts the Build-Sell-Course playbook (sell outcomes not features; diagnose→value→price; Trojan Horse distribution) to product/SaaS monetization. NOT for selling custom AI services as an agency (that's the raw course) — this is for monetizing existing products."
---

# saas-monetization — Monetize the platforms

Turns the Build-Sell-Course selling playbook into an actionable workflow for the
user's **existing SaaS products** (not agency services). Source knowledge:
`{{RESEARCH_PATH}}\Claude Code Resurch\wiki\summaries\Build-Sell-Claude-Code-Course.md`.

## Core reframe (apply to everything)

**Sell outcomes, not features.** Businesses buy **time, money, focus** — never the
tech. Every pricing page, upgrade prompt, and email reframes a feature → an outcome.

| Feature | Outcome |
|---------|---------|
| "Unlimited invoices" | "Issue an invoice in 30s, not 10 min" |
| "Expense scanning" | "Snap a receipt → done. 5h/month back" |

**10× ROI rule:** price ≈ a fraction of the yearly value. Always be able to show the
math (price ÷ value). If a plan saves 5h/mo × €25 = €125/mo value, €20/mo = 6× ROI.

## The 5 monetization levers (diagnose which is the bottleneck first)

Ask for the numbers BEFORE optimizing: active users · free→paid conversion % ·
ARPU · churn. Without metrics, default to **Distribution** (it's growth, not tuning).

### 1. Positioning (the reframe)
- Landing + pricing lead with outcomes + the time/money/focus the product returns.
- Add a timely hook when one exists (e.g. Facturka: "ready for the euro" — urgency converts).

### 2. Pricing & packaging
- Value-based, anchored on what it's worth to that customer segment.
- Tier on the pain points users will pay to remove (gate the *painful* features, not basics).
- Show the ROI math on the pricing page. Annual plans (discount) for cash + retention.

### 3. Conversion (free → paid)
- **Activation first:** new user must reach the "aha" fast (e.g. first invoice <5 min).
- **Gate at the moment of value:** in-app upgrade prompt exactly when they hit a limit
  ("5/5 invoices this month → Pro = unlimited"). Use the project's plan-guard layer.
- Identify which gated features actually drive upgrades; double down on those.

### 4. Distribution (highest leverage — the Trojan Horse)
- Find who already owns the trust of your buyers and partner with them.
- **Facturka example:** accountants serve 10-50 small businesses each. Win the
  accountant (Accountant track + rev-share/free tier for referred clients) → they
  bring their whole book. One accountant ≫ paid ads.
- Generalize: for each platform, ask "who already has my customers' trust?"
  (StroyOffice → construction associations/suppliers; Cinemind → production houses).
- Also: referrals (ask happy users after a results moment), content/SEO.

### 5. Retention & expansion
- Make value visible: monthly "you saved X hours / Y invoices this month".
- Expansion triggers (add 2nd user → Business; add-ons as light retainers).
- Reducing churn 5% often beats acquiring new users.

## Workflow when invoked

1. **Diagnose** — ask for the metrics (users, conversion, ARPU, churn) OR read the
   project's monetization doc if it exists (`<project>/wiki/MONETIZATION.md`).
2. **Pick the lever** with the biggest gap. (No data → Distribution.)
3. **Act** — concrete deliverables: rewrite pricing copy (outcome-driven), design a
   referral/partner mechanism, add in-app upgrade prompts (via plan-guard), build a
   value dashboard, or set up annual plans.
4. **Verify & document** — update `<project>/wiki/MONETIZATION.md`; if a decision,
   add an ADR; save the strategy to Pinecone (`<project>` namespace) for recall.

## Per-project docs

- **Facturka.bg** → `{{WIKI_PATH}}\Fakturka.bg\wiki\MONETIZATION.md` (plans: Free/Pro/
  Business + Accountant track; plan_guard.py + PricingPlans.tsx already exist).
- Other platforms: create `<project>/wiki/MONETIZATION.md` on first use.

## Anti-patterns
- ❌ Listing features instead of outcomes on the pricing page.
- ❌ Pricing on build-cost/hours instead of customer value.
- ❌ Gating basics (kills activation) instead of the painful power-features.
- ❌ Spending on ads before exploiting partner/Trojan-Horse distribution.
- ❌ Optimizing conversion before you know the funnel numbers.
