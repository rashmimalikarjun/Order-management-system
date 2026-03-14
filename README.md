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
