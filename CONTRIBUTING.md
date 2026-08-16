# Contributing

Thank you for helping improve `moodle-autotask`. Contributions are welcome when they preserve the
project's approval, identity, isolation, and lifecycle boundaries.

## Before opening an issue

- Use a public issue for reproducible bugs, documentation problems, or bounded feature proposals.
- Do not include Moodle tokens, Telegram credentials, AWS identifiers, student data, assignment
  content, private URLs, Terraform state, plans, logs, or runtime evidence.
- Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development setup

Python 3.11–3.13 is supported.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Before proposing a change, run:

```bash
python -m ruff check .
python -m mypy src tests
python -m pytest -q
```

Changes to PowerShell, PHP, Terraform, systemd units, filesystem ownership, or generated shell
payloads must also include the relevant executable/static harness used elsewhere in the test suite.

## Design rules

- Keep domain code independent of application, ports, and provider packages.
- Never bypass either human approval checkpoint.
- Keep task, revision, report, artifact, and cloud-resource identities exact and digest-bound.
- Treat delivery and cloud control-plane calls as retryable and potentially ambiguous.
- Preserve fail-closed validation for unknown files, schema drift, ownership drift, and unsafe paths.
- Do not broaden IAM, filesystem, network, or agent capabilities to make a test pass.
- Never add real provider calls to unit tests or commit anything under `.runtime/`.

## Pull requests

- Branch from current `main`.
- Keep the patch focused and document why the change is safe.
- Add tests for success, hostile input, retry, response loss, and recovery where applicable.
- Use conventional commit prefixes such as `feat:`, `fix:`, `docs:`, `test:`, or `chore:`.
- Wait for the Python matrix, root-ownership checks, and Terraform job to pass.

By contributing, you agree that your contribution is licensed under the repository's Apache-2.0
license.
