# MCP-DayOne

## 1. Project Overview

MCP-DayOne is a Model Context Protocol (MCP) server that connects Claude to a local Day One
journal on macOS. It exposes ten tools: three that create entries, five that read and search
them, and two legacy stubs that explain CLI limitations. Writes go through the `dayone`
command-line binary bundled inside the Day One app; reads go directly against Day One's
Core Data SQLite database, which is far faster and supports queries the CLI cannot express.

Every write is verified after the fact. The server reads back which journal the entry
actually landed in, confirms whether Day One has uploaded it to the sync server, and
guarantees the Day One app is running so that upload can happen at all.

## 2. Purpose

Day One has no public API. The bundled CLI can create an entry but cannot list journals,
count entries, search text, or report whether anything succeeded beyond returning a UUID.
That makes unattended automation risky: a scheduled job can "succeed" while silently
writing to the wrong journal, or while writing an entry that never leaves the machine.

This server closes both gaps so that scheduled captures — daily briefings, trading journals,
email digests — can run without a human watching. Without it, you would be trusting an exit
code that does not mean what it appears to mean.

## 3. Features

**Entry creation with metadata.** Create entries with tags, explicit dates and timezones,
starred status, GPS coordinates, all-day flags, and up to ten attachments (photos, video,
audio, PDF).

**Journal placement verification.** The CLI returns success and a UUID even when an entry
lands in a different journal than the one requested. After each write the server reads the
database back and reports `OK`, `MISPLACED`, or `UNVERIFIED`. On a mismatch it states
explicitly that the write succeeded and must not be retried — retrying is how one misplaced
entry becomes two.

**Automatic sync activation.** Day One's sync engine runs inside the *main app*, not inside
the always-running `com.bloombuilt.dayone-mac-agent` background process. A CLI write made
while the main app is closed produces an entry that exists only on that Mac, invisible on
every other device, until someone happens to open Day One. After each write the server
launches the app in the background (`open -g -b com.bloombuilt.dayone-mac`), which is a
no-op if it is already running and never steals focus from the user.

**Sync verification.** `ZREMOTEENTRY` is Day One's mirror of what the sync server has
acknowledged. A row in `ZENTRY` with no matching `ZREMOTEENTRY` row means written-locally,
never-uploaded. The server checks this and reports `SYNCED`, `PENDING`, or `UNVERIFIED`.

**Watchdog-bounded verification.** Both verifications run on a daemon thread under a hard
time ceiling. Immediately after a write — especially while the app is starting up — reads of
`DayOne.sqlite` can block inside `open(2)` for minutes; a 14-minute stall was measured.
SQLite's own `busy_timeout` cannot bound that, because the process never gets far enough to
attempt a lock. Verification is a convenience and must never delay a write that already
succeeded, so it is abandoned when it overruns its budget.

**Reading and search.** Read recent entries with full metadata, full-text search across
entry content, list journals with real entry counts, and an "On This Day" lookup that
retrieves entries from the same calendar date across previous years.

**Resilient database reads.** Reads try the live database read-only with a short bounded
wait, then fall back to a disposable snapshot copy that no other process can lock. The
`-wal` sidecar is always copied with it — a just-written entry still lives in the
write-ahead log and is invisible in a copy of the main file alone.

## 4. File Descriptions

```
src/mcp_dayone/server.py   — MCP server: tool schemas and the 10 request handlers
src/mcp_dayone/tools.py    — DayOneTools: CLI wrapper, database reads, text extraction,
                             placement/upload verification, app-launch logic
src/mcp_dayone/__init__.py — Package marker
test_setup.py              — Setup validation: CLI reachable, database readable, tools listed
pyproject.toml             — Project metadata and Python dependencies
uv.lock                    — Pinned dependency versions
smithery.toml              — Smithery packaging configuration
CLAUDE.md                  — Repository guidance for Claude Code sessions
CONTRIBUTING.md            — Commit and README standards (canonical, source of truth)
AGENTS.md                  — Agent-facing restatement of those standards
CHANGELOG.md               — Dated record of notable changes
README.md                  — This file
LICENSE                    — MIT
```

## 5. How to Use

**Prerequisites:** macOS with the Day One app installed and run at least once, Python 3.11+,
and the `uv` package manager. The `dayone` CLI ships inside the app bundle and is symlinked
at `/usr/local/bin/dayone`; no separate install is required. Verify with `dayone help`.

**Setup:**

```bash
cd mcp-dayone
uv sync
uv run python test_setup.py
```

**Configure Claude Desktop** in `~/Library/Application Support/Claude/claude_desktop_config.json`,
replacing the path with your actual checkout location:

```json
{
  "mcpServers": {
    "dayone": {
      "command": "uv",
      "args": ["--directory", "/FULL/PATH/TO/mcp-dayone", "run", "python", "-m", "mcp_dayone.server"]
    }
  }
}
```

Restart Claude Desktop afterward. The server is a long-lived process, so it must be
restarted for any code change to take effect.

**Invoking tools** happens in natural language: "Create a journal entry about my day",
"Show me my recent journal entries", "Search my journal for entries about work", "What were
my journal entries on this day?", "List my Day One journals with entry counts".

**Reading the output.** A creation reports three things: the UUID, a placement line, and a
sync line. `Placement verified` and `Sync verified` mean everything landed. A `PLACEMENT
MISMATCH` means the entry exists in the wrong journal — move it manually in the app, and do
not write again.

`Sync pending (normal)` is the ordinary result, not a warning. An idle Day One uploads on a
periodic cycle — 174 seconds measured — so a check bounded at roughly 12 seconds will
usually still be waiting. It resolves on its own. `Sync UNVERIFIED` means the database could
not be read in time, which is routine while the app is starting. In every non-`SYNCED` case
the entry already exists: never write it again, because nothing can delete the duplicate
programmatically.

## 6. Data Sources

No external or networked data sources. All data is local:

- **Day One CLI** — `/usr/local/bin/dayone`, symlinked into the Day One app bundle. Used for
  all write operations. Requires the Day One app to be installed.
- **Day One SQLite database** — `~/Library/Group Containers/5U8NS4GX82.dayoneapp2/Data/Documents/DayOne.sqlite`.
  Read-only, used for all read and verification operations. Core Data schema with `Z`-prefixed
  tables (`ZENTRY`, `ZJOURNAL`, `ZREMOTEENTRY`, `ZTAG`). Timestamps are seconds since
  2001-01-01, so add 978307200 to convert to Unix epoch.

Sync traffic to Day One's servers is handled entirely by the Day One app. This server never
contacts the network.

## 7. Known Limitations

- **The CLI cannot create journals.** `--journal` requires the journal to already exist;
  create it in the app first.
- **Entries cannot be moved or deleted programmatically.** A misplaced entry must be fixed
  by hand in the Day One app.
- **Verification is best-effort.** On a cold start — app quit, then a write that launches it
  — the database is typically unresponsive for the duration of both watchdog budgets, so
  placement and sync usually report `UNVERIFIED`. This is expected, not an error. When the
  app is already running, both checks normally succeed in seconds.
- **Sync verification proves upload, not delivery.** A `SYNCED` verdict means Day One's
  server acknowledged the entry; it does not prove another device has pulled it yet.
- **macOS only.** The database path, the `open` command, and the bundled CLI are all
  macOS-specific.
- **No automated test suite.** `test_setup.py` validates the environment, not behavior.

## 8. Workarounds

| Limitation | Workaround |
|---|---|
| CLI cannot create journals | Create the journal in the Day One app before the first automated write to it |
| Entries cannot be moved or deleted | Fix placement manually in the app; heed the "do not retry" warning so you are fixing one entry and not two |
| Cold-start verification returns `UNVERIFIED` | Keep Day One running — add it to System Settings → General → Login Items. Verification then succeeds routinely, and entries sync immediately rather than at next launch |
| `SYNCED` does not prove delivery | Open Day One on the target device; it pulls on foreground |
| No automated tests | Run `uv run python test_setup.py` after any environment change |

## 9. Build Notes

- **Runtime:** Python 3.11+ (developed and validated against 3.13 via `uv`).
- **Dependencies:** `mcp>=1.0.0`, `click>=8.0.0`, `pydantic>=2.0.0`. Install with `uv sync`;
  versions are pinned in `uv.lock`.
- **Platform:** Validated on macOS 15 (Darwin 25.5) with Day One 2026.16 (build 1774).
  Not validated on Windows or Linux, and not expected to work there.
- **No network access required** at build or run time.
- **Database access is read-only.** The database file belongs to the Day One app; a writable
  handle buys nothing and risks corrupting app state.
- **Restart required after code changes.** Claude Desktop keeps the MCP server process alive,
  so edits do not take effect until Claude Desktop is restarted.

## License

MIT
