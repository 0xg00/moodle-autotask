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
directory before Codex can run. The agent workspace is an independently mounted, root-owned 16 GiB
ext4 image under the dedicated `root:root` mode `0700` `/var/lib/moodle-autotask-root` parent,
with `nodev,nosuid`; the agent cannot start unless that exact filesystem is mounted. Each runner
holds its job-retention lock before the shared workspace admission lock, through materialization,
Codex execution, validation, and result publication, preventing concurrent capacity admission.
A root-owned Codex policy denies sandboxed commands access to the authentication
cache and application secrets, disables tool network access, and prevents permission escalation.
Central work is three fresh, stateless `central_planner`, `central_executor`, and
`central_reviewer` jobs. The planner has no operational authority; its ordered plan binds the
executor's exact `outputs/` set and the reviewer receives only wrapper-validated plan, evidence,
manifest, and dependency digests. The collector accepts 1--64 regular files (2 MiB each,
1,900,000 raw bytes) with canonical POSIX paths and publishes a deterministic stored ZIP under
its SHA-256. Central success is impossible without reviewer acceptance, all role/result digests,
and immutable bundle provenance. Telegram sends the reviewed Markdown and verified ZIP at least
once; crashes between sends may duplicate them. Retention Phase B is an explicitly callable,
crash-safe filesystem protocol, not a scheduled service: controller-private prepared/completed
records and shared committed job barriers coordinate with agent-private intents/barriers/trash and
agent-owned acknowledgement records. It validates canonical tombstones and fixed digest-named
targets, deletes only its owner's trees, and leaves terminal receipts/barriers durable so the
engine itself can suppress identical completed replays. The continuously running worker and agent
each perform at most one bounded retention action before their ordinary work in every cycle: scratch
data has a 24-hour TTL, evidence has a seven-day TTL, and each candidate/scan pass is capped at
1,024 entries. Terminal metadata is intentionally immutable and is not automatically swept.
Periodic wiring and installer directories remain outside this engine boundary. Lab work first returns bounded PowerShell
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
explicit submission boundary: after a distinct Telegram decision it verifies the current
task/revision/assignment identity, uploads one Markdown report to Moodle `upload.php`, persists the
draft item ID before `mod_assign_save_submission`, verifies the exact draft, durably records
`finalizing`, then calls official `mod_assign_submit_for_grading` and verifies the exact submitted
file through `mod_assign_get_submission_status`. For a required statement, its formatted bytes,
format, deterministic plain presentation, and digest are revision-bound and Telegram exposes the
explicit `Acepto y entregar` decision. A crash after a saved draft is resolved by verification,
never a blind second save. Telegram uses long polling over outbound HTTPS;
the controller exposes no webhook or inbound port. Bot credentials are read only from a protected
file and never accepted as a command-line value or placed in callback data.

Controller liveness is independent of journal export and CloudWatch Agent: a root-owned
one-minute systemd publisher evaluates four fixed service heartbeats, exact systemd state,
and restart-counter stability. A root-owned presence marker is the activation boundary:
present requires all four applications healthy; absent requires all four disabled and
inactive. The publisher emits only bounded aggregate/service health measurements through
the controller instance role; missing measurements are an alarm condition.

The controller's normal lab cleanup deadline is a soft operational target two hours after
execution. A separate Terraform-owned EventBridge Lambda provides a controller- and SQLite-
independent hard cost guard: every five minutes it paginates EC2, defensively rechecks the exact
ownership tags and server-side launch time, and terminates only eligible labs at least the
configured four-hour TTL old. Its dedicated role has no controller or IAM authority and limits
each run to 20 deterministic terminations; delayed EventBridge delivery can make the bound
eventual but cannot expand its scope beyond exact tagged labs. The reaper does not reserve Lambda
concurrency or serialize executions. EventBridge has zero pre-invocation delivery retries and a
300-second age bound; Lambda's zero retry-attempt setting suppresses function-error retries, while
throttling or system errors can still retry within the same age bound. The next five-minute event is
the normal function-error retry path, not the only possible retry. Duplicate delivery remains safe
because each invocation has its own deterministic cap and EC2 termination is idempotent.
Terraform independently retains asynchronous failure evidence in one encrypted, bounded standard
SQS queue: EventBridge can send target-delivery failures through a rule-scoped queue policy and
Lambda can send failed asynchronous invocation records through its execution role. CloudWatch alarms
for the controller, Lambda, EventBridge, queue visibility, and missing scheduled invocation publish
to an operator-confirmed SNS email subscription. These records are diagnostic evidence, not work
items: recovery fixes the cause and uses the next idempotent empty scheduled event rather than
replaying opaque records.

AWS infrastructure is a separate adapter boundary. The Terraform baseline creates a Linux
controller whose instance role can read the exact Moodle secret, use bounded artifact prefixes, and
connect to Systems Manager. It cannot call EC2 directly or mutate IAM. For an approved provision
operation it can assume one exact lab-provisioner role. A second exact role can start and clean up
tagged VM imports and can pass only the dedicated VM Import/Export service role. The provisioner can
launch only the operator-fixed Windows image or a project-owned imported image, plus the fixed
instance type, subnet, security group, volume bounds, and lab instance profile; it can terminate only
project-tagged labs. The guest profile has Systems Manager runtime permissions only; it cannot read
the shared artifact bucket, Moodle token, or provision another lab. Before a hybrid or in-guest
agent plan is created, a canonical manifest of the approved task, revision, specification digest,
and each object selected by the execution topology derives a transfer digest. The current direct-
AMI route excludes only its exact imported OVA; nested Hyper-V can deliberately include OVA bytes.
It binds guest paths and
non-central job/report identities. One short-lived exact-object S3 GET presign is delivered per
bounded SSM command; the guest rejects redirects and unsafe files, checks size and SHA-256, and
writes `manifest.json` last. Empty input is deterministically ready without an SSM transfer. URLs
never enter SQLite, the spool, jobs/results, reports, Telegram, or command output. AWS/SSM control-
plane history necessarily retains the short-lived command body and the SSM Agent may handle it
transiently; this application writes no URL-bearing guest script or marker. Appliance work still
reaches the lab through the imported AMI pending a compatible OVA gate.

`AwsEc2LabProvider` hashes task and workflow identities before tagging, binds EC2's client token to
the complete immutable request plus caller idempotency key, reconciles by that key, validates every
opaque handle and ownership tag, and treats a lab as ready only after both EC2 and Systems Manager
report it usable. Launch configuration is operator input, never data extracted from Moodle text.

## CENTRAL terminal retention

CENTRAL outcomes retain provenance only after a durable job/result exists. Successful
three-role outcomes retain the existing v2 full-chain provenance. Failed or rejected
outcomes use v3 provenance for the exact ordered durable prefix: planner only,
planner/executor, or planner/executor/reviewer. The prefix binds the Moodle identity,
prepared-input manifest, job IDs, completed result digests, terminal role and status;
an executor bundle is bound only when it was durably produced.

Retention deletes only that validated scratch prefix after the controller/agent
two-owner acknowledgement protocol. A retained executor bundle is evidence-only and
is eligible after delivery plus the evidence TTL. Durable intents, barriers,
acknowledgements, and terminal receipts are audit records: they are never swept, and
a receipt prevents a retained SQLite row from being planned again.

HYBRID and IN_GUEST outcomes use an independent lab provenance contract. It binds the exact
`lab_plan`/`lab_report` durable prefix, result digests, per-job barriers, and the immutable SSM
dispatch record. Scratch retention validates that family-specific chain before the agent removes
its workspaces/results and the controller removes its jobs/dispatch. Dispatch-unknown and capacity
failures retain only the exact durable prefix; no missing job or command is synthesized. Lab job
and dispatch publication share the jobs admission lock, so capacity is refused before input
download or an SSM command.
