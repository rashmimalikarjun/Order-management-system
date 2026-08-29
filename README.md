# Order Management System

A Flask-based order management app for users and admins, with stock control, payments, and audit logs.
## Development Progress

### Phase 1 — Core Order Management
- Core Flask-based order management functionality established.
- User and admin workflows supported.
- Stock and payment-related functionality integrated.
- Audit logging included for important administrative operations.

### Phase 2 — Financial Case Foundation
- Financial case infrastructure introduced.
- Financial cases connected with orders and supporting evidence.
- Financial risk and reasoning data incorporated into the admin workflow.
- Database changes use safe initialization/migration patterns.

### Phase 3 — Financial Case Approval
- Human approval workflow introduced for financial reasoning.
- Financial reasoning records support approval and rejection states.
- Approval and rejection decisions are recorded for auditability.
- Admin financial case detail page provides the approval workflow.

### Phase 4 — Follow-up Visibility & Failure Auditability
- Follow-up due dates surfaced in the financial case workflow.
- Overdue follow-ups are visibly flagged without automatically changing case status.
- AI analysis failures are recorded as auditable case actions.
- Failure handling preserves transaction rollback behavior.
- Phase 3 and Phase 4 regression tests pass.
## Production Readiness

This repo is now configured for deployment with:
- Environment-variable based secrets/config
- `Procfile` for WSGI startup
- `requirements.txt` for dependency install
- Session cookie hardening options

## Quick Start (Local)

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set env vars (PowerShell example):
```powershell
$env:SECRET_KEY="change-me"
$env:ADMIN_USERNAME="admin-owner"
$env:ADMIN_PASSWORD="change-me"
$env:SESSION_COOKIE_SECURE="0"
```

3. Run:
```bash
python app.py
```

## Deploy (Render/Railway/Heroku-style)

1. Build command:
```bash
pip install -r requirements.txt
```

2. Start command:
```bash
gunicorn app:app
```

3. Required env vars:
- `SECRET_KEY` (long random string)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `SESSION_COOKIE_SECURE=1` (for HTTPS)
- On Render, attach a persistent disk and set `DATABASE_PATH=/var/data/catering.db`

4. Optional env vars:
- `UPI_ID`
- `UPI_NAME`
- `DATABASE_PATH` (local default is `catering.db`; on Render use a persistent disk path)
- `PORT` (usually injected by host)
- `FLASK_DEBUG=0`

## Files Added for Deploy

- `requirements.txt`
- `Procfile`
- `.env.example`
- `.gitignore`

## Notes

- The admin login is disabled outside debug mode if `ADMIN_USERNAME=admin` and `ADMIN_PASSWORD=admin123` are still in use.
- SQLite on Render is not durable unless the database file lives on a persistent disk such as `/var/data/catering.db`.
- SQLite (`catering.db`) is fine for small/internal usage.
- For higher traffic or multi-instance deploys, migrate to PostgreSQL/MySQL.
