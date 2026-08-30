# PR-03 non-production OIDC acceptance

This is the credential-gated acceptance procedure for PR-03. Repository tests
use an injected provider and intentionally block external network access; they
cannot prove that a real IdP client, HTTPS proxy, callback registration, or
browser cookie policy works. Until this procedure is completed, record the
acceptance state as `pending_external_credentials`.

## Safety boundary

- Use a dedicated non-production Dispatch Center origin and non-production IdP
  client. Never point this procedure at a production tenant or production DB.
- The browser-visible origin and callback must be HTTPS. The registered redirect
  URI must be exactly `https://<dispatch-origin>/auth/callback`.
- Inject `OIDC_CLIENT_SECRET` through the deployment secret mechanism. Do not
  paste it into a command line, screenshot, log, shell history, issue, or this
  repository.
- Proxy, APM, WAF, ingress, and egress tracing must suppress callback query
  strings, cookies, authorization headers, `X-Auth-Token`, `Set-Cookie`, and
  provider token bodies.
- Use two controlled human test subjects. Any disabled-actor check must use a
  disposable DB or an already approved identity-administration procedure; do
  not edit an operational DB ad hoc.
- Stop if credentials, callback ownership, TLS, or log redaction cannot be
  verified. A fake-provider result is not a substitute.

## Required build and flag evidence

Record the exact commit, wheel SHA-256, schema version, origin, issuer identifier,
callback URI, operator, and UTC start time. Record only whether secrets are
present, never their values.

Normal test state:

```dotenv
API_V2_ENABLED=true
PRODUCT_RBAC_V2_ENABLED=true
OIDC_ENABLED=true
AUTHORIZATION_MODE=enforce
LEGACY_SHARED_TOKEN_ENABLED=false
SERVICE_TOKEN_AUTH_ENABLED=true
```

The selected human must have an approved Product role in Project A and no role
in Project B. Service-token verification uses a separately scoped test token;
it is not placed in the browser.

## Browser login and workspace

1. Start from a fresh browser profile and open `https://<dispatch-origin>/`.
2. Confirm the Product v2 shell is served and an unauthenticated
   `GET /api/v2/me` returns 401 with `Cache-Control: no-store`.
3. Select OIDC login. Confirm the authorization request uses the expected
   non-production issuer, Authorization Code flow, PKCE S256, exact callback,
   state, and nonce.
4. Complete MFA if the IdP requires it. Confirm callback returns to `/`, a
   Secure/HttpOnly/SameSite=Lax session cookie exists, and no provider token is
   visible in browser storage.
5. Confirm `/api/v2/me` reports `authentication.method=session` and the exact
   approved Project A roles. It must not contain email, issuer, subject, groups,
   binding IDs, or credential identifiers.
6. Confirm `/api/v2/workspace` contains Project A data only. Project B names,
   run IDs, approval IDs, counts, or payload hints must not appear.
7. Confirm `/api/v2/me/sessions` marks exactly the verified browser session as
   current and exposes no session/identity ID, raw token, or digest.
8. Inspect Application storage. `localStorage`, `sessionStorage`, and IndexedDB
   must contain no legacy token or OIDC credential.
9. Logout. Confirm the current server session is immediately revoked, its cookie
   is cleared, and the next v2 read returns 401. Other test sessions, if any,
   remain unchanged.

## Negative and rollback checks

Run these only on the isolated non-production instance:

1. Attempt login with an actor already disabled through the approved test setup.
   The callback must return 403 and no new `actor_sessions` row may be created.
2. Cancel at the IdP, replay a consumed callback, and exercise a controlled IdP
   unavailable condition. Each must fail with a credential-free response and
   must never yield anonymous access to a v2 route.
3. While an unexpired test session exists, set `OIDC_ENABLED=false` and restart.
   Existing valid sessions must still read the v2 workspace; `/auth/login` and
   `/auth/callback` must return 404. The application must not automatically
   enable legacy auth or change authorization mode.
4. Set `API_V2_ENABLED=false` and restart. `/` must serve the legacy UI and all
   three v2 read routes must return 404. Re-enable API v2 before continuing.
5. If simulating the emergency rollback, record operator, reason, UTC start/end,
   and approval before enabling a rotated legacy token and shadow mode. The
   browser token must disappear on reload. Restore enforce mode and disable the
   legacy token when the exercise ends.

## Evidence record

Store redacted evidence in the deployment's controlled evidence system, not in
Git. The record must contain:

```text
status: passed | failed | pending_external_credentials
commit:
wheel_sha256:
schema_version:
nonprod_origin:
issuer_identifier:
callback_uri:
operator:
started_at_utc:
finished_at_utc:
login_result:
logout_result:
disabled_actor_result:
idp_failure_result:
cross_project_result:
oidc_disabled_rollback_result:
api_v2_disabled_rollback_result:
audit_event_ids:
redacted_artifact_references:
notes:
```

Every result must be backed by status codes, safe response-field assertions,
and audit event identifiers. Never attach cookies, authorization codes, state,
nonce, PKCE verifier, client secret, raw service/legacy token, or session hash.
