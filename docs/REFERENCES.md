# References

Generated from `docs/references.json` by `scripts/check_refs.py --write`. Every entry was opened and
checked against the claim it supports on the access date shown. Primary sources only (official documentation,
regulators, statutes). Notes paraphrase what each source says; quotes are short and attributed.

<a id="cfr-16-255"></a>
**[cfr-16-255]** US Federal Trade Commission. *Guides Concerning the Use of Endorsements and Testimonials in Advertising, 16 CFR Part 255* (2023-07-26). <https://www.ecfr.gov/current/title-16/chapter-I/subchapter-B/part-255> (accessed 2026-09-30).

> Section 255.5: material connections must be disclosed clearly and conspicuously; 255.0(f): a disclosure in both visual and audible portions is more likely clear and conspicuous.

<a id="ftc-disclosures-101"></a>
**[ftc-disclosures-101]** US Federal Trade Commission. *Disclosures 101 for Social Media Influencers*. <https://www.ftc.gov/business-guidance/resources/disclosures-101-social-media-influencers> (accessed 2026-09-30).

> Put the disclosure with the endorsement; in the video, not just the description; audio and video together is more noticeable; don't rely only on a platform's disclosure tool.

<a id="ftc-endorsement-faq"></a>
**[ftc-endorsement-faq]** US Federal Trade Commission. *FTC's Endorsement Guides: What People Are Asking*. <https://www.ftc.gov/business-guidance/resources/ftcs-endorsement-guides-what-people-are-asking> (accessed 2026-09-30).

> Affiliate links need disclosure; 'affiliate link' alone is not adequate; example wording: 'I get commissions for purchases made through links in this post.'

<a id="google-2sv"></a>
**[google-2sv]** Google Account Help. *Turn on 2-Step Verification*. <https://support.google.com/accounts/answer/185839?hl=en> (accessed 2026-09-30).

> Google Account > Security & sign-in > 'Turn on 2-Step Verification'; passkeys and hardware security keys are recommended because they resist phishing.

<a id="google-consent"></a>
**[google-consent]** Google for Developers. *Configure the OAuth consent screen and choose scopes*. <https://developers.google.com/workspace/guides/configure-oauth-consent> (accessed 2026-09-30).

> Menu > Google Auth platform > Branding, then Audience (Internal/External, test users) and Data Access (scopes).

<a id="google-oauth-native"></a>
**[google-oauth-native]** Google for Developers. *OAuth 2.0 for iOS & Desktop Apps*. <https://developers.google.com/identity/protocols/oauth2/native-app> (accessed 2026-09-30).

> PKCE with S256 code challenge; loopback redirect http://127.0.0.1:port for desktop apps; state parameter.

<a id="google-oauth-overview"></a>
**[google-oauth-overview]** Google for Developers. *Using OAuth 2.0 to Access Google APIs (refresh token expiration)*. <https://developers.google.com/identity/protocols/oauth2> (accessed 2026-09-30).

> External apps in 'Testing' publishing status get refresh tokens that expire in 7 days; limit of 100 refresh tokens per Google Account per OAuth client ID (oldest silently invalidated).

<a id="google-oauth-web"></a>
**[google-oauth-web]** Google for Developers. *Using OAuth 2.0 for Web Server Applications (token revocation, incremental authorization)*. <https://developers.google.com/identity/protocols/oauth2/web-server> (accessed 2026-09-30).

> Revoke via POST https://oauth2.googleapis.com/revoke; request scopes incrementally, in context; store refresh tokens securely.

<a id="owasp-llm-2025"></a>
**[owasp-llm-2025]** OWASP GenAI Security Project. *OWASP Top 10 for Large Language Model Applications 2025*. <https://genai.owasp.org/llm-top-10/> (accessed 2026-09-30).

> LLM01:2025 Prompt Injection; LLM05:2025 Improper Output Handling; LLM06:2025 Excessive Agency.

<a id="sec-social-media-scams"></a>
**[sec-social-media-scams]** US Securities and Exchange Commission, Office of Investor Education and Advocacy. *Social Media and Stock Tip Scams (Investor Alert)* (2026-02-06). <https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/social-media-stock-scams> (accessed 2026-09-30).

> Red flags: guaranteed high returns with little risk, pump-and-dump, scalping, touting without disclosing compensation.

<a id="usc-15-77q"></a>
**[usc-15-77q]** Legal Information Institute, Cornell Law School. *15 U.S.C. § 77q(b) (Securities Act of 1933 § 17(b), anti-touting)*. <https://www.law.cornell.edu/uscode/text/15/77q> (accessed 2026-09-30).

> Publicizing a security for consideration from an issuer, underwriter or dealer requires fully disclosing the receipt and the amount of that consideration.

<a id="yt-altered"></a>
**[yt-altered]** YouTube Help. *Disclosing use of altered or synthetic content*. <https://support.google.com/youtube/answer/14328491?hl=en> (accessed 2026-09-30).

> Disclose realistic content that makes a real person appear to say/do something, alters real events/places, or generates realistic scenes; captions, filters and cloning one's own voice for dubs don't need it.

<a id="yt-analytics-metrics"></a>
**[yt-analytics-metrics]** Google for Developers. *YouTube Analytics and Reporting APIs: Metrics*. <https://developers.google.com/youtube/analytics/metrics> (accessed 2026-09-30).

> averageViewDuration (s), averageViewPercentage, audienceWatchRatio, relativeRetentionPerformance definitions.

<a id="yt-chapters"></a>
**[yt-chapters]** YouTube Help. *Video chapters*. <https://support.google.com/youtube/answer/9884579?hl=en> (accessed 2026-09-30).

> First timestamp 00:00; at least three timestamps; each chapter at least 10 seconds.

<a id="yt-kids"></a>
**[yt-kids]** YouTube Help. *Determining if your content is made for kids*. <https://support.google.com/youtube/answer/9528076?hl=en> (accessed 2026-09-30).

> Cites COPPA and YouTube's agreement with the US FTC.

<a id="yt-monetization"></a>
**[yt-monetization]** YouTube Help. *YouTube channel monetization policies (reused content; inauthentic content, formerly repetitious)*. <https://support.google.com/youtube/answer/1311392?hl=en> (accessed 2026-09-30).

> Reused content applies 'even if you have permission from the original creator'; renamed repetitious -> inauthentic on 2025-07-15 to cover repetitive or mass-produced content.

<a id="yt-paid-promotion"></a>
**[yt-paid-promotion]** YouTube Help. *Add paid product placements, sponsorships & endorsements*. <https://support.google.com/youtube/answer/154235?hl=en> (accessed 2026-09-30).

> Creators tick the paid-promotion box in video details; creators and partners remain responsible for legal requirements.

<a id="yt-shorts"></a>
**[yt-shorts]** YouTube Help. *Upload YouTube Shorts (length and aspect ratio)*. <https://support.google.com/youtube/answer/15424877?hl=en> (accessed 2026-09-30).

> Vertical or square videos up to three minutes uploaded after 2024-10-15 are Shorts.

<a id="ytapi-channels-update"></a>
**[ytapi-channels-update]** Google for Developers. *YouTube Data API v3: Channels: update*. <https://developers.google.com/youtube/v3/docs/channels/update> (accessed 2026-09-30).

> Accepts youtubepartner, youtube or youtube.force-ssl scopes (not youtube.upload).

<a id="ytapi-delete"></a>
**[ytapi-delete]** Google for Developers. *YouTube Data API v3: Videos: delete*. <https://developers.google.com/youtube/v3/docs/videos/delete> (accessed 2026-09-30).

> Accepts youtubepartner, youtube or youtube.force-ssl scopes (not youtube.upload).

<a id="ytapi-insert"></a>
**[ytapi-insert]** Google for Developers. *YouTube Data API v3: Videos: insert*. <https://developers.google.com/youtube/v3/docs/videos/insert> (accessed 2026-09-30).

> 1 unit in the Video Uploads quota bucket; accepted scopes include youtube.upload; uploads from unverified API projects created after 2020-07-28 are restricted to private; settable: status.privacyStatus, publishAt, selfDeclaredMadeForKids, containsSyntheticMedia (paidProductPlacementDetails is a readable part, not a settable property).

<a id="ytapi-quota"></a>
**[ytapi-quota]** Google for Developers. *YouTube Data API: Quota calculator / determine quota cost*. <https://developers.google.com/youtube/v3/determine_quota_cost> (accessed 2026-09-30).

> Default allocation: 100 search.list calls, 100 videos.insert calls, and 10,000 units per day for all other endpoints.

<a id="ytapi-revisions"></a>
**[ytapi-revisions]** Google for Developers. *YouTube Data API: Revision history*. <https://developers.google.com/youtube/v3/revision_history> (accessed 2026-09-30).

> 2024-10-30 containsSyntheticMedia added; 2025-12-04 upload cost cut from ~1600 to ~100 units; 2026-06-01 videos.insert and search.list moved to their own quota buckets.

<a id="ytapi-rmf"></a>
**[ytapi-rmf]** Google for Developers. *YouTube API Services: Required Minimum Functionality*. <https://developers.google.com/youtube/terms/required-minimum-functionality> (accessed 2026-09-30).

> Upload clients must let users choose public, private or unlisted.

<a id="ytapi-scopes"></a>
**[ytapi-scopes]** Google for Developers. *YouTube Data API: OAuth 2.0 for installed applications (scopes)*. <https://developers.google.com/youtube/v3/guides/auth/installed-apps> (accessed 2026-09-30).

> Lists youtube, youtube.force-ssl, youtube.readonly, youtube.upload and partner scopes.

<a id="ytapi-update"></a>
**[ytapi-update]** Google for Developers. *YouTube Data API v3: Videos: update*. <https://developers.google.com/youtube/v3/docs/videos/update> (accessed 2026-09-30).

> Requires youtube, youtube.force-ssl or youtubepartner scope (not youtube.upload); 50 units; paidProductPlacementDetails not among settable properties.

<a id="ytapi-videos"></a>
**[ytapi-videos]** Google for Developers. *YouTube Data API v3: Videos resource*. <https://developers.google.com/youtube/v3/docs/videos> (accessed 2026-09-30).

> Title max 100 characters, description max 5000 bytes, neither may contain < or >; tags max 500 characters incl. commas; publishAt only when privacyStatus is private.
