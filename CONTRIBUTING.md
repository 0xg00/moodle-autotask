# Contributing

This is a private-development repository. Keep changes small and provider-neutral.

Before proposing a change, run:

```powershell
python -m ruff check .
python -m mypy src tests
python -m pytest -q
```

Domain code must not import application, ports, or provider packages. Never add credentials,
real provider calls, or an adapter that bypasses approval and lifecycle boundaries.
