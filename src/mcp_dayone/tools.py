"""Day One CLI operations and tools."""

import subprocess
import json
import os
import shutil
import sqlite3
import re
import tempfile
import threading
import time
import weakref
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path
import shlex


# Placement-verification verdicts (see DayOneTools.verify_placement).
PLACEMENT_OK = "OK"
PLACEMENT_MISPLACED = "MISPLACED"
PLACEMENT_UNVERIFIED = "UNVERIFIED"

# Upload-verification verdicts (see DayOneTools.verify_upload).
UPLOAD_SYNCED = "SYNCED"
UPLOAD_PENDING = "PENDING"
UPLOAD_UNVERIFIED = "UNVERIFIED"

# How long a read may wait on the live database before falling back to a snapshot.
# Kept short on purpose: the point is to avoid a multi-minute stall, not to win the race.
LIVE_READ_TIMEOUT = 5.0

# Day One's sync engine lives in the MAIN app, not in the always-running
# com.bloombuilt.dayone-mac-agent. The CLI writes happily with the main app closed, but
# nothing is uploaded until the main app next launches -- so a scheduled job that writes
# while the app is quit produces an entry that exists locally and on no other device.
# Measured 2026-08-14: entry written with the app quit sat unuploaded for 165s; it reached
# the server 8s after a background launch, with no user interaction.
DAYONE_BUNDLE_ID = "com.bloombuilt.dayone-mac"

# Seconds to wait for the `open` call itself. Launching is fire-and-forget -- this bounds
# the subprocess, not the app's startup.
APP_LAUNCH_TIMEOUT = 10.0

# Hard ceiling on journal-placement verification. See UPLOAD_VERIFY_BUDGET -- same hazard,
# and create_entry launching the app made it more likely, since the read now frequently
# lands while Day One is starting up.
PLACEMENT_VERIFY_BUDGET = 20.0

# Hard ceiling on upload verification, enforced by a watchdog thread rather than by
# sqlite's own timeout. Observed 2026-08-14: immediately after a write, while the app is
# starting and syncing, even open(2) on DayOne.sqlite blocks -- a 14-minute stall was
# measured, and `ls` on the container directory hung too. sqlite's busy_timeout cannot
# bound that, because the process never gets far enough to acquire a lock. Verification is
# a convenience; it must never hold up a write that has already succeeded.
UPLOAD_VERIFY_BUDGET = 15.0


class DayOneError(Exception):
    """Exception raised for Day One CLI errors."""
    pass


class _SnapshotConnection(sqlite3.Connection):
    """Read-only connection to a throwaway copy of the database.

    The temp directory has to outlive the connection, so cleanup is tied to the
    connection's lifetime. It cannot rely on close() alone: the read helpers in this
    module call conn.close() on the success path only, so an exception mid-query would
    leak the copy. A finalizer covers that -- the directory is removed when the
    connection is closed, or when it is garbage collected, whichever happens first.
    """

    _cleanup = None

    def _bind_temp_dir(self, temp_dir: str) -> None:
        self._cleanup = weakref.finalize(self, shutil.rmtree, temp_dir, True)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._cleanup is not None:
                self._cleanup()


class DayOneTools:
    """Wrapper for Day One CLI operations."""
    
    def __init__(self, cli_path: str = "dayone"):
        self.cli_path = cli_path
        self._verify_cli()
        self.db_path = self._get_db_path()
    
    def _verify_cli(self) -> None:
        """Verify Day One CLI is available."""
        try:
            result = subprocess.run(
                [self.cli_path, "--version"],
                capture_output=True,
                text=True,
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise DayOneError(
                f"Day One CLI '{self.cli_path}' not found or not working. "
                f"Please install Day One CLI first. Error: {e}"
            )
    
    def _get_db_path(self) -> Path:
        """Get the path to Day One database."""
        db_path = Path.home() / "Library/Group Containers/5U8NS4GX82.dayoneapp2/Data/Documents/DayOne.sqlite"
        return db_path
    
    def _get_db_connection(self) -> sqlite3.Connection:
        """Get a read-only connection to the Day One database.

        Two things this deliberately does NOT do:

        1. It never opens the database read-write. Every consumer in this module only
           SELECTs, and the file belongs to the Day One app -- a writable handle buys
           nothing and risks corrupting the app's state.
        2. It never waits indefinitely. The app holds write locks while syncing, and a
           live read blocks behind them even when opened read-only -- measured at 19 s
           during a routine sync and roughly four minutes immediately after a batch of
           writes. An MCP call that stalls for minutes is indistinguishable from a hang.

        Strategy: try the live file read-only with a short, bounded wait. That is the
        common case and costs nothing. If the database is locked, fall back to querying a
        disposable snapshot, which cannot block. Callers see an ordinary connection either
        way and do not need to know which path was taken.
        """
        if not self.db_path.exists():
            raise DayOneError(
                f"Day One database not found at {self.db_path}. "
                "Make sure Day One app is installed and has been run at least once."
            )

        # Fast path: the live database, read-only, with a bounded wait.
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=LIVE_READ_TIMEOUT
            )
            conn.row_factory = sqlite3.Row  # Enable column access by name
            conn.execute(f"PRAGMA busy_timeout={int(LIVE_READ_TIMEOUT * 1000)}")
            # sqlite3.connect() is lazy: it does not touch the file until the first
            # statement runs. Force the read lock here so contention surfaces now and can
            # be handled, instead of erupting later inside the caller's query.
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except sqlite3.Error as live_error:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

        # Fallback: a throwaway copy, which no other process can lock.
        temp_dir = tempfile.mkdtemp(prefix="mcp-dayone-read-")
        try:
            snapshot = self._snapshot_db(temp_dir)
            conn = sqlite3.connect(
                f"file:{snapshot}?mode=ro",
                uri=True,
                timeout=LIVE_READ_TIMEOUT,
                factory=_SnapshotConnection,
            )
            conn.row_factory = sqlite3.Row
            conn._bind_temp_dir(temp_dir)
            return conn
        except (OSError, sqlite3.Error) as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise DayOneError(
                f"Failed to connect to Day One database: {e}. The live database was "
                "locked and a snapshot could not be read either."
            )

    def _snapshot_db(self, dest_dir: str) -> str:
        """Copy the live database plus its WAL/SHM sidecars into dest_dir.

        Reading the live file blocks on the Day One app's write lock even when opened
        read-only -- observed at 19 seconds during a routine sync and at roughly four
        minutes immediately after a batch of writes. A scheduled, unattended run that
        stalls that long is indistinguishable from a hang, so verification always reads
        a disposable copy instead.

        Copying the `-wal` sidecar is required, not optional: a just-written entry still
        lives in the write-ahead log and is invisible in a snapshot of the main file alone.

        Returns:
            Path to the copied database.
        """
        dest = os.path.join(dest_dir, "DayOne.sqlite")
        for suffix in ("", "-wal", "-shm"):
            source = f"{self.db_path}{suffix}"
            if os.path.exists(source):
                shutil.copy2(source, f"{dest}{suffix}")
        return dest

    def verify_placement(
        self,
        entry_uuid: str,
        expected_journal: Optional[str] = None,
        attempts: int = 5,
        delay: float = 2.0,
        budget: float = PLACEMENT_VERIFY_BUDGET,
    ) -> Dict[str, Any]:
        """Check which journal an entry landed in, under a hard time ceiling.

        Same watchdog rationale as verify_upload: a read issued straight after a write can
        block in open(2) for minutes. That risk rose once create_entry started launching
        the Day One app, because the read now often lands while the app is starting up.

        Returns:
            dict as documented on _probe_placement.
        """
        box: Dict[str, Any] = {}

        def _run() -> None:
            box["result"] = self._probe_placement(
                entry_uuid, expected_journal, attempts, delay
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(budget)

        if worker.is_alive() or "result" not in box:
            return {
                "verdict": PLACEMENT_UNVERIFIED,
                "uuid": entry_uuid.strip().replace("-", "").upper(),
                "expected_journal": expected_journal,
                "actual_journal": None,
                "detail": (
                    f"Journal placement could not be read within {int(budget)}s -- the "
                    "Day One database was unresponsive, which is common while the app is "
                    "starting up or syncing."
                ),
            }
        return box["result"]

    def _probe_placement(
        self,
        entry_uuid: str,
        expected_journal: Optional[str] = None,
        attempts: int = 5,
        delay: float = 2.0,
    ) -> Dict[str, Any]:
        """Read back which journal an entry actually landed in.

        The Day One CLI returns a UUID and exit code 0 whenever it creates an entry --
        including when the entry lands in a journal other than the one `--journal` named.
        That was observed on 2026-07-30: the requested journal was correct and the entry
        materialized elsewhere, with nothing in the stack noticing. This method is the
        read-back that closes that gap.

        Never raises. A verification problem must never be reported as a creation
        failure, because by this point the entry already exists and any retry would
        create a duplicate.

        Args:
            entry_uuid: UUID returned by the CLI. Normalized to undashed uppercase.
            expected_journal: Journal name that was requested. When None, the actual
                journal is reported without a pass/fail judgement.
            attempts: How many times to re-snapshot before giving up.
            delay: Seconds between attempts, to let a pending commit land.

        Returns:
            dict with keys: verdict (OK/MISPLACED/UNVERIFIED), uuid, expected_journal,
            actual_journal, detail.
        """
        result: Dict[str, Any] = {
            "verdict": PLACEMENT_UNVERIFIED,
            "uuid": entry_uuid,
            "expected_journal": expected_journal,
            "actual_journal": None,
            "detail": "",
        }

        if not entry_uuid or not re.fullmatch(r"[0-9A-Fa-f-]{32,36}", entry_uuid.strip()):
            result["detail"] = (
                "The CLI did not return a recognizable UUID, so placement cannot be "
                "checked. The entry may still have been created -- check Day One before "
                "writing again."
            )
            return result

        # ZENTRY stores UUIDs undashed and uppercase; the column is ZUUID, not ZIDENTIFIER.
        uid = entry_uuid.strip().replace("-", "").upper()
        result["uuid"] = uid
        sql = (
            "SELECT z.ZNAME FROM ZENTRY e "
            "JOIN ZJOURNAL z ON e.ZJOURNAL = z.Z_PK "
            "WHERE e.ZUUID = ?"
        )

        last_error = None
        for attempt in range(attempts):
            tmp_dir = tempfile.mkdtemp(prefix="mcp-dayone-verify-")
            try:
                snapshot = self._snapshot_db(tmp_dir)
                conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True, timeout=5)
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    row = conn.execute(sql, (uid,)).fetchone()
                finally:
                    conn.close()
                if row:
                    result["actual_journal"] = row[0]
                    break
            except (OSError, sqlite3.Error) as e:
                last_error = e
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if attempt < attempts - 1:
                time.sleep(delay)

        actual = result["actual_journal"]
        if actual is None:
            waited = int(attempts * delay)
            result["detail"] = (
                f"No row for this UUID after ~{waited}s"
                + (f" (last error: {last_error})" if last_error else "")
                + ". The entry was still created -- do not retry, or it will be duplicated."
            )
        elif expected_journal is None:
            result["verdict"] = PLACEMENT_OK
            result["detail"] = f'Entry is in "{actual}".'
        elif actual == expected_journal:
            result["verdict"] = PLACEMENT_OK
            result["detail"] = f'Entry is in "{actual}", as requested.'
        else:
            result["verdict"] = PLACEMENT_MISPLACED
            result["detail"] = (
                f'Entry was created but landed in "{actual}" instead of the requested '
                f'"{expected_journal}".'
            )
        return result

    @staticmethod
    def describe_placement(placement: Dict[str, Any]) -> str:
        """Render a verify_placement() result as caller-facing text.

        The wording matters as much as the check. On any non-OK verdict the message has
        to say plainly that the write SUCCEEDED and must not be retried -- otherwise a
        caller reads "problem" and retries, which is precisely how a misplaced entry
        becomes two entries. Day One exposes no programmatic move or delete, so the only
        real remedy is manual.
        """
        verdict = placement.get("verdict")
        uid = placement.get("uuid")
        if verdict == PLACEMENT_OK:
            return f'Placement verified: {placement.get("detail")}'
        if verdict == PLACEMENT_MISPLACED:
            return (
                f'PLACEMENT MISMATCH -- {placement.get("detail")}\n'
                f"DO NOT retry this write. The entry exists (UUID {uid}); creating it "
                "again would duplicate it, and a journal-scoped search will not find the "
                "misplaced original. Day One cannot move entries programmatically -- "
                "move it manually in the app."
            )
        return (
            f'Placement UNVERIFIED -- {placement.get("detail")}\n'
            f"Confirm in Day One before writing anything further for UUID {uid}."
        )

    @staticmethod
    def ensure_app_running() -> bool:
        """Launch the Day One main app in the background if it is not already running.

        This is what makes an entry actually reach the user's other devices. The CLI only
        writes to the local store; the sync engine that uploads it runs inside the main
        app. A scheduled job firing while the app is quit therefore produces an entry that
        is invisible everywhere except this Mac, until the user happens to open Day One.

        `-g` keeps the launch in the background so an unattended 6 AM job never steals
        focus from whatever the user is doing. Launching an already-running app is a
        no-op, so this is safe to call on every write.

        Never raises. Failing to launch must not turn a successful write into a reported
        failure -- the entry exists either way, and a retry would duplicate it.

        Returns:
            True if the launch command succeeded, False otherwise.
        """
        try:
            subprocess.run(
                ["open", "-g", "-b", DAYONE_BUNDLE_ID],
                capture_output=True,
                text=True,
                check=True,
                timeout=APP_LAUNCH_TIMEOUT,
            )
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    def verify_upload(
        self,
        entry_uuid: str,
        attempts: int = 6,
        delay: float = 2.0,
        budget: float = UPLOAD_VERIFY_BUDGET,
    ) -> Dict[str, Any]:
        """Check whether an entry reached the sync server, under a hard time ceiling.

        Delegates to _probe_upload on a daemon thread and abandons it if it overruns
        `budget`. That watchdog is not defensive padding -- a database read issued right
        after a write can block in open(2) for many minutes (14 measured), which would
        otherwise stall every journal write by that long. An abandoned thread dies with
        the process; the temp snapshot it may hold is cleaned up by _SnapshotConnection's
        finalizer.

        Args:
            entry_uuid: UUID returned by the CLI.
            attempts: Re-checks before concluding the upload has not landed.
            delay: Seconds between attempts.
            budget: Hard ceiling in seconds across all attempts.

        Returns:
            dict with keys: verdict (SYNCED/PENDING/UNVERIFIED), uuid, detail.
        """
        box: Dict[str, Any] = {}

        def _run() -> None:
            box["result"] = self._probe_upload(entry_uuid, attempts, delay)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(budget)

        if worker.is_alive() or "result" not in box:
            return {
                "verdict": UPLOAD_UNVERIFIED,
                "uuid": entry_uuid.strip().replace("-", "").upper(),
                "detail": (
                    f"Sync state could not be read within {int(budget)}s -- the Day One "
                    "database was unresponsive, which is common while the app is starting "
                    "up or syncing."
                ),
            }
        return box["result"]

    def _probe_upload(
        self,
        entry_uuid: str,
        attempts: int = 6,
        delay: float = 2.0,
    ) -> Dict[str, Any]:
        """Unbounded upload read-back. Call verify_upload() instead, not this directly.

        ZREMOTEENTRY is the app's mirror of what the server has acknowledged. A row in
        ZENTRY with no matching ZREMOTEENTRY row means the entry was written locally and
        never uploaded -- the exact failure this check exists to catch, and one that is
        otherwise completely silent: the CLI returns success, the entry is visible in the
        Mac app, and only the user's phone knows anything is wrong.

        This can block for minutes on an unresponsive database, which is why verify_upload
        runs it under a watchdog rather than calling it inline.

        Never raises, for the same reason verify_placement does not: by this point the
        entry exists, so a verification problem must never be reported as a write failure.

        Args:
            entry_uuid: UUID returned by the CLI. Normalized to undashed uppercase.
            attempts: How many times to re-check before giving up.
            delay: Seconds between attempts. Upload was measured at ~8s from a cold app
                launch, so ~12s of polling covers the common case.

        Returns:
            dict with keys: verdict (SYNCED/PENDING/UNVERIFIED), uuid, detail.
        """
        result: Dict[str, Any] = {
            "verdict": UPLOAD_UNVERIFIED,
            "uuid": entry_uuid,
            "detail": "",
        }

        if not entry_uuid or not re.fullmatch(r"[0-9A-Fa-f-]{32,36}", entry_uuid.strip()):
            result["detail"] = (
                "The CLI did not return a recognizable UUID, so upload cannot be checked."
            )
            return result

        uid = entry_uuid.strip().replace("-", "").upper()
        result["uuid"] = uid

        last_error = None
        for attempt in range(attempts):
            conn = None
            try:
                conn = self._get_db_connection()
                row = conn.execute(
                    "SELECT COUNT(*) FROM ZREMOTEENTRY WHERE ZUUID = ?", (uid,)
                ).fetchone()
                if row and row[0]:
                    result["verdict"] = UPLOAD_SYNCED
                    result["detail"] = (
                        f"Entry reached the Day One sync server after ~{int(attempt * delay)}s."
                    )
                    return result
            except (DayOneError, sqlite3.Error) as e:
                last_error = e
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
            if attempt < attempts - 1:
                time.sleep(delay)

        waited = int(attempts * delay)
        if last_error is not None:
            result["detail"] = f"Could not read sync state after ~{waited}s: {last_error}"
            return result

        result["verdict"] = UPLOAD_PENDING
        result["detail"] = (
            f"Entry is stored locally but the sync server has not acknowledged it after "
            f"~{waited}s."
        )
        return result

    @staticmethod
    def describe_upload(upload: Dict[str, Any]) -> str:
        """Render a verify_upload() result as caller-facing text.

        As with describe_placement, the wording has to keep a caller from "fixing" a sync
        delay by writing the entry again. Upload is asynchronous: PENDING frequently just
        means the push had not finished within the check budget, and it resolves on its
        own. Re-writing would duplicate the entry permanently.
        """
        verdict = upload.get("verdict")
        uid = upload.get("uuid")
        if verdict == UPLOAD_SYNCED:
            return f'Sync verified: {upload.get("detail")}'
        if verdict == UPLOAD_PENDING:
            # PENDING is the ordinary outcome, not a warning. An idle Day One uploads on a
            # periodic cycle -- 174s measured on 2026-08-14 -- so a check bounded at ~12s
            # will normally still be waiting. Phrasing this as a problem would put a false
            # alarm on nearly every capture, and the one thing a caller must not do in
            # response is write the entry again.
            return (
                f'Sync pending (normal): {upload.get("detail")} '
                "An idle Day One uploads on a periodic cycle, so this usually resolves on "
                "its own within a few minutes. No action needed, and do not write the "
                "entry again."
            )
        return (
            f'Sync UNVERIFIED -- {upload.get("detail")}\n'
            f"The entry itself was created (UUID {uid}). Do not write it again; check Day "
            "One directly. If entries never sync, confirm the Day One app is running -- "
            "its sync engine does not run while the app is quit."
        )

    def create_entry(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        date: Optional[str] = None,
        journal: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        starred: Optional[bool] = None,
        coordinates: Optional[Dict[str, float]] = None,
        timezone: Optional[str] = None,
        all_day: Optional[bool] = None
    ) -> str:
        """Create a new Day One journal entry.
        
        Args:
            content: The entry text content
            tags: Optional list of tags to add
            date: Optional date string (YYYY-MM-DD HH:MM:SS format)
            journal: Optional journal name
            attachments: Optional list of file paths to attach (up to 10)
            starred: Optional flag to mark entry as starred
            coordinates: Optional dict with 'latitude' and 'longitude' keys
            timezone: Optional timezone string
            all_day: Optional flag to mark as all-day event
            
        Returns:
            UUID of the created entry
            
        Raises:
            DayOneError: If entry creation fails
        """
        if not content.strip():
            raise DayOneError("Entry content cannot be empty")
        
        # Validate attachments
        if attachments:
            if len(attachments) > 10:
                raise DayOneError("Maximum 10 attachments allowed per entry")
            
            for attachment in attachments:
                if not os.path.exists(attachment):
                    raise DayOneError(f"Attachment file not found: {attachment}")
        
        # Validate coordinates
        if coordinates:
            if 'latitude' not in coordinates or 'longitude' not in coordinates:
                raise DayOneError("Coordinates must include both 'latitude' and 'longitude'")
        
        # Build command
        cmd = [self.cli_path]
        
        # Add attachments
        if attachments:
            cmd.extend(["--attachments"] + attachments)
        
        # Add tags
        if tags:
            cmd.extend(["--tags"] + tags)
        
        # Add journal
        if journal:
            cmd.extend(["--journal", journal])
        
        # Add date
        if date:
            cmd.extend(["--date", date])
        
        # Add starred flag
        if starred:
            cmd.append("--starred")
        
        # Add coordinates
        if coordinates:
            coord_str = f"{coordinates['latitude']} {coordinates['longitude']}"
            cmd.extend(["--coordinate", coord_str])
        
        # Add timezone
        if timezone:
            cmd.extend(["--time-zone", timezone])
        
        # Add all-day flag
        if all_day:
            cmd.append("--all-day")
        
        # Add the command and content
        cmd.extend(["new", content])
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            # The write only reached the local store. Day One's sync engine runs inside
            # the main app, so unless that app is running this entry will not reach any
            # other device -- silently, and for as long as the app stays closed. Launching
            # it here (backgrounded, no-op if already running) is what makes a scheduled,
            # unattended write actually sync.
            self.ensure_app_running()

            # Extract UUID from output
            output = result.stdout.strip()
            if "Created new entry with uuid:" in output:
                uuid = output.split("uuid:")[-1].strip()
                return uuid
            else:
                return output

        except subprocess.CalledProcessError as e:
            raise DayOneError(f"Failed to create entry: {e.stderr}")
    
    def list_journals(self) -> List[str]:
        """List available journals.
        
        Note: Day One CLI doesn't provide a direct way to list journals.
        This method returns a helpful message explaining the limitation.
        
        Returns:
            List with explanatory message
            
        Raises:
            DayOneError: If there's an issue
        """
        # Day One CLI doesn't have a journals command
        return [
            "Day One CLI doesn't provide a command to list journals.",
            "You can specify a journal name using the --journal parameter when creating entries.",
            "If no journal is specified, entries go to the default journal."
        ]
    
    def get_entry_count(self, journal: Optional[str] = None) -> int:
        """Get total number of entries.
        
        Note: Day One CLI doesn't provide a command to count entries.
        
        Args:
            journal: Optional journal name to count entries for
            
        Returns:
            Always returns -1 to indicate this functionality is not available
            
        Raises:
            DayOneError: If there's an issue
        """
        # Day One CLI doesn't have a list command
        raise DayOneError(
            "Day One CLI doesn't provide a command to count entries. "
            "You can view entry counts through the Day One app interface."
        )
    
    def read_recent_entries(self, limit: int = 10, journal: Optional[str] = None) -> List[Dict[str, Any]]:
        """Read recent journal entries from the database.
        
        Args:
            limit: Maximum number of entries to return
            journal: Optional journal name to filter by
            
        Returns:
            List of entry dictionaries with metadata
            
        Raises:
            DayOneError: If database access fails
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Base query to get entries with journal information
            query = """
            SELECT 
                e.ZUUID as uuid,
                e.ZRICHTEXTJSON as rich_text,
                e.ZMARKDOWNTEXT as markdown_text,
                e.ZCREATIONDATE as creationDate,
                e.ZMODIFIEDDATE as modifiedDate,
                e.ZSTARRED as starred,
                e.ZTIMEZONE as timeZone,
                j.ZNAME as journal_name,
                e.ZLOCATION as location,
                e.ZWEATHER as weather
            FROM ZENTRY e
            LEFT JOIN ZJOURNAL j ON e.ZJOURNAL = j.Z_PK
            """
            
            params = []
            if journal:
                query += " WHERE j.ZNAME = ?"
                params.append(journal)
            
            query += " ORDER BY e.ZCREATIONDATE DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            entries = []
            
            for row in cursor.fetchall():
                # Extract text from rich text JSON or markdown
                text_content = self._extract_text_content(row['rich_text'], row['markdown_text'])
                
                entry = {
                    'uuid': row['uuid'],
                    'text': text_content or '',
                    'creation_date': datetime.fromtimestamp(row['creationDate'] + 978307200) if row['creationDate'] else None,  # Convert from Core Data timestamp
                    'modified_date': datetime.fromtimestamp(row['modifiedDate'] + 978307200) if row['modifiedDate'] else None,
                    'starred': bool(row['starred']),
                    'timezone': str(row['timeZone']) if row['timeZone'] else None,
                    'journal_name': row['journal_name'] or 'Default',
                    'has_location': bool(row['location']),
                    'has_weather': bool(row['weather'])
                }
                
                # Get tags for this entry
                entry['tags'] = self._get_entry_tags(cursor, row['uuid'])
                
                entries.append(entry)
            
            conn.close()
            return entries
            
        except sqlite3.Error as e:
            raise DayOneError(f"Failed to read entries from database: {e}")
    
    def _get_entry_tags(self, cursor: sqlite3.Cursor, entry_uuid: str) -> List[str]:
        """Get tags for a specific entry."""
        try:
            cursor.execute("""
                SELECT t.ZNAME 
                FROM ZTAG t
                JOIN Z_13TAGS zt ON t.Z_PK = zt.Z_55TAGS1
                JOIN ZENTRY e ON zt.Z_13ENTRIES = e.Z_PK
                WHERE e.ZUUID = ?
            """, (entry_uuid,))
            
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error:
            return []
    
    def search_entries(self, search_text: str, limit: int = 20, journal: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search journal entries by text content.
        
        Args:
            search_text: Text to search for in entry content
            limit: Maximum number of entries to return
            journal: Optional journal name to filter by
            
        Returns:
            List of entry dictionaries matching the search
            
        Raises:
            DayOneError: If database access fails
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            query = """
            SELECT 
                e.ZUUID as uuid,
                e.ZRICHTEXTJSON as rich_text,
                e.ZMARKDOWNTEXT as markdown_text,
                e.ZCREATIONDATE as creationDate,
                e.ZMODIFIEDDATE as modifiedDate,
                e.ZSTARRED as starred,
                e.ZTIMEZONE as timeZone,
                j.ZNAME as journal_name
            FROM ZENTRY e
            LEFT JOIN ZJOURNAL j ON e.ZJOURNAL = j.Z_PK
            WHERE (e.ZRICHTEXTJSON LIKE ? OR e.ZMARKDOWNTEXT LIKE ?)
            """
            
            params = [f'%{search_text}%', f'%{search_text}%']
            
            if journal:
                query += " AND j.ZNAME = ?"
                params.append(journal)
            
            query += " ORDER BY e.ZCREATIONDATE DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            entries = []
            
            for row in cursor.fetchall():
                # Extract text from rich text JSON or markdown
                text_content = self._extract_text_content(row['rich_text'], row['markdown_text'])
                
                entry = {
                    'uuid': row['uuid'],
                    'text': text_content or '',
                    'creation_date': datetime.fromtimestamp(row['creationDate'] + 978307200) if row['creationDate'] else None,
                    'modified_date': datetime.fromtimestamp(row['modifiedDate'] + 978307200) if row['modifiedDate'] else None,
                    'starred': bool(row['starred']),
                    'timezone': str(row['timeZone']) if row['timeZone'] else None,
                    'journal_name': row['journal_name'] or 'Default'
                }
                
                entry['tags'] = self._get_entry_tags(cursor, row['uuid'])
                entries.append(entry)
            
            conn.close()
            return entries
            
        except sqlite3.Error as e:
            raise DayOneError(f"Failed to search entries: {e}")
    
    def list_journals_from_db(self) -> List[Dict[str, Any]]:
        """List all journals from the database with entry counts.
        
        Returns:
            List of journal dictionaries with metadata
            
        Raises:
            DayOneError: If database access fails
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    j.ZNAME as name,
                    j.ZUUIDFORAUXILIARYSYNC as uuid,
                    COUNT(e.Z_PK) as entry_count,
                    MAX(e.ZCREATIONDATE) as last_entry_date
                FROM ZJOURNAL j
                LEFT JOIN ZENTRY e ON e.ZJOURNAL = j.Z_PK
                GROUP BY j.Z_PK, j.ZNAME, j.ZUUIDFORAUXILIARYSYNC
                ORDER BY j.ZNAME
            """)
            
            journals = []
            for row in cursor.fetchall():
                journal = {
                    'name': row['name'],
                    'uuid': row['uuid'],
                    'entry_count': row['entry_count'],
                    'last_entry_date': datetime.fromtimestamp(row['last_entry_date'] + 978307200) if row['last_entry_date'] else None
                }
                journals.append(journal)
            
            conn.close()
            return journals
            
        except sqlite3.Error as e:
            raise DayOneError(f"Failed to list journals from database: {e}")
    
    def get_entry_count_from_db(self, journal: Optional[str] = None) -> int:
        """Get actual entry count from database.
        
        Args:
            journal: Optional journal name to count entries for
            
        Returns:
            Number of entries
            
        Raises:
            DayOneError: If database access fails
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            if journal:
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM ZENTRY e
                    JOIN ZJOURNAL j ON e.ZJOURNAL = j.Z_PK
                    WHERE j.ZNAME = ?
                """, (journal,))
            else:
                cursor.execute("SELECT COUNT(*) FROM ZENTRY")
            
            count = cursor.fetchone()[0]
            conn.close()
            return count
            
        except sqlite3.Error as e:
            raise DayOneError(f"Failed to count entries from database: {e}")
    
    def _extract_text_content(self, rich_text_json: Optional[str], markdown_text: Optional[str]) -> str:
        """Extract readable text content from Day One's rich text JSON or markdown.
        
        Args:
            rich_text_json: Rich text JSON string from Day One
            markdown_text: Markdown text alternative
            
        Returns:
            Extracted text content
        """
        if not rich_text_json and not markdown_text:
            return ""
        
        # Try to extract from rich text JSON first
        if rich_text_json:
            try:
                rich_data = json.loads(rich_text_json)
                
                # Handle different rich text JSON structures
                if isinstance(rich_data, dict):
                    # Look for common text fields in Day One's rich text format
                    if 'text' in rich_data:
                        return str(rich_data['text']).strip()
                    
                    # Handle attributedString format
                    if 'attributedString' in rich_data:
                        attr_string = rich_data['attributedString']
                        if isinstance(attr_string, dict) and 'string' in attr_string:
                            return str(attr_string['string']).strip()
                    
                    # Handle ops format (similar to Quill.js delta format)
                    if 'ops' in rich_data:
                        text_parts = []
                        for op in rich_data['ops']:
                            if isinstance(op, dict) and 'insert' in op:
                                insert_value = op['insert']
                                if isinstance(insert_value, str):
                                    text_parts.append(insert_value)
                                elif isinstance(insert_value, dict) and 'text' in insert_value:
                                    text_parts.append(str(insert_value['text']))
                        return ''.join(text_parts).strip()
                    
                    # Handle delta format
                    if 'delta' in rich_data:
                        delta = rich_data['delta']
                        if isinstance(delta, dict) and 'ops' in delta:
                            text_parts = []
                            for op in delta['ops']:
                                if isinstance(op, dict) and 'insert' in op:
                                    text_parts.append(str(op['insert']))
                            return ''.join(text_parts).strip()
                    
                    # Handle NSAttributedString format (macOS native)
                    if 'NSString' in rich_data:
                        return str(rich_data['NSString']).strip()
                    
                    # Fallback: try to find any string values in the JSON
                    def extract_strings(obj, max_depth=3):
                        if max_depth <= 0:
                            return []
                        
                        strings = []
                        if isinstance(obj, str) and len(obj.strip()) > 0:
                            strings.append(obj.strip())
                        elif isinstance(obj, dict):
                            for value in obj.values():
                                strings.extend(extract_strings(value, max_depth - 1))
                        elif isinstance(obj, list):
                            for item in obj:
                                strings.extend(extract_strings(item, max_depth - 1))
                        return strings
                    
                    extracted_strings = extract_strings(rich_data)
                    if extracted_strings:
                        # Return the longest meaningful string
                        meaningful_strings = [s for s in extracted_strings if len(s) > 10]
                        if meaningful_strings:
                            return max(meaningful_strings, key=len)
                        elif extracted_strings:
                            return extracted_strings[0]
                
                elif isinstance(rich_data, str):
                    return rich_data.strip()
                
            except (json.JSONDecodeError, KeyError, TypeError):
                # If JSON parsing fails, try to extract plain text from the raw string
                if rich_text_json.strip():
                    # Remove common JSON artifacts and extract readable text
                    # Remove JSON structure characters but keep content
                    cleaned = re.sub(r'[{}\[\]"]', ' ', rich_text_json)
                    cleaned = re.sub(r'\\n', '\n', cleaned)
                    cleaned = re.sub(r'\\t', '\t', cleaned)
                    cleaned = re.sub(r'\s+', ' ', cleaned)
                    
                    # Look for sentences (text with punctuation and reasonable length)
                    sentences = re.findall(r'[A-Z][^.!?]*[.!?]', cleaned)
                    if sentences:
                        return ' '.join(sentences[:3]).strip()  # First few sentences
                    
                    # Fallback to first meaningful chunk
                    words = cleaned.split()
                    meaningful_words = [w for w in words if len(w) > 2 and w.isalpha()]
                    if len(meaningful_words) >= 5:
                        return ' '.join(meaningful_words[:20]).strip()
        
        # Fallback to markdown text
        if markdown_text:
            return markdown_text.strip()
        
        return ""
    
    def get_entries_by_date(self, target_date: str, years_back: int = 5) -> List[Dict[str, Any]]:
        """Get journal entries for a specific date across multiple years ('On This Day').
        
        Args:
            target_date: Target date in MM-DD format (e.g., '06-14' for June 14th)
            years_back: How many years back to search (default 5)
            
        Returns:
            List of entries from this date in previous years
            
        Raises:
            DayOneError: If database access fails
        """
        try:
            # Parse and validate date format
            from datetime import datetime, timedelta
            
            # Handle different date formats
            if len(target_date) == 5 and '-' in target_date:  # MM-DD
                month, day = target_date.split('-')
            elif len(target_date) == 10:  # YYYY-MM-DD
                month, day = target_date.split('-')[1:3]
            else:
                # Try parsing various formats
                try:
                    parsed_date = datetime.strptime(target_date, '%Y-%m-%d')
                    month, day = f"{parsed_date.month:02d}", f"{parsed_date.day:02d}"
                except ValueError:
                    try:
                        parsed_date = datetime.strptime(target_date, '%m-%d')
                        month, day = f"{parsed_date.month:02d}", f"{parsed_date.day:02d}"
                    except ValueError:
                        raise DayOneError(f"Invalid date format: {target_date}. Use MM-DD or YYYY-MM-DD format.")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # Get current year to search backwards
            current_year = datetime.now().year
            
            # Build query to find entries on this date across multiple years
            date_conditions = []
            params = []
            
            for year in range(current_year - years_back, current_year + 1):
                # Create date range for the full day
                start_date = datetime(year, int(month), int(day))
                end_date = start_date + timedelta(days=1)
                
                # Convert to Core Data timestamp (seconds since 2001-01-01)
                start_timestamp = (start_date.timestamp() - 978307200)
                end_timestamp = (end_date.timestamp() - 978307200)
                
                date_conditions.append("(e.ZCREATIONDATE >= ? AND e.ZCREATIONDATE < ?)")
                params.extend([start_timestamp, end_timestamp])
            
            query = f"""
            SELECT 
                e.ZUUID as uuid,
                e.ZRICHTEXTJSON as rich_text,
                e.ZMARKDOWNTEXT as markdown_text,
                e.ZCREATIONDATE as creationDate,
                e.ZMODIFIEDDATE as modifiedDate,
                e.ZSTARRED as starred,
                e.ZTIMEZONE as timeZone,
                j.ZNAME as journal_name,
                e.ZLOCATION as location,
                e.ZWEATHER as weather
            FROM ZENTRY e
            LEFT JOIN ZJOURNAL j ON e.ZJOURNAL = j.Z_PK
            WHERE ({' OR '.join(date_conditions)})
            ORDER BY e.ZCREATIONDATE DESC
            """
            
            cursor.execute(query, params)
            entries = []
            
            for row in cursor.fetchall():
                # Extract text content
                text_content = self._extract_text_content(row['rich_text'], row['markdown_text'])
                
                entry_date = datetime.fromtimestamp(row['creationDate'] + 978307200) if row['creationDate'] else None
                
                entry = {
                    'uuid': row['uuid'],
                    'text': text_content or '',
                    'creation_date': entry_date,
                    'modified_date': datetime.fromtimestamp(row['modifiedDate'] + 978307200) if row['modifiedDate'] else None,
                    'starred': bool(row['starred']),
                    'timezone': str(row['timeZone']) if row['timeZone'] else None,
                    'journal_name': row['journal_name'] or 'Default',
                    'has_location': bool(row['location']),
                    'has_weather': bool(row['weather']),
                    'year': entry_date.year if entry_date else None,
                    'years_ago': current_year - entry_date.year if entry_date else None
                }
                
                # Get tags for this entry
                entry['tags'] = self._get_entry_tags(cursor, row['uuid'])
                
                entries.append(entry)
            
            conn.close()
            return entries
            
        except sqlite3.Error as e:
            raise DayOneError(f"Failed to get entries by date: {e}")
        except ValueError as e:
            raise DayOneError(f"Date parsing error: {e}")