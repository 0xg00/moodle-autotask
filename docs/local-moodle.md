# Local Moodle integration environment

This is a development-only Moodle instance for adapter integration tests. It must never be
exposed publicly: its HTTP listener defaults to `127.0.0.1:8000`; a validated private/Tailscale
bind is available only through the explicit opt-in described below. Developer debugging is enabled
only in this local instance.

## Prerequisites

- Windows Docker Desktop with its daemon already running. The script does not start Docker Desktop.
- Git for Windows, including Git Bash (`bash.exe`), and Git on `PATH`.
- PowerShell 5.1 or newer.

The stack uses the official [Moodle Docker environment](https://github.com/moodlehq/moodle-docker)
and its documented [PostgreSQL and loopback web-port configuration](https://github.com/moodlehq/moodle-docker#environment-variables).
It clones only the official [Moodle source mirror](https://github.com/moodle/moodle). The exact
source pins are in `infra/moodle/versions.psd1`: moodle-docker commit
`f4c2324d32fb74d7753264381f0a9b418b6034b2`, Moodle v5.2.1 annotated tag object
`cbc847cd037906036e7047630eee03d5f87d3ff8`, and its peeled commit
`63e16b757ca8fee05b672a27c23ee27cc8f9fabb`.

After checkout, Bootstrap verifies each official `origin`, rejects tracked changes in either source,
and rejects all moodle-docker untracked or ignored files (including auto-loaded `local.yml`). Moodle
permits only its generated ignored local `config.php`; before each normal wrapper invocation its
SHA-256 bytes must exactly match the pinned `config.docker-template.php`. Any other untracked or
ignored payload, such as `vendor/` or `node_modules/`, is rejected. Bootstrap overwrites the
configuration from that pinned template before wrapper trust is required. The seed runs through
Moodle core/plugin APIs inside the web container and does not create source-tree dependencies or
require Composer autoload generation.

Before an existing runtime checkout is fetched or checked out, the script rejects active Git hooks,
local attributes, effective `.git/info/exclude` entries, and any Git local/worktree configuration
outside a small validated clone allowlist. It asks Git's canonical configuration parser for those
settings, so commented section syntax cannot bypass checks for filters, includes, credential helpers,
hooks, upload packs, or checkout controls. Git clone/fetch/checkout use an ignored, validated empty
hooks directory and disable fsmonitor, so runtime Git controls cannot execute code or conceal
payloads during source updates. The script also rejects index assume-unchanged/skip-worktree flags
and local replacement objects; every Git inspection and mutation disables replacement-object use.

Before every normal invocation of either upstream wrapper, the script revalidates the complete
repository ancestor chain (no reparse points), each official origin and pin, and source
cleanliness. This includes `Up`, `Down`, `Status`, and `Smoke`: they refuse a modified Moodle or
moodle-docker checkout before Docker or an upstream wrapper is invoked. Reset deliberately uses a
narrower moodle-docker-only validation so it can remove a broken Moodle checkout safely.

Bootstrap enables the official mobile service through Moodle's
[`admin_setting_enablemobileservice`](https://phpdoc.moodledev.io/4.5/d8/dfa/classadmin__setting__enablemobileservice.html)
write path, then verifies the enabled `moodle_mobile_app` service, REST protocol, and
`webservice/rest:use` capability. A plain `cfg.php` write is not used for that activation.

The script verifies both the annotated tag object and peeled commit after fetching, before it
checks out Moodle. Source is pinned; upstream Compose images are not claimed to be bit-for-bit
reproducible. Their resolved names, digests, or immutable image IDs are recorded in ignored
`.runtime/moodle-images.json` after startup.

## Commands

Run from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Bootstrap
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Smoke
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Down
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Up
```

`Bootstrap` is idempotent: it verifies/clones sources, writes local-only configuration, starts
PostgreSQL and Moodle through the upstream wrapper, installs Moodle when needed, enables web and
the official mobile service, seeds the fixture, stores a mobile token, and runs the REST smoke
test. For Moodle 5.2.1 it separately validates the root core CLI/configuration paths and candidate
public endpoints. Before installation it probes
the PostgreSQL state using the pinned upstream stack's validated `m_` table prefix; an
already-installed site recreates ignored install evidence rather than rerunning installation, while
an incomplete database is reported instead of treated as empty. A crash during database installation
or fixture creation is fail-closed: inspect it or run `Reset -Force`; the script does not resume a
partially-created database or fixture.

It creates only ignored `.runtime/moodle*` paths. `.runtime/moodle-secrets.json` holds generated
admin and student passwords, while `.runtime/moodle-token.json` holds the mobile token. Neither is
printed. Do not put school, production, or personal credentials in this environment.

To remove the local sources, database data, generated configuration, evidence, secrets, and token,
explicitly opt in:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Reset -Force
```

`Reset` first brings down this Compose project, validates every deletion target is inside this
repository's `.runtime/moodle*` namespace, and then removes those targets. It does not touch any
other Docker project or filesystem path.

With `-Force`, Reset removes an ordinary `.runtime/moodle-docker/local.yml` only after validating
containment and reparse-point safety, and before invoking the Compose wrapper. A directory or
reparse-point at that path is rejected rather than followed or removed.

Before Reset runs Compose down, it verifies that moodle-docker is a clean Git checkout with the
official origin, the pinned commit, and the expected regular wrapper. A partial or modified
checkout is not executed; Reset warns and removes only validated local runtime targets.
When that Docker-only validation succeeds but the Moodle checkout is missing, Reset uses the
validated `.runtime` directory as a Compose WWWROOT solely for `down`; normal actions always use
the pinned Moodle checkout.

The script rejects a reparse-point repository/runtime path or ancestor before source, Compose, or
deletion work, and rejects `.runtime/moodle-docker/local.yml` before any Compose wrapper call. Do
not add local Compose overrides to this reproducible test environment.

`Down` only stops this Compose project, preserving its containers and database. `Up` resumes those
existing containers, waits for PostgreSQL, and refreshes image evidence; it fails with a Bootstrap
instruction if no local containers exist. Only `Reset -Force` destroys volumes.

## Optional private or Tailscale bind

The default is still loopback-only: `127.0.0.1:8000`. To opt in for a private Windows address or
Tailscale, set the process environment variable before running the script. Use a placeholder or
your own currently assigned address; do not commit a personal address.

```powershell
$env:MOODLE_AUTOTASK_BIND_IP = '<TAILSCALE_LOCAL_IPV4>'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Up
```

The script accepts only canonical dotted-quad IPv4: exactly `127.0.0.1`, RFC1918 (`10/8`,
`172.16/12`, or `192.168/16`), or Tailscale CGNAT (`100.64/10`). Non-loopback values must be
currently assigned by Windows; a CGNAT address must be assigned to the `Tailscale` interface.
Hostnames, wildcards, public addresses, IPv6, leading-zero forms, and unassigned addresses fail
closed. The one validated value controls the Compose bind, Moodle `wwwroot`, token endpoints, and
Smoke allowlist. Run `Bootstrap` or `Up` after adding, removing, or changing it so Compose uses
`up -d` to reconcile the stored port binding.

To return to loopback in the current shell, remove the process override and reconcile again:

```powershell
Remove-Item Env:MOODLE_AUTOTASK_BIND_IP
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Up
```

For a Tailscale-only host firewall rule, review and adapt this example before running it. It limits
TCP/8000 to the local Tailscale address and remote Tailscale CGNAT range; it does not create a LAN
or public rule:

```powershell
New-NetFirewallRule -DisplayName 'Moodle Tailscale 8000' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress '<TAILSCALE_LOCAL_IPV4>' -RemoteAddress '100.64.0.0/10' -LocalPort 8000 -InterfaceAlias Tailscale
```

After each `Bootstrap` or `Up`, the script sets and verifies `unless-stopped` for every project
container. This lets containers restart after the Docker daemon returns, but `Down` remains an
intentional stop. Docker Desktop must itself be configured to start automatically and its daemon
must be running; the script neither changes Docker Desktop settings nor starts it.

## Smoke contract

The inline Moodle CLI seed uses supported core/course/manual-enrolment APIs to create `student1`,
the enrolled `ASIX-LAB` course, and the `AutoTask assignment` course module with a course-module
idnumber. The script obtains a token from Moodle's official
`login/token.php` endpoint for service `moodle_mobile_app`, then verifies REST
`core_webservice_get_site_info`, `core_enrol_get_users_courses`, and
`core_course_get_contents`. Moodle exception and error responses fail the command.

The token endpoint and REST root are selected from the validated local endpoint candidates rather
than assuming an obsolete pre-5.2 path. See Moodle's [web-service documentation](https://moodledev.io/docs/4.5/apis/subsystems/external) and
[mobile-service setting guidance](https://docs.moodle.org/500/en/Moodle_app_FAQ).
Before Smoke sends HTTP using the persisted token, it requires the saved base URL to be exactly
one configured, validated endpoint candidate. The default candidates remain
`http://127.0.0.1:8000` and `http://127.0.0.1:8000/public`.
