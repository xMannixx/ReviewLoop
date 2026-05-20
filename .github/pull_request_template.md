## Summary

- 

## Test Plan

- [ ] `python -m ruff check app.py pipeline tests`
- [ ] `python -m mypy app.py pipeline`
- [ ] `python -m pytest`

## Safety Checklist

- [ ] No `.env`, `config.toml`, API keys, private paths, or runtime data included.
- [ ] User-facing strings are neutral and, where relevant, covered by i18n.
