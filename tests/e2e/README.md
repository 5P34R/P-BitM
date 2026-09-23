# Local E2E testing environment

`local_e2e.py` drives a full live stack and verifies every feature end to
end: module seeding, campaign creation, victim onboarding over the public
lure URL (real Chromium via Playwright), module execution (MFA relay,
clipboard hijack, bot guard), the operator WebSocket feed, snapshot
capture against the selkies victim container, and the CLI export commands.

## Prerequisites

1. `python3 p-bitm.py setup` and `python3 p-bitm.py up --build` completed.
2. Playwright installed in the project venv and a Chromium binary
   (adjust `executable_path` in the script if not at `/usr/bin/chromium`).
3. Admin credentials printed by `p-bitm.py up` after the first start.

## Run

```bash
.venv/bin/python tests/e2e/local_e2e.py <admin_user> <admin_password>
```

The script creates uniquely named landing pages/target lists/campaigns and
prints `PASS`/`FAIL` per check with a final summary. Artifacts land in
`tests/e2e/export/` and `tests/e2e/victim-page.png` (gitignored).

Clean up the created campaigns afterwards, e.g. via the dashboard or
`DELETE /api/campaigns/{id}`.
