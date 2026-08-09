# moddle-autotask

Private-development repository for a provider-neutral task automation foundation.

Current functionality includes a narrow Moodle mobile-service connector: it verifies a configured
site identity, enumerates assignments and official attachment metadata, records explicit local
acknowledgements, and safely downloads selected plugin-file attachments. It does not submit work,
use password login, scrape or automate a browser, or invoke Codex. The scheduler can emit compact
JSON locally or send an outbound Telegram message with `Hacer tarea`, `Ignorar`, and `Ver detalles`
buttons. An exact `Hacer tarea` decision atomically creates durable work. The controller selects
`central`, `hybrid`, or `in_guest`; modes requiring a machine provision one idempotent Windows lab,
wait for Systems Manager, and enforce a two-hour cleanup deadline. Agent execution and Moodle
submission remain separate, unimplemented approval boundaries. For a practice with exactly one
`.ova`, the worker re-reads the approved Moodle revision, verifies and stages every attachment in
private S3, imports the appliance through AWS VM Import/Export with an idempotent token, and launches
only the resulting owned AMI. Other virtual-disk layouts remain blocked; a blank Windows lab is never
substituted for an appliance.

The repository now also defines an isolated AWS controller and ephemeral lab boundary in Terraform.
It creates a Linux controller with no inbound network access, encrypted private storage, Systems
Manager access, empty Moodle and Telegram Secrets Manager containers, separate capability-limited
lab roles, and a commit/digest-bound deployment helper. Deployment never enables the services, and the boundary
does not launch a lab without an approved application workflow. See
[AWS controller documentation](docs/aws.md).

Future adapters must preserve the capability-limited `AgentRuntime`, opaque `LabHandle`,
idempotency-keyed lab operations, and dual approval checkpoints defined here.

## Development

Requires Python 3.11–3.13.

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m mypy src tests
python -m pytest -q
```

See [architecture documentation](docs/architecture.md), [contributing guidance](CONTRIBUTING.md),
and [security policy](SECURITY.md).

## Local Moodle integration environment

The development-only Moodle integration stack is documented in
[docs/local-moodle.md](docs/local-moodle.md). It uses pinned official Moodle sources, PostgreSQL,
and binds HTTP to `127.0.0.1:8000` by default, with an explicit validated private/Tailscale opt-in.
Its deterministic fictitious ASIX catalog provides 12 assignments, varied deadlines, and safe
sample attachments including a metadata-only `.ova`; development does not depend on active tasks
or credentials from a real school Moodle.

The connector uses Moodle's official mobile REST API, not scraping. It deliberately provides
at-least-once candidates: acknowledge only after downstream delivery succeeds.
