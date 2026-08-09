# moddle-autotask

Private-development repository for a provider-neutral task automation foundation.

Current functionality includes a narrow Moodle mobile-service connector: it verifies a configured
site identity, enumerates assignments and official attachment metadata, records explicit local
acknowledgements, and safely downloads selected plugin-file attachments. It does not submit work,
use password login, scrape or automate a browser, provision AWS/KVM labs, invoke Codex, or notify
anyone. The local scheduler emits compact JSON notification events to stdout as an observable
development/service-log sink; it is not Telegram, email, or user approval to execute or submit work.

The repository now also defines an isolated AWS controller baseline in Terraform. It creates a
Linux controller with no inbound network access, encrypted private storage, Systems Manager access,
an empty Secrets Manager container, and a commit/digest-bound application deployment helper. The
helper never enables the scheduler, and no lab machines are created. See
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

The connector uses Moodle's official mobile REST API, not scraping. It deliberately provides
at-least-once candidates: acknowledge only after downstream delivery succeeds.
