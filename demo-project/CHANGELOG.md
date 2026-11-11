# Changelog

## 1.2.0 - 2026-11-02
### Breaking
- `SECRET_KEY` is renamed to `APP_SECRET`. Rename it in your `.env` after upgrading.
### Added
- `python app.py --check` validates config and exits without starting the server.

## 1.1.0 - 2026-09-14
- Notes are written to `data/notes.txt`.
- Logs go to `logs/app.log`.
