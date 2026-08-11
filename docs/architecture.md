# Architecture

`moddle_autotask` is a deliberately small hexagonal automation foundation.
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
`approved` decision atomically creates a durable work item for the exact stored event. A leased
worker deterministically selects `central`, `hybrid`, or `in_guest`. Non-central work calls the
capability-limited AWS lab provider with a stable SHA-256 idempotency key, waits for EC2 plus SSM,
and records the opaque handle. Only one non-central lab may be active; leases recover after crashes
without changing the EC2 client token. After readiness, a digest-bound filesystem spool transfers
only the approved assignment snapshot and verified non-appliance attachments to a separate
`moodle-agent` user. Jobs and agent workspaces publish inputs only after a fully verified temporary
directory is synced and renamed; startup revalidates prior inputs and replaces an incomplete stale
directory before Codex can run. A root-owned Codex policy denies sandboxed commands access to the authentication
cache and application secrets, disables tool network access, and prevents permission escalation.
Central work returns a structured Markdown report. Lab work first returns bounded PowerShell
commands; the worker validates the opaque handle and ownership tags, executes the plan with the
official Systems Manager document, and sends the transcript back for an evidence-based final report.
A guest-side execution marker makes retries idempotent and fails closed if a previous execution is
still ambiguous. Completed reports are sent to the authorized Telegram chat at least once: a crash
after Telegram accepts the document but before the work lease is committed can resend the same
digest-bound report. Ready labs become
cleanup work two hours after execution. OVA/OVF and
virtual-disk attachments fail closed unless the approved revision contains exactly one OVA. That
supported path re-enumerates Moodle, requires the exact task and revision digests, downloads all
attachments through the hardened pluginfile client, and stages them under a content-addressed
private S3 prefix. The importer uses a stable EC2 client token, an isolated importer role, and the
VM Import/Export service role. It verifies the import task's tags and S3 source, tags the resulting
AMI and snapshots, and passes that exact AMI to the lab provider. Cleanup terminates the lab,
deregisters the imported AMI, and deletes its snapshots before completing the work item.

The connector never uses password login, scraping, or browser automation. Its only mutation is the
explicit submission boundary: after a distinct Telegram `Entregar` decision it verifies the current
task/revision/assignment identity, uploads one Markdown report to Moodle `upload.php`, persists the
draft item ID before `mod_assign_save_submission`, then verifies the final result through
`mod_assign_get_submission_status`. A crash after a saved draft is resolved by verification, never a
blind second save. Telegram uses long polling over outbound HTTPS;
the controller exposes no webhook or inbound port. Bot credentials are read only from a protected
file and never accepted as a command-line value or placed in callback data.

AWS infrastructure is a separate adapter boundary. The Terraform baseline creates a Linux
controller whose instance role can read the exact Moodle secret, use bounded artifact prefixes, and
connect to Systems Manager. It cannot call EC2 directly or mutate IAM. For an approved provision
operation it can assume one exact lab-provisioner role. A second exact role can start and clean up
tagged VM imports and can pass only the dedicated VM Import/Export service role. The provisioner can
launch only the operator-fixed Windows image or a project-owned imported image, plus the fixed
instance type, subnet, security group, volume bounds, and lab instance profile; it can terminate only
project-tagged labs. The guest profile has Systems Manager runtime permissions only; it cannot read
the shared artifact bucket, Moodle token, or provision another lab. Per-task digest-bound transfer
into a lab is deferred; appliance work reaches the lab through the imported AMI.

`AwsEc2LabProvider` hashes task and workflow identities before tagging, binds EC2's client token to
the complete immutable request plus caller idempotency key, reconciles by that key, validates every
opaque handle and ownership tag, and treats a lab as ready only after both EC2 and Systems Manager
report it usable. Launch configuration is operator input, never data extracted from Moodle text.
