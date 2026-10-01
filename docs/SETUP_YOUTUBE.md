# Connecting your YouTube channel (only when you are ready to leave dry-run)

You don't need any of this for `make demo`. Do it once per deployment. Google's console labels change from time to time; the steps below match Google's documentation as of 30 September 2026.

## 0. Lock down the Google account first

The account that owns the channel is the real target. Turn on **2-Step Verification**: Google Account → **Security & sign-in** → **Turn on 2-Step Verification**. Use a **passkey or hardware security key** as the second step. Google recommends both because they resist phishing ([google-2sv](REFERENCES.md#google-2sv)).

## 1. A separate Google Cloud project for this deployment

1. Open the Google Cloud console and create a **new project** just for clipwarden (for example `clipwarden-<channel>`). Don't reuse a project that holds other credentials: a separate project limits the blast radius and gives you separate quota.
2. In **APIs & Services → Library**, enable **YouTube Data API v3**. Leave the Analytics API off until you turn on analytics (milestone 2).

## 2. OAuth consent and a Desktop client

1. **Menu → Google Auth platform → Branding**: give the app a name and your support email ([google-consent](REFERENCES.md#google-consent)).
2. **Audience**: choose **External** (unless your channel belongs to a Google Workspace organization), then add the channel's Google account under **Test users**.
3. **Data Access**: add **only** `https://www.googleapis.com/auth/youtube.upload`.
4. **Clients → Create client → Desktop app**. Download the JSON and save it as `client_secret.json` next to your `clipwarden.toml`. It is git-ignored. Google doesn't treat installed-app client secrets as confidential; the protection is PKCE plus the loopback redirect ([google-oauth-native](REFERENCES.md#google-oauth-native)).

> ⚠️ **Testing mode tokens expire after 7 days.** An External app whose publishing status is "Testing" gets refresh tokens that expire in 7 days ([google-oauth-overview](REFERENCES.md#google-oauth-overview)). In Testing mode, expect to run `clipwarden auth login` again weekly, or move the app to "In production" (which may require Google's verification for sensitive scopes).

## 3. Sign in once

```bash
pip install --require-hashes -r requirements-youtube.lock   # or: make setup
export CLIPWARDEN_VAULT_PASSPHRASE='…'   # only needed if your OS has no keychain (e.g. WSL); 12+ chars
clipwarden auth login
```

A browser opens on Google's sign-in page. clipwarden listens on a random port of `127.0.0.1` for the redirect, checks the `state`, exchanges the code with Proof Key for Code Exchange (PKCE), and stores **only the refresh token** in the vault. If Google ever grants a scope we didn't ask for, the token is refused and not stored.

**Docker deployment:** a browser on your host can't reach the loopback port inside the container. Run `clipwarden auth login` once from a host install with the same `data/` folder and the same vault passphrase (the one in `secrets/vault_passphrase.txt`).

## 4. Turn on live publishing (deliberately awkward)

1. Run at least one dry run (`clipwarden publish`), then read the request it wrote to `data/dry-run/<clip>.json`.
2. Set `[publish] mode = "live"` in `clipwarden.toml`.
3. Run `clipwarden publish enable-live` and type `ENABLE LIVE`.

Live uploads also need an intact audit chain. If anything looks wrong, use the **kill switch** (dashboard → Settings, or `clipwarden kill on`).

## 5. Know the platform limits

- **Unaudited projects upload private-only.** Videos uploaded through `videos.insert` from unverified API projects created after 28 July 2020 are restricted to private viewing ([ytapi-insert](REFERENCES.md#ytapi-insert)). Until your project passes Google's API audit, approve clips as **Private** and publish them from YouTube Studio. Scheduled-public uploads will stay private.
- **Quota:** 100 `videos.insert` calls per day by default, in their own bucket ([ytapi-quota](REFERENCES.md#ytapi-quota)). clipwarden's daily cap (3 by default) keeps you far below that. If Google still says no, the clip is *deferred*, not failed.
- **Paid promotion** can't be set through the API, so paid clips upload private and you tick the box in Studio ([POLICY.md](POLICY.md)).

## 6. Revoke

Dashboard → **Settings → Revoke YouTube access**, or `clipwarden auth revoke`. This calls Google's revoke endpoint and deletes the local token ([google-oauth-web](REFERENCES.md#google-oauth-web)). You can also remove access from your Google Account's third-party connections page.
