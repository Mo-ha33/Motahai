# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 1.x     | Yes       |
| < 1.0   | No        |

## Reporting a Vulnerability

Please report security vulnerabilities privately using GitHub's private vulnerability reporting:

1. Open the **Security** tab of [Mo-ha33/Motahai](https://github.com/Mo-ha33/Motahai).
2. Click **Report a vulnerability**.

Please do not open public issues for security problems. Do not include real customer data, live API tokens, or production credentials in your report; use test data or redacted examples instead.

## Response Targets

- **Acknowledgement:** within 3 business days of your report.
- **Triage:** within 10 business days.
- **Coordinated disclosure:** we will work with you on a fix and publish details after the fix ships. The default disclosure window is 90 days from the report.

## Scope

**In scope**

- Webhook signature verification (Shopify, Salla)
- The Ed25519 human-in-the-loop (HITL) publish gate
- The Fernet identity vault
- Tenant isolation
- SSRF guards

**Out of scope**

- Third-party platforms (Meta, Shopify, Salla, Bosta, OTO); please report issues in those platforms to their vendors
- Denial of service
- Social engineering
