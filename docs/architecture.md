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
needs its own idempotent outbox/lease semantics to avoid duplicate external notifications.

The connector is read-only: AWS, notification delivery, scheduler execution, password login,
scraping/browser automation, task execution, and Moodle submission remain outside this milestone.
