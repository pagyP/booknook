# Copilot Instructions

## Commands

```bash
# Run all tests
pytest

# Run a single test class or test
pytest tests/test_app.py::TestAuthentication
pytest tests/test_app.py::TestAuthentication::test_login_success

# Run with verbose output
pytest -v

# Lint
flake8 .
```

## Architecture

BookNook is a **monolithic Flask app** — all routes, models, and business logic live in `app.py` (~2000 lines). There is no blueprint structure.

**Database models:** `User`, `Book`, `RecoveryCode`, `TrustedDevice` — all defined in `app.py` via SQLAlchemy. `init_db.py` handles schema initialization.

**Request flow:** Route handler → WTForms validation → SQLAlchemy DB operation → Jinja2 template render. All forms use Flask-WTF for CSRF protection.

**Templates** extend `base.html` (Georgia serif font, warm/cozy color palette). `isbn_lookup_script.html` is a partial included by the book forms, not a standalone page.

**Test fixtures** (`tests/conftest.py`) use **SQLite** (`test.db`) with CSRF and rate limiting disabled. Each test gets a fresh DB via `db.drop_all()` / `db.create_all()`. The `auth_user` fixture registers and logs in a test user.

## Key Conventions

**Security patterns — do not deviate from these:**
- Passwords: Argon2 only (`argon2-cffi`), never bcrypt or werkzeug hashing.
- MFA secrets: always encrypt at rest via `derive_encryption_key()` (PBKDF2HMAC, 480k iterations) before storing.
- Recovery codes: base32-encoded, hashed before storage, single-use, formatted `XXX-XXX-XXX`.
- Signed tokens (password reset, recovery code flow): use `itsdangerous` with explicit short expiry.

**Data isolation:** Every book query must be scoped to the current user — always `filter_by(user_id=current_user.id)`. Never fetch books without a user filter.

**Cover URL handling:** `cover_url` stores either an Open Library CDN URL (`https://covers.openlibrary.org/...`) or a local upload path (`/static/uploads/covers/{uuid}.ext`). Use `validate_cover_url()` for CDN URLs only. Uploaded files go through `save_cover_upload()` (validates extension + Pillow verify) and are stored in `static/uploads/covers/`. Call `delete_local_cover(url)` before replacing or when deleting a book so orphaned files are cleaned up.

**Rate limiting:** Applied via Flask-Limiter decorators on sensitive routes (login: 5/min, email change: 10/hour). New auth routes should follow the same pattern.

**Genre handling:** Genres are stored as a comma-separated string; use `normalize_genre_input()` when reading user-supplied genre input.

**Open Library integration:** `fetch_book_from_open_library(isbn)` performs a two-stage fetch (search.json → works/{key}.json for subjects). It has a 3-second timeout and returns `None` gracefully on failure.

**Device trust:** `generate_device_fingerprint()` hashes User-Agent + IP. Trusted devices expire after 30 days.

## Environment

Requires PostgreSQL — there is no SQLite fallback for production. Dev setup uses `docker-compose.yml` (Flask + PostgreSQL). Production uses `docker-compose.prod.yml` (Gunicorn + Nginx + PostgreSQL). Copy `.env.example` to `.env` before running.

Required env vars: `SECRET_KEY`, `DATABASE_URL`, `ENCRYPTION_KEY`.
