# Platform and legal rules this tool is built around

This page maps each outside rule to the code that enforces it. Every rule was checked against its primary source on **30 September 2026**. Citations like [ytapi-insert](REFERENCES.md#ytapi-insert) link to [REFERENCES.md](REFERENCES.md), which records what each source says. Platforms change their rules, and the weekly `links` workflow catches dead links but not changed wording. **Re-read the sources before relying on this page.**

> **Not legal advice.** clipwarden enforces conservative defaults. It can't know your contracts, your jurisdiction, or each affiliate program's terms. You remain responsible for what you publish ([yt-paid-promotion](REFERENCES.md#yt-paid-promotion)).

## At a glance

| Rule (source) | What clipwarden does | Where in code | Gate reason(s) |
|---|---|---|---|
| Reused content isn't monetizable without significant original commentary or modification, **even with the creator's permission** ([yt-monetization](REFERENCES.md#yt-monetization)) | Every clip needs owner-written value-add on screen for ≥ 25 % of its length (floor: 15 %, set in code), with ≥ 8 words, and < 60 % of its phrases copied from the transcript | `gate.check_value_add` | `VALUE_ADD_*` |
| "Inauthentic" (formerly "repetitious") content: repetitive or mass-produced, renamed 2025-07-15 ([yt-monetization](REFERENCES.md#yt-monetization)) | Daily cap (default 3, code ceiling 10), max 2 clips per source per day, title-similarity and template-variety checks, near-duplicate detection against your own uploads | `config.HARD_DAILY_CAP`, `gate.check_uniqueness` | `DAILY_CAP_REACHED`, `VARIETY_*`, `DUPLICATE_OF_OWN_UPLOAD` |
| Disclose realistic altered/synthetic content ([yt-altered](REFERENCES.md#yt-altered)); the API exposes `status.containsSyntheticMedia` ([ytapi-insert](REFERENCES.md#ytapi-insert), [ytapi-revisions](REFERENCES.md#ytapi-revisions)) | The flag is set from the rights record or clip plan. **When unsure, disclose** (the demo's synthetic voice is flagged). The gate checks the *actual* request body | `publish.body.synthetic_required`, `gate.check_disclosures` | `SYNTHETIC_DISCLOSURE_MISSING` |
| Paid promotion must be declared with YouTube's paid-promotion checkbox ([yt-paid-promotion](REFERENCES.md#yt-paid-promotion)) | ⚠ **The API can't set it**: `paidProductPlacementDetails` is readable but not a settable property of `videos.insert` or `videos.update` ([ytapi-insert](REFERENCES.md#ytapi-insert), [ytapi-update](REFERENCES.md#ytapi-update)). So paid clips can only be approved as **private**, and the reviewer ticks the box in YouTube Studio before publishing there. The description states the paid promotion in plain words | `approval.decide`, `metadata.assemble_description` | `PAID_PROMOTION_STUDIO_STEP` (warn), `PAID_PROMOTION_DESCRIPTION_MISSING` |
| Paid promotion must be disclosed **in the video itself**, not only in a description or platform checkbox ([ftc-disclosures-101](REFERENCES.md#ftc-disclosures-101), [ftc-endorsement-faq](REFERENCES.md#ftc-endorsement-faq)) | A paid clip needs an on-screen "paid promotion" card naming the sponsor, readable (≥ 2 s) and starting in the first 10 s, or a spoken line that the transcript confirms at the time the plan claims | `gate.check_disclosures`, `media.overlay` | `PAID_PROMOTION_INVIDEO_MISSING` |
| CC BY 4.0 §3(a): name the creator, link the license, say the work was modified ([cc-by-4](REFERENCES.md#cc-by-4)); YouTube only lets you publish under its standard license or CC BY ([yt-cc](REFERENCES.md#yt-cc)) | Full credit with license URL and "edited: clipped, reframed, captioned" in the description, short credit on screen. ShareAlike sources are blocked because the clip can't carry the same license on YouTube. A file not obtained from the rights holder warns | `rights.attribution_line`, `rights.rights_problems`, `gate.check_rights` | `RIGHTS_SHAREALIKE`, `RIGHTS_PLATFORM_COPY` (warn) |
| OAuth only; uploads need an authorized scope such as `youtube.upload` ([ytapi-insert](REFERENCES.md#ytapi-insert), [ytapi-scopes](REFERENCES.md#ytapi-scopes)) | Requests **only** `youtube.upload`. `videos.update` would need the broad `youtube` / `youtube.force-ssl` scope ([ytapi-update](REFERENCES.md#ytapi-update)), so we never update: privacy and `publishAt` go in the single insert call, made *after* approval | `config.PublishConfig.scopes`, `publish.body.apply_decision` | n/a |
| Uploads from unverified API projects created after 2020-07-28 are locked to private ([ytapi-insert](REFERENCES.md#ytapi-insert)) | Documented in [SETUP_YOUTUBE.md](SETUP_YOUTUBE.md). Until your project passes Google's audit, choose "private" and publish from Studio | docs | n/a |
| Quota: `videos.insert` has its own bucket, default 100 calls/day ([ytapi-quota](REFERENCES.md#ytapi-quota)); granular buckets since 2026-06-01 ([ytapi-revisions](REFERENCES.md#ytapi-revisions)) | Low volume by design (the daily cap is far below quota). `quotaExceeded`/`uploadLimitExceeded` defer the clip instead of failing it. Resumable upload with exponential backoff on 5xx | `publish.youtube.upload`, `publish.publisher` | n/a (`deferred_quota` status) |
| Upload clients must let users pick public/private/unlisted ([ytapi-rmf](REFERENCES.md#ytapi-rmf)) | The approval form offers private, unlisted, scheduled-public | `web/templates/clip.html` | n/a |
| Title ≤ 100 characters, description ≤ 5000 bytes, no `<` or `>`, tags ≤ 500 characters ([ytapi-videos](REFERENCES.md#ytapi-videos)) | Schema limits plus a byte-accurate gate check | `models.Metadata`, `gate.check_metadata` | `METADATA_LIMITS` |
| Chapters: first at 0:00, ≥ 3, each ≥ 10 s ([yt-chapters](REFERENCES.md#yt-chapters)) | Chapters are generated only when all three rules hold (never for Shorts) | `metadata.chapters_for` | n/a |
| Shorts: vertical/square up to 3 minutes ([yt-shorts](REFERENCES.md#yt-shorts)) | Clip plans are limited to 5–180 s | `models.ClipPlan` | n/a |

## Affiliate marketing (milestone 3 adds the UI; the gate rules are live now)

What the Federal Trade Commission (FTC) asks, and how it maps to checks:

| FTC guidance | Check |
|---|---|
| Disclose a material connection "clearly and conspicuously" ([cfr-16-255](REFERENCES.md#cfr-16-255) §255.5) | an affiliate plan is required for any clip tagged `affiliate` (`AFFILIATE_PLAN_MISSING`) |
| Put the disclosure **in the video**, not just the description; audio + video together is more noticeable ([ftc-disclosures-101](REFERENCES.md#ftc-disclosures-101); [cfr-16-255](REFERENCES.md#cfr-16-255) §255.0(f)) | spoken (the transcript must contain the disclosure words) or on-screen, starting within the first 10 s, else **block** (`AFFILIATE_START_DISCLOSURE_MISSING`); only one of the two gives a warning (`AFFILIATE_SINGLE_CHANNEL`) |
| Place it with the endorsement ([ftc-disclosures-101](REFERENCES.md#ftc-disclosures-101)) | a disclosure card within ±3 s of every marked recommendation (`AFFILIATE_CARD_MISSING`, `AFFILIATE_RECOMMENDATION_UNMARKED`) |
| Not only behind "more" ([ftc-disclosures-101](REFERENCES.md#ftc-disclosures-101)) | in the description, the disclosure sits **above the first link** (`AFFILIATE_DESCRIPTION_ORDER`). The code builds the description frame, so a model can't reorder it |
| "Affiliate link" alone is not enough; say you earn money ([ftc-endorsement-faq](REFERENCES.md#ftc-endorsement-faq)) | the wording must mention commission, payment or earning (`AFFILIATE_WEAK_WORDING`) |
| Platform tools aren't enough on their own ([ftc-disclosures-101](REFERENCES.md#ftc-disclosures-101)) | we never treat a YouTube checkbox as the disclosure |

Rules of our own (stricter than the law requires; ethical by construction):

- **Only products you evaluated:** every linked product must be in `products_evaluated` (`AFFILIATE_UNEVALUATED_PRODUCT`).
- **No fabricated urgency, reviews or results:** "only 3 left", "selling out", "customers love", "clinically proven", "lose 10 lbs in" and similar block the clip (`AFFILIATE_FABRICATED_CLAIM`). The tool has no feature that generates reviews, testimonials, prices or scarcity.
- **No affiliate content aimed at children:** a made-for-kids clip can't carry affiliate links (`AFFILIATE_KIDS`). YouTube's made-for-kids rules rest on the Children's Online Privacy Protection Act (COPPA) and an FTC agreement ([yt-kids](REFERENCES.md#yt-kids)).
- **No cloaking:** links must match the allowlist exactly. Shorteners, redirect parameters, `user@host` tricks and punycode hosts are refused (`LINK_NOT_ALLOWLISTED`, `LINK_CLOAKED`).
- **Honor each program's terms:** document each program you join in your config notes. Some programs restrict where and how links may appear.

## Markets (financial) content (milestone 4 adds the UI; the gate rules are live now)

| Source | Check |
|---|---|
| Anyone paid by an issuer, underwriter or dealer to publicize a security must disclose the payment **and the amount** (Securities Act §17(b), [usc-15-77q](REFERENCES.md#usc-15-77q)) | a paid securities promotion without payer and amount is blocked (`FINANCE_PAID_UNDISCLOSED`) |
| Red flags of fraud: guaranteed high returns with little risk, pump-and-dump, touting without disclosing pay ([sec-social-media-scams](REFERENCES.md#sec-social-media-scams)) | guaranteed-return and advice language → `FINANCE_ADVICE_LANGUAGE`; hype/urgency patterns ("to the moon", "act now", "next 100x", "penny stock") → `FINANCE_PUMP_PATTERN`, with **no override** |

Also enforced:

- **Educational framing only.** The disclaimer must appear both on screen and in the description (`FINANCE_DISCLAIMER_MISSING`).
- **Positions held must be disclosed** (`FINANCE_POSITION_UNDISCLOSED`).
- **Every number needs a timestamped source.** A number in the title or value-add, or said in the clip, needs a data point with an https source (`FINANCE_UNSOURCED_NUMBER`). Data older than 72 h (configurable) blocks the clip (`FINANCE_STALE_DATA`).
- **Human approval can't be switched off** for markets, affiliate or sponsored clips (`approval.human_required`).

## Corrections to the original project brief

Checking the brief's notes against primary sources turned up these differences:

1. **Paid promotion via API.** The brief assumed the pipeline would set YouTube's paid-promotion setting. The API reference lists `paidProductPlacementDetails` as a readable part but not a settable property, for both insert and update ([ytapi-insert](REFERENCES.md#ytapi-insert), [ytapi-update](REFERENCES.md#ytapi-update)). The design handles this with the private-only rule plus a manual Studio step (above).
2. **Upload quota.** The brief said uploads "reportedly" get 100 calls/day. Confirmed in the official quota page: 100 `videos.insert` calls/day by default, in their own bucket ([ytapi-quota](REFERENCES.md#ytapi-quota)). The bucket arrived 2026-06-01, after a 2025-12-04 cost cut from ~1600 to ~100 units ([ytapi-revisions](REFERENCES.md#ytapi-revisions)).
3. **Testing-mode tokens expire.** This wasn't in the brief. An External OAuth app left in "Testing" status gets refresh tokens that expire after 7 days ([google-oauth-overview](REFERENCES.md#google-oauth-overview)). See [SETUP_YOUTUBE.md](SETUP_YOUTUBE.md).
4. **Sources.** The brief cited two secondary blogs for FTC rules and one for the quota. This page cites the FTC, the eCFR, the statute and Google's own docs instead.

## Untrusted content and AI

We follow the OWASP Top 10 for Large Language Model (LLM) Applications ([owasp-llm-2025](REFERENCES.md#owasp-llm-2025)):

- **LLM01 Prompt Injection.** Transcripts are wrapped in random boundaries, instruction-like phrases are flagged, and a spoken link raises a warning.
- **LLM05 Improper Output Handling.** Model output must match a strict schema, links must match the allowlist, numbers must be grounded in the transcript, and HTML is autoescaped.
- **LLM06 Excessive Agency.** A model can only propose text. It can't approve, publish or change settings.

The default metadata engine uses no model at all. Details: [SECURITY_MODEL.md](SECURITY_MODEL.md).
