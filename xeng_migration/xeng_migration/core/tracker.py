"""tracker.py - SQLite state & audit backbone for the xENG migration.

Thread-safe: a single connection guarded by an RLock, safe for the
multi-worker document upload phase.

Scan-driven model:
  - `documents` rows come from SCANNING the network drive.
  - `records` rows come from SLFE's metadata records (no paths needed).
  - `match` links records to documents by file name.

Project status flow:
    PENDING -> MASTER_CREATED -> PROJECT_CREATED -> RELATED -> COMPLETE
Document status flow:
    PENDING -> LOADED -> REVISION_SET -> COMPLETE
Any step may go -> FAILED. Document match_status: UNMATCHED|MATCHED|AMBIGUOUS
"""

from __future__ import annotations

import sqlite3
import datetime
import threading


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Tracker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_number   TEXT PRIMARY KEY,
                    project_title    TEXT,
                    program          TEXT,
                    business_line    TEXT,
                    extra_json       TEXT,
                    master_id        INTEGER,
                    project_id       INTEGER,
                    status           TEXT NOT NULL DEFAULT 'PENDING',
                    error            TEXT,
                    migration_start  TEXT,
                    migration_end    TEXT,
                    created_at       TEXT,
                    updated_at       TEXT
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_number   TEXT NOT NULL,
                    file_name        TEXT NOT NULL,
                    folder_path      TEXT,
                    source_path      TEXT,
                    file_size        INTEGER,
                    match_status     TEXT NOT NULL DEFAULT 'UNMATCHED',
                    record_id        INTEGER,
                    revision_number  TEXT,
                    revision_stage   TEXT,
                    meta_json        TEXT,
                    target_folder_id INTEGER,
                    document_id      INTEGER,
                    rev_status_id    INTEGER,
                    status           TEXT NOT NULL DEFAULT 'PENDING',
                    error            TEXT,
                    created_at       TEXT,
                    updated_at       TEXT,
                    UNIQUE(project_number, folder_path, file_name)
                );

                CREATE TABLE IF NOT EXISTS records (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_number   TEXT NOT NULL,
                    file_name        TEXT NOT NULL,
                    revision_number  TEXT,
                    revision_stage   TEXT,
                    meta_json        TEXT,
                    match_status     TEXT NOT NULL DEFAULT 'UNMATCHED',
                    matched_doc_id   INTEGER,
                    created_at       TEXT,
                    updated_at       TEXT,
                    UNIQUE(project_number, file_name)
                );

                CREATE TABLE IF NOT EXISTS run_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT,
                    level       TEXT,
                    scope       TEXT,
                    message     TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_doc_proj ON documents(project_number);
                CREATE INDEX IF NOT EXISTS ix_doc_status ON documents(status);
                CREATE INDEX IF NOT EXISTS ix_doc_match ON documents(match_status);
                CREATE INDEX IF NOT EXISTS ix_rec_proj ON records(project_number);
                CREATE INDEX IF NOT EXISTS ix_proj_status ON projects(status);
                """
            )
            # add columns to pre-existing databases
            cols = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(projects)").fetchall()}
            for col in ("migration_start", "migration_end"):
                if col not in cols:
                    self.conn.execute(
                        f"ALTER TABLE projects ADD COLUMN {col} TEXT")
            dcols = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(documents)").fetchall()}
            if "file_size" not in dcols:
                self.conn.execute(
                    "ALTER TABLE documents ADD COLUMN file_size INTEGER")
            self.conn.commit()

    # ---------- projects ----------
    def upsert_project(self, project_number, project_title, program,
                       business_line, extra_json):
        with self._lock:
            row = self.conn.execute(
                "SELECT project_number FROM projects WHERE project_number=?",
                (project_number,)).fetchone()
            if row:
                self.conn.execute(
                    """UPDATE projects SET project_title=?, program=?,
                       business_line=?, extra_json=?, updated_at=?
                       WHERE project_number=?""",
                    (project_title, program, business_line, extra_json,
                     _now(), project_number))
            else:
                self.conn.execute(
                    """INSERT INTO projects(project_number, project_title,
                       program, business_line, extra_json, status,
                       created_at, updated_at)
                       VALUES(?,?,?,?,?, 'PENDING', ?, ?)""",
                    (project_number, project_title, program, business_line,
                     extra_json, _now(), _now()))
            self.conn.commit()

    # ---------- documents (from scan) ----------
    def upsert_scanned_document(self, project_number, file_name, folder_path,
                                source_path, file_size=None):
        with self._lock:
            row = self.conn.execute(
                """SELECT id FROM documents
                   WHERE project_number=? AND folder_path IS ? AND file_name=?""",
                (project_number, folder_path, file_name)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE documents SET source_path=?, file_size=?, "
                    "updated_at=? WHERE id=?",
                    (source_path, file_size, _now(), row["id"]))
                self.conn.commit()
                return row["id"], False
            cur = self.conn.execute(
                """INSERT INTO documents(project_number, file_name, folder_path,
                   source_path, file_size, status, created_at, updated_at)
                   VALUES(?,?,?,?,?, 'PENDING', ?, ?)""",
                (project_number, file_name, folder_path, source_path,
                 file_size, _now(), _now()))
            self.conn.commit()
            return cur.lastrowid, True

    # ---------- records (from SLFE) ----------
    def upsert_record(self, project_number, file_name, revision_number,
                      revision_stage, meta_json):
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM records WHERE project_number=? AND file_name=?",
                (project_number, file_name)).fetchone()
            if row:
                self.conn.execute(
                    """UPDATE records SET revision_number=?, revision_stage=?,
                       meta_json=?, updated_at=? WHERE id=?""",
                    (revision_number, revision_stage, meta_json, _now(),
                     row["id"]))
            else:
                self.conn.execute(
                    """INSERT INTO records(project_number, file_name,
                       revision_number, revision_stage, meta_json,
                       created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (project_number, file_name, revision_number,
                     revision_stage, meta_json, _now(), _now()))
            self.conn.commit()

    # ---------- generic setters ----------
    def set_project(self, project_number, **fields):
        with self._lock:
            fields["updated_at"] = _now()
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE projects SET {cols} WHERE project_number=?",
                (*fields.values(), project_number))
            self.conn.commit()

    def set_document(self, doc_id, **fields):
        with self._lock:
            fields["updated_at"] = _now()
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE documents SET {cols} WHERE id=?",
                (*fields.values(), doc_id))
            self.conn.commit()

    def set_record(self, rec_id, **fields):
        with self._lock:
            fields["updated_at"] = _now()
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE records SET {cols} WHERE id=?",
                (*fields.values(), rec_id))
            self.conn.commit()

    # ---------- queries ----------
    def get_project(self, project_number):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM projects WHERE project_number=?",
                (project_number,)).fetchone()

    def all_projects(self):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM projects ORDER BY project_number").fetchall()

    def documents_for(self, project_number, statuses=None):
        with self._lock:
            if statuses:
                q = ("SELECT * FROM documents WHERE project_number=? AND "
                     f"status IN ({','.join('?'*len(statuses))}) ORDER BY id")
                return self.conn.execute(
                    q, (project_number, *statuses)).fetchall()
            return self.conn.execute(
                "SELECT * FROM documents WHERE project_number=? ORDER BY id",
                (project_number,)).fetchall()

    def records_for(self, project_number):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM records WHERE project_number=? ORDER BY id",
                (project_number,)).fetchall()

    def documents_by_name(self, project_number, file_name):
        with self._lock:
            return self.conn.execute(
                """SELECT * FROM documents
                   WHERE project_number=? AND file_name=? COLLATE NOCASE""",
                (project_number, file_name)).fetchall()

    def log(self, level, scope, message):
        with self._lock:
            self.conn.execute(
                "INSERT INTO run_log(ts, level, scope, message) "
                "VALUES(?,?,?,?)",
                (_now(), level, scope, message))
            self.conn.commit()

    def summary(self):
        with self._lock:
            proj = self.conn.execute(
                "SELECT status, COUNT(*) c FROM projects GROUP BY status"
            ).fetchall()
            doc = self.conn.execute(
                "SELECT status, COUNT(*) c FROM documents GROUP BY status"
            ).fetchall()
            match = self.conn.execute(
                "SELECT match_status, COUNT(*) c FROM documents "
                "GROUP BY match_status").fetchall()
            return ({r["status"]: r["c"] for r in proj},
                    {r["status"]: r["c"] for r in doc},
                    {r["match_status"]: r["c"] for r in match})

    def close(self):
        with self._lock:
            self.conn.close()
