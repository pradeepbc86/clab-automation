# Security Policy

## Reporting a vulnerability

Please email security findings to `pradeepbc86@gmail.com` with the subject
prefix `[SECURITY]`. Do **not** open a public GitHub issue for security
findings.

Include in the report:

- Affected repo, file, and line numbers if known
- Reproduction steps
- Impact assessment (information disclosure, RCE, lateral movement, etc.)

You can expect:

- Acknowledgement within 72 hours
- Initial assessment within 7 days
- A coordinated disclosure timeline negotiated based on severity

## Scope

These projects are POC / portfolio repositories. They are not intended for
production deployment. That said, the patterns demonstrated (especially in
`clab-ai-mcp`) are designed to be production-safe primitives, and security
issues in the example code will be addressed.

In particular, please report:

- Command-injection vectors in `mcp_server.py` or the agent
- Authentication / authorization bypasses
- Hardcoded credentials or secrets that escape `.gitignore`
- Dependency vulnerabilities flagged by `pip-audit` / `npm audit`

## Threat models

Per-repo threat models live in `docs/THREAT_MODEL.md` where applicable
(currently `clab-ai-mcp` and `clab-automation`).
