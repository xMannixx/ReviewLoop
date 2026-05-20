# Contributing

Thanks for improving ReviewLoop.

## Local Checks

```powershell
pip install -r requirements-dev.txt
python -m ruff check app.py pipeline tests
python -m mypy app.py pipeline
python -m pytest
```

## Pull Requests

- Keep PRs focused and small.
- Include tests for behavior changes.
- Do not commit `.env`, `config.toml`, runtime data, logs, screenshots with private paths, or API keys.
- Prefer neutral product language in the UI: **Orchestrator**, **Reviewers**, **approval gates**.
- Public-facing UI text should be added to the i18n dictionaries.

## Security

If you find a private-data leak or secret-handling issue, open a private report first if GitHub security advisories are enabled. Otherwise, contact the maintainer before publishing exploit details.
