# Incident & Remediation Log

> Newest first. IDs: `INC-<YYYYMMDD>-<client>-<seq>`. Severity follows the linter scale.
> `HYPOTHESIS` entries are allowed here (and only here) and must be resolved or closed.

## Severity guide
| Sev | Definition | Response |
|---|---|---|
| critical | Leaked secret; consent bypass on regulated traffic; purchases lost or doubled > 5% | Escalate to human immediately; fix same day |
| high | A conversion destination wrong/missing; dedup broken; tag firing on wrong pages | Fix within 48 h |
| medium | Partial data quality loss (missing params, EMQ drop, consent gaps on minor tags) | Next sprint |
| low | Bloat / hygiene | Batch cleanup |

## Template

```markdown
### INC-<YYYYMMDD>-<client>-<seq> — <one-line title>
- **Status:** OPEN | MITIGATED | RESOLVED | HYPOTHESIS | CLOSED-NO-ACTION
- **Severity:** critical | high | medium | low
- **Detected by:** <agent> via <tool/skill> at <ISO time>
- **Scope:** container <id> v<n> / store <domain> / event <name>
- **Symptom (observed vs expected):** ...
- **Evidence:** <lint report ref, snapshot ref, dataLayer capture ref>
- **Root cause:** ... (or `HYPOTHESIS: ...` + test that would confirm it)
- **Blast radius:** dates affected, % of events, destinations affected
- **Remediation:** verdict HV-<id>; diff ref; published GTM v<n> by <human role> on <date>
- **Verification:** <QA evidence after fix>
- **Rollback:** <exact version / revert>
- **Prevention:** <new linter rule / monitor / pattern added to gtm-patterns.md>
- **Data repair:** <none | GA4 annotations | Ads conversion adjustments | Meta offline upload>
```

## Log

_(empty — first entry will be the baseline audit of container 100000001)_
