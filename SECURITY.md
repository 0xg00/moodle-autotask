# Security policy

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/0xg00/moodle-autotask/security/advisories/new)
for suspected vulnerabilities. Do not open a public issue containing exploit details, credentials,
student data, private Moodle URLs, AWS identifiers, Terraform state, logs, or runtime evidence.

Include the affected revision, impact, reproduction steps using fictitious data, and any suggested
mitigation. The maintainer will acknowledge a complete report when it has been reviewed; this
project does not currently promise a fixed response SLA.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| `main` | Best effort |
| Older releases | No |

## Credential handling

The Moodle connector accepts an opaque mobile-service token only from protected local/runtime JSON
or environment configuration. Never pass tokens on a CLI command line, commit `.runtime/`, or log
token files, request URLs, attachment keys, presigned URLs, or credentials.

Telegram and Moodle configuration files are expected to be regular, one-link files with strict
ownership and mode checks in production. Terraform creates Secrets Manager containers but does not
manage application secret values. Terraform state and plans may contain account metadata and must
never be committed.

## Trust boundaries

- Real Moodle access uses public HTTPS. HTTP is restricted to validated loopback, RFC1918, or
  Tailscale IPv4 development endpoints.
- Downloads reject redirects and URLs outside the verified site's exact `pluginfile.php` route.
- The AWS controller has no inbound security-group rules and is administered through Systems
  Manager.
- The controller instance role cannot directly create EC2 labs or mutate IAM.
- Codex runs as a separate unprivileged user under a root-owned policy that denies application
  secrets, authentication material, instance credentials, direct tool network access, and
  permission escalation.
- Human execution and submission approvals are independent, durable, and bound to immutable
  digests.

Security fixes should be minimal, preserve forensic evidence, and include regressions for the
reported failure and adjacent retry/crash paths.
