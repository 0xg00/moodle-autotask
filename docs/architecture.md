# Architecture

`moddle_autotask` is a deliberately small hexagonal foundation, not a working automation system.
The domain contains immutable IDs, revision and digest bindings, execution and submission values,
and a pure task-state transition function. It imports neither application code nor adapters.

The application orchestrator is the only included component that turns valid lifecycle state and
human approvals into calls on ports. Start-work and submission approvals are independently bound to
the task ID, workflow revision, checkpoint, and relevant immutable digest. The START_WORK digest
must exactly match both provisioning and execution requests. A mismatch is rejected before a port call.

Ports define future seams. `AgentRuntime` receives only a structured, capability-limited execution
request and an opaque lab handle; it receives no `LabProvider`, cloud credentials, or host-admin
command channel. `LabProvider` exposes explicit idempotency keys for provisioning and teardown;
the orchestrator permits retries only with the identical request and key, and adapters must make
repeated teardown safe. If provisioning is ambiguous, the provider must reconcile the same request
and key before cleanup: a returned handle is torn down, while `None` definitively confirms that no
lab exists and permits cleanup to complete. `TaskSubmitter` accepts an immutable `SubmissionIntent` only after the
orchestrator validates a matching submit approval bound to that exact intent.

The Moodle adapter is a separate provider boundary and does not alter generic task ports. It uses
the official mobile REST API only: it verifies `core_webservice_get_site_info`, enumerates
`mod_assign_get_assignments`, and exposes immutable assignment snapshots. A site URL is trusted
only after Moodle returns exactly the canonical configured URL and advertises the required mobile
functions. Task, attachment, and revision identifiers are versioned SHA-256 values; revision input
uses canonical JSON metadata, never tokens or stateful URLs.

SQLite acknowledgement state is intentionally at-least-once. A scan reports NEW until a task has
ever been acknowledged, UPDATED for a later revision, and omits the exact acknowledged revision.
`acknowledge(task_key, revision_digest)` is exact, transactional, and idempotent. A later notifier
uses schema v2: a same-database durable outbox creates a stable event ID from the exact task and
revision hashes, leases delivery with renewable ownership, and atomically records successful local
delivery with the exact acknowledgement. It is honestly at-least-once: a crash after a sink side
effect can repeat the same event ID, so consumers must deduplicate it. The local JSON stdout sink
remains available for development. The optional outbound-only Telegram sink reuses opaque callback
tokens across delivery retries, accepts decisions only from one configured user and chat, and stores
`pending`, `approved`, or `ignored` against the exact task and revision in a separate SQLite state.
Its durable update cursor makes callbacks idempotent across process restarts. Neither successful
delivery nor manual acknowledgement is authorization to execute or submit work. A Telegram
`approved` decision is durable input for the future execution workflow, not a direct cloud action.

The connector remains read-only: password login, scraping/browser automation, task execution, and
Moodle submission remain outside this milestone. Telegram uses long polling over outbound HTTPS;
the controller exposes no webhook or inbound port. Bot credentials are read only from a protected
file and never accepted as a command-line value or placed in callback data.

AWS infrastructure is a separate adapter boundary. The Terraform baseline creates a Linux
controller whose instance role can read the exact Moodle secret, use bounded artifact prefixes, and
connect to Systems Manager. It cannot call EC2 directly or mutate IAM. For an approved provision
operation it can assume one exact lab-provisioner role. That role can launch only the operator-fixed
Windows image, instance types, subnet, security group, volume bounds, and lab instance profile, and
can terminate only project-tagged labs. The guest profile can read task inputs, write lab results,
and connect to Systems Manager; it cannot read the Moodle token or provision another lab.

`AwsEc2LabProvider` hashes task and workflow identities before tagging, binds EC2's client token to
the complete immutable request plus caller idempotency key, reconciles by that key, validates every
opaque handle and ownership tag, and treats a lab as ready only after both EC2 and Systems Manager
report it usable. Launch configuration is operator input, never data extracted from Moodle text.
