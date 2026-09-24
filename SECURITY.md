# Project Scope Security

Project Scope is a private commercial-research dashboard.

## Production access

Production uses HTTP Basic authentication at the application middleware layer.

Railway variables:

- `SCOPE_AUTH_USER`
- `SCOPE_AUTH_PASSWORD`

The password must never be committed to GitHub.

When `SCOPE_AUTH_PASSWORD` is blank, authentication is disabled. This is intentional for local development and CI only; production should always set it.

## Health checks

`/health` is intentionally unauthenticated so Railway can perform deployment health checks.

All other application pages and API endpoints are protected when production authentication is enabled.

## Pilot customer separation

Customer-facing APIs use a customer slug and database `customer_profile_id` boundaries.

Signals, feedback and buyer-access rules are customer-specific. The Research Intelligence pool is intentionally global because it represents shared market/source intelligence rather than a customer's private classification.

## Real-pilot rule

Do not load commercially sensitive real-company information into an unprotected deployment. Before an external pilot begins, confirm:

1. authentication is enabled in Railway;
2. `/health` passes;
3. customer-specific dashboard/profile/access/export routes are isolated;
4. no credentials exist in GitHub;
5. pilot data is limited to information the participant has agreed to provide.
