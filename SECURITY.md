# Security policy

This private-development project has no public support channel. Report suspected vulnerabilities
privately to the maintainers; do not include secrets, credentials, or student data in reports.

The Moodle connector accepts an opaque mobile-service token only from a local JSON token file or
environment variables. Never pass tokens on a CLI command line, commit `.runtime`, or log token
files/request URLs. State schema v2 also contains allowlisted course and assignment summary metadata
(including student/course information) for pending local notifications. Stdout can contain that
metadata. Tokens, URLs, token paths, attachment keys, and credentials are excluded.

Use public HTTPS for real Moodle sites. HTTP is intentionally restricted to literal loopback,
RFC1918, or Tailscale IPv4 endpoints for local development. Downloads reject redirects and URLs
outside the verified site's exact `pluginfile.php` route.
