# moddle-autotask

Private-development repository for a provider-neutral task automation foundation.

Current functionality is intentionally limited to typed domain values, lifecycle rules,
approval validation, and abstract provider ports. The package does not automate Moodle,
provision AWS/KVM labs, invoke Codex, submit work, store credentials, or make network calls.

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
and binds HTTP only to `127.0.0.1:8000`.
