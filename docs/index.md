# ReviewLoop

ReviewLoop is a local control room for AI-assisted code review loops.

## Start

```powershell
docker compose up --build
```

Open `http://localhost:5000`.

## Key Ideas

- Use the demo provider for first-run onboarding.
- Add real provider keys only when you are ready.
- Keep local secrets in `.env` and local paths in `config.toml`.
- Approve each handoff before the next phase starts.

Continue with [Concepts](concepts.md).
