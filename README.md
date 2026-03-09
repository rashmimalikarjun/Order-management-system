# Order Management System

A Flask-based order management app for users and admins, with stock control, payments, and audit logs.

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
$env:ADMIN_USERNAME="admin"
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

4. Optional env vars:
- `UPI_ID`
- `UPI_NAME`
- `DATABASE_PATH` (defaults to `catering.db`)
- `PORT` (usually injected by host)
- `FLASK_DEBUG=0`

## Files Added for Deploy

- `requirements.txt`
- `Procfile`
- `.env.example`
- `.gitignore`

## Notes

- SQLite (`catering.db`) is fine for small/internal usage.
- For higher traffic or multi-instance deploys, migrate to PostgreSQL/MySQL.
