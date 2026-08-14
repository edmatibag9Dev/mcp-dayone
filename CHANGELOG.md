# Changelog

All notable changes to this project are documented here.

## [Unreleased] — 2026-08-14

### Fixed

- **Entries written while the Day One app was closed never synced to other devices.**
  Day One's sync engine runs inside the main app, not inside the always-running
  `com.bloombuilt.dayone-mac-agent` background process. A CLI write made with the app quit
  landed in the local store and stayed there — invisible on every other device until
  someone happened to open Day One. Scheduled captures were the common victim, since they
  fire at hours when the app is typically closed. `create_entry` now launches the app in
  the background after each write.

  Measured on 2026-08-14: an entry written with the app quit sat unuploaded for 165s and
  showed no sign of ever uploading; it reached the sync server 8s after a background
  launch, with no user interaction and no focus change.

- **Verification could stall a write by minutes.** Reads of `DayOne.sqlite` issued right
  after a write can block inside `open(2)` — a 14-minute stall was measured, during which
  even `ls` on the container directory hung. SQLite's `busy_timeout` cannot bound this,
  because the process never gets far enough to attempt a lock. Both `verify_placement` and
  `verify_upload` now run on a daemon thread under a hard time ceiling and return
  `UNVERIFIED` rather than blocking the caller.

### Added

- `DayOneTools.ensure_app_running()` — background launch of Day One by bundle ID
  (`open -g -b com.bloombuilt.dayone-mac`). No-op when already running, never steals focus,
  never raises.
- `DayOneTools.verify_upload()` / `describe_upload()` — reads back whether the sync server
  has acknowledged an entry, using `ZREMOTEENTRY` as ground truth. Reports `SYNCED`,
  `PENDING`, or `UNVERIFIED`, and states plainly that a non-synced verdict is not a reason
  to retry the write.
- Journal placement verification (`verify_placement` / `describe_placement`), previously
  uncommitted, lands with this change. The Day One CLI returns success and a UUID even when
  an entry goes to a different journal than requested; this reads the database back and
  reports `OK`, `MISPLACED`, or `UNVERIFIED`.
- `CONTRIBUTING.md` and `AGENTS.md` — repository documentation and commit standards.
- `CHANGELOG.md` — this file.

### Changed

- Default CLI path is now `dayone` rather than `dayone2`, matching the binary shipped in
  current Day One builds.
- All three creation handlers in `server.py` now report placement and sync status alongside
  the UUID.
- `README.md` restructured to the required nine sections and corrected throughout — it
  still documented the `dayone2` CLI name.

### Known limitations

- On a cold start (app quit, write launches it) the database is typically unresponsive for
  the duration of both watchdog budgets, so placement and sync usually report `UNVERIFIED`.
  The entry is still written and still syncs; only the confirmation is unavailable. Keeping
  Day One running via Login Items avoids this.
- Inline sync verification usually reports `pending`, because an idle Day One uploads on a
  periodic cycle — 174s measured — while the check is bounded at ~12s to avoid delaying the
  write. It is therefore a safety net rather than a positive confirmation, and is worded as
  routine so it does not read as a failure on every capture. The auto-launch is what
  actually prevents the never-syncs condition.
- A `SYNCED` verdict proves the server accepted the entry, not that another device has
  pulled it.
