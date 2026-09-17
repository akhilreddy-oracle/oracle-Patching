# Company login and approval inbox

Company login is optional and provider-neutral. The application implements
OpenID Connect authorization code with PKCE against an administrator-configured
issuer. Entra ID, Okta or another standards-compatible provider can be selected
later. No tenant or company authentication is enabled by default.

Install the optional dependency into the controller environment:

```sh
.venv/bin/python -m pip install -r webapp/requirements-sso.txt
```

Register a confidential web application with the provider, with the exact
callback `https://your-patching-host/auth/callback`. Enable authorization code,
PKCE S256 and RS256 ID tokens. Include the company group identifiers in the ID
token; set `groups_claim` to the provider's configured claim. The app requests
`openid profile`; configure provider-specific group emission in the tenant.
Group-overage responses without the configured group list are rejected.

Create a private administrator-owned JSON file and point `OPU_OIDC_CONFIG` to
it. Do not commit tenant configuration or client secrets. Example structure:

```json
{
  "label": "Company sign in",
  "issuer": "https://identity.example/your-tenant",
  "client_id": "registered-client-id",
  "client_secret_env": "OPU_COMPANY_CLIENT_SECRET",
  "token_endpoint_auth_method": "client_secret_basic",
  "redirect_uri": "https://patching.example/auth/callback",
  "groups_claim": "groups",
  "group_roles": {
    "company-viewer-group-id": ["viewer"],
    "company-requester-group-id": ["requester"],
    "company-approver-group-id": ["approver"],
    "company-operator-group-id": ["operator"]
  },
  "session_seconds": 900
}
```

Supply the client secret through the deployment's environment/secret manager.
Confidential clients support `client_secret_basic` (default) or
`client_secret_post` when advertised by the provider. Public PKCE clients omit
`client_secret_env` and use `none`; choose the method registered in the tenant.
The config must be a regular single-link file owned by the controller or root,
without group/world write permission. Use `0600`. Start the controller with the
same Python environment in which the dependency was installed. HTTPS is
required; the only exception is an explicitly configured
`allow_loopback_http: true` for a loopback callback during integration testing.
Provider endpoints always require HTTPS. Configure the public redirect URI
explicitly; untrusted forwarding headers do not choose it.

Sessions use random HttpOnly/SameSite cookies backed by a private SQLite store.
Provider tokens are not stored in browser local storage. Login verifies the
provider signature, issuer, audience, authorized party, expiry and nonce, with
one-use state bound to the browser and PKCE. Session lifetime is the shorter of
the ID token's remaining life or 60–900 configured seconds. API writes require
the session CSRF token and reject a mismatched Origin. Logout deletes the local
session; provider-wide logout is not performed. If its provider binding changes
or local roles are revoked, Sign out can still remove the unusable cookie and
session record, but only from the configured application origin. Active-session
sign-out retains the CSRF check. This cleanup does not authorize any patching
or recovery operation.

Roles come from approved group IDs. Local role-mapping edits apply on every
request. Changes to a person's IdP group membership take effect at the next
login, within the bounded session lifetime; real-time directory revocation is
not claimed. Actors use stable issuer-and-subject identities, so changing a
display name does not bypass the existing separation of duties. No role is
granted from a browser-supplied actor or an unverified email address.

The approval inbox at `#/approvals` collects patch and backup requests awaiting
approval or execution authorization. It links to the original sealed review
page. Approving a request still calls the existing guarded API and cannot
authorize the requester to self-approve.

Existing service principal tokens can specify `expires_at` (an ISO timestamp
with timezone) and `disabled: true` in the centrally managed principal registry.
Expired or disabled credentials and their roles are rejected. Existing tokens
without expiry remain compatible; configure an expiry when provisioning them.
Company configuration never silently falls back to lab authentication.

Validation uses locally signed RSA tokens and a mocked provider, including
wrong signatures/issuer/audience/nonce, replay, expiry, CSRF and role revocation.
A tenant login and its group mappings must be tested after a provider is chosen.
These fixtures do not constitute production SSO approval.

For a complete local HTTP session acceptance check, run:

```sh
.venv/bin/python -B tests/company_auth_http.py
```

This suite starts the actual controller HTTP handler and a simulated HTTPS
identity provider on ephemeral loopback ports. It uses the application's real
discovery, authorization-code exchange, PKCE, JWKS/signature verification,
cookies, CSRF protection and server-side session store. The simulated provider
checks the code verifier, and an authenticated company admin performs a real
metadata API write in disposable storage. Tests also cover callback replay,
browser binding, wrong nonce, denied endpoint redirects, role changes,
revocation, expiry and logout.

The HTTPS provider uses a freshly generated certificate trusted only by the
test transport; certificate and hostname verification stay enabled. Outbound
connections are restricted to the two loopback listeners. Tenant settings,
signing keys, sessions and host metadata are generated inside a temporary
directory. The test uses a public PKCE client and does not read deployment
credentials, contact a real provider or start Oracle work. Session expiry uses a
controlled clock. Passing establishes local protocol/session integration,
including actual HTTP requests; it does not verify a selected company tenant,
confidential-client registration, production TLS deployment or production SSO.

Implementation references: [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)
and [PyJWT validation documentation](https://pyjwt.readthedocs.io/en/stable/usage.html).
