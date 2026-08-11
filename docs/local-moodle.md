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

After checkout, Bootstrap verifies each official `origin` and rejects tracked changes in either
source. Moodle-docker permits only one ignored `local.yml`, generated with exact bytes by the
script, which mounts the named `moodledata` volume at `/var/www/moodledata`; any altered override
fails before the wrapper runs. Moodle permits only its generated ignored local `config.php`; before
each normal wrapper invocation its
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
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action AdvanceFixture
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action ExpandFixture
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

The seeded assignment has the exact intro attachment `autotask-brief.txt` with 76 bytes and SHA-256
`beec33f762521fcc5976c5dd799348d888014d988dd335e91c7e195ed811f11c`. A missing attachment with
the old empty or current expected intro is a safe legacy upgrade. Any existing attachment with
different bytes, size, hash, or intro is partial and requires `Reset -Force`; Bootstrap never
overwrites it.

Bootstrap also creates a deterministic, entirely fictitious ASIX catalog inspired only by public
course names: 11 modules, 11 rich assignments, and the original base assignment. The matrix covers
an overdue task, a future-opening task, a task without a deadline, a task without attachments,
multiple attachments, and common ASIX formats such as `.pdf`, `.ova`, `.sql`, `.xml`, `.xsd`,
`.yml`, `.ps1`, `.txt`, and `.md`. `asix-router-lab.ova` is intentionally a tiny metadata-only
fixture and is not bootable; it tests lab-artifact routing without distributing a real VM image.
No private course content, person, password, or submission from either reference school is copied.

The rich fixture is versioned and fail-closed. An exact v1 or v2 is idempotent; a reserved course,
category, assignment, user, enrolment, file, or digest with unexpected content is `partial` and
requires inspection or `Reset -Force`. To simulate a teacher updating the OVA task, run
`AdvanceFixture` once. It keeps the stable task identity, changes its revision fields, and adds
`revision-2.txt`; a second advance is rejected.

`ExpandFixture` applies the declarative `infra/moodle/catalog-v3.json` catalog. It accepts only an
exact v1, v2, or absent fixture: absent is seeded and advanced first, v1 is advanced to v2, and v2
is expanded to v3. An exact v3 is a no-op. The PHP tool validates the complete JSON schema,
identities, cardinalities, types, duplicate keys, and a canonical SHA-256 digest before it creates
anything. A small JSON lexer rejects duplicate keys at every object depth after decoding escapes,
so `"x"` and `"\\u0078"` cannot silently overwrite one another. Canonical JSON recursively sorts
object keys, preserves array order, and uses unescaped Unicode and slashes. It stores that digest
only after complete verification. Existing campaign users, course,
assignments, partial migrations, or a changed catalog digest fail closed and are never overwritten.
The wrapper copies both the PHP tool and catalog to fixed temporary container paths, verifies each
SHA-256 value there, and removes both paths in `finally` cleanup.

V3 creates `ASIX-CAMPAIGN-01`, four fictitious `@example.test` teachers with `editingteacher`, and
11 new fictitious `@example.test` students with `student`; together with the existing `student1`,
there are exactly 12 students enrolled in that course. Every newly created v3 account uses Moodle's
non-interactive `nologin` authentication and has no usable password. The four deterministic
campaign assignments are `central-report-success`, `windows-ssm-success`,
`windows-command-failure`, and `ova-import-negative`. Its `negative.ova` attachment is a tiny
text metadata fixture, not a real or bootable OVA. Use `Reset -Force` followed by `Bootstrap` to
return to v1.

An offset of `0` in either assignment date field means Moodle's no-date sentinel `0`; non-zero
offsets are added to the stable fixture anchor. The same helper is used for creation and complete
state verification. V3 creation and its version/digest configuration writes run in one delegated
Moodle transaction, so an exception rolls them back; a residual v3 user, course module, course, or
digest under a v1/v2 version is instead reported as `partial`.

It creates only ignored `.runtime/moodle*` paths. `.runtime/moodle-secrets.json` holds generated
admin and student passwords, while `.runtime/moodle-token.json` holds the mobile token. Neither is
printed. Do not put school, production, or personal credentials in this environment.
The generated local token is sufficient for all development and live-fixture tests. A real
institutional Moodle token is needed only when the owner explicitly enables the final external
integration; holiday periods or an empty real course do not block development.

Bootstrap removes inherited ACLs from `.runtime` and grants secret access only to the current
Windows user, `SYSTEM`, and Administrators. If that ACL operation cannot be applied, Bootstrap
fails before writing secrets or a token. Keep the connector's acknowledgement database in a
similarly private directory.

To remove the local sources, database data, generated configuration, evidence, secrets, and token,
explicitly opt in:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/moodle.ps1 -Action Reset -Force
```

`Reset` first brings down this Compose project with `--volumes`, validates every deletion target is
inside this repository's `.runtime/moodle*` namespace, and then removes those targets. This deletes
the named local Moodle database and `moodledata` volumes for this exact Compose project. It does not
touch any other Docker project or filesystem path.

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
deletion work. Before any Compose wrapper call it accepts only the exact generated
`.runtime/moodle-docker/local.yml`; do not edit it or add other local overrides.

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

The Moodle CLI seeds use supported core/course/manual-enrolment APIs to create `student1`, the
base `ASIX-LAB` course, the public-structure-inspired ASIX category tree, 11 module courses, and 12
managed assignments in total. Before expansion, Smoke requires those exact 12 managed assignments.
After `ExpandFixture`, Smoke requires the exact 16 managed assignments, four campaign assignments, and both
simulated OVA metadata attachments. It resolves campaign assignment `cmid` values against the one
official `core_course_get_contents` response for the campaign course and fails closed on a missing,
duplicate, or case-mismatched assignment title. Moodle's student REST response intentionally does
not expose course-module `idnumber`; the fixture PHP verifier checks those exact IDs in Moodle's
database, while REST Smoke independently checks the campaign `cmid`/title mapping, count, and
attachments. Assignments in unrelated courses are deliberately outside this managed scope. The script obtains a token from Moodle's official
`login/token.php` endpoint for service `moodle_mobile_app`, then verifies REST
`core_webservice_get_site_info`, `core_enrol_get_users_courses`, and
`core_course_get_contents`. Moodle exception and error responses fail the command.

The token endpoint and REST root are selected from the validated local endpoint candidates rather
than assuming an obsolete pre-5.2 path. See Moodle's [web-service documentation](https://moodledev.io/docs/4.5/apis/subsystems/external) and
[mobile-service setting guidance](https://docs.moodle.org/500/en/Moodle_app_FAQ).
Before Smoke sends HTTP using the persisted token, it requires the saved base URL to be exactly
one configured, validated endpoint candidate. The default candidates remain
`http://127.0.0.1:8000` and `http://127.0.0.1:8000/public`.

## Connector CLI

The Python connector uses Moodle's official mobile REST API, never scraping or browser automation.
For external Moodle, configure a public canonical HTTPS URL. Plain HTTP is accepted only for a
literal loopback, RFC1918, or Tailscale IPv4 local endpoint. The token is supplied from the local
file produced by Bootstrap (or the two environment variables), never as a command-line argument.

```powershell
moodle-autotask-moodle scan --token-file .runtime/moodle-token.json --state .runtime/moodle-state.sqlite3
moodle-autotask-moodle acknowledge --state .runtime/moodle-state.sqlite3 --task-key '<task-key>' --revision-digest '<revision-digest>'
moodle-autotask-moodle download --token-file .runtime/moodle-token.json --task-key '<task-key>' --attachment-key '<attachment-key>' --output-directory .runtime/downloads
```

`scan` is at-least-once: it may return the same candidate until its exact task key and revision are
acknowledged. The scheduler uses the same state database's durable v2 outbox: run
`moodle-autotask-scheduler once --token-file .runtime/moodle-token.json --state .runtime/moodle-state.sqlite3 --request-timeout-seconds 60`
for one immediate scan and drain, or `run` for an immediate cycle followed by a daily cycle
(`--interval-seconds` defaults to `86400`, range `1` through `604800`). Press `Ctrl+C` to stop `run`
cleanly. Both scheduler commands require `--token-file`; they never fall back to environment
credentials. Both commands accept `--lease-seconds` (default `30`, range `6` through `3600`),
`--batch-size` (default `20`, range `1` through `100`), `--retry-base-seconds` (default `5`, range
`1` through `3600`), and `--retry-max-seconds` (default `3600`, range from the base through
`86400`). `--request-timeout-seconds` controls each Moodle transport request (default `15`, range
`1` through `120`); local Docker Desktop bind mounts can require `60` or `120`. The campaign
controller pins an explicit `60` seconds because its post-restart Moodle site-info request can
exceed the CLI default; this does not change the CLI default. The durable outbox
has a fixed safety cap of `1000000` delivery attempts per event; it is
not a command-line setting. A failed scan or delivery
leaves events recoverable; `run` retries after the bounded retry-base delay, while successful cycles
wait for the configured interval. Leases renew while a sink call is active; future sinks must return
within their lease or renew it.

Without additional options the sink is compact JSON on stdout. Telegram is an explicit opt-in. Put
the bot token and numeric private-chat identities in a protected, ignored file; never pass the token
on the command line or commit this example with a real value:

```json
{"botToken":"<TELEGRAM_BOT_TOKEN>","chatId":123456789,"allowedUserId":123456789}
```

On POSIX the configuration file must be mode `0600`. Start the scheduler with both Telegram options:

```powershell
moodle-autotask-scheduler run --token-file .runtime/moodle-token.json --state .runtime/moodle-state.sqlite3 --telegram-config-file .runtime/telegram.json --approval-state .runtime/approval.sqlite3
moodle-autotask-telegram run --config-file .runtime/telegram.json --state .runtime/approval.sqlite3
```

The first process sends each durable event; the second uses outbound Telegram long polling and
persists the update cursor. No webhook or inbound firewall rule is required. Only the exact
`allowedUserId` in the exact `chatId` can decide. `Hacer tarea` records approval of that exact
revision, `Ignorar` records the opposite terminal decision, and `Ver detalles` is read-only. Replayed
buttons are idempotent and an updated Moodle revision needs its own decision. Successful execution
does not submit anything. For a notification carrying an exact Moodle assignment ID, the delivered
report receives independent `Entregar`, `No entregar`, and `Ver detalles` controls. `Entregar` is
one-use and bound to the manifest digest (task key, revision, assignment ID, report bytes and
SHA-256); Moodle is re-enumerated immediately before submission and a changed/deleted assignment
fails closed. The worker uses Moodle's `upload.php` and `mod_assign_save_submission`, persists the
draft ID before the final save, and confirms it through `mod_assign_get_submission_status` before
reporting success. Legacy notifications without an assignment ID remain executable but cannot be
submitted until safely re-enumerated as a new exact revision.

The development fixture sets `submissiondrafts=0` and `requiresubmissionstatement=0`. If Moodle reports either policy as `1`, the
worker does not show `Entregar`: Moodle 5.2.1's official `mod_assign_submit_for_grading` requires
the student's `acceptsubmissionstatement`, which this service must not assert on the student's
behalf. The execution report remains available and Telegram states that delivery was not offered.
This policy is part of the assignment revision and submission manifest. Site info must explicitly
advertise `uploadfiles=true` or Moodle's numeric `uploadfiles=1` before an approval or upload is
offered; absent, null, false, and strings fail closed.

Notification delivery remains at-least-once: a crash after Telegram accepts `sendMessage` but before
local commit can repeat a message with the same buttons. Download selection uses keys returned by
`scan`, not URLs. Transfers reject redirects, non-pluginfile URLs,
unsafe filenames, and size mismatches; the mobile token is appended only after URL validation.
The default attachment cap is 16 GiB (hard maximum 64 GiB), so plan local disk capacity before
selecting OVA images; callers may lower the limit but it is always enforced while streaming.
