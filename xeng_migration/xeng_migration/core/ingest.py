"""ingest.py - Read the input Excels into the tracking database.

Scan-driven model:
  - Projects Excel (required): one row per project.
  - Records Excel (optional): SLFE's metadata records, one row per document,
    matched later by FILE NAME - no Folder Path column required. If a
    'Folder Path' column exists it is stored in meta for reference but is
    NOT used to locate files (the scan does that).
"""

from __future__ import annotations

import json
import datetime
import openpyxl


def _clean(v):
    if v is None:
        return None
    # Excel date cells arrive as datetime/date objects; Content Server
    # requires ISO format (YYYY-MM-DDTHH:MM:SS) - the "YYYY-MM-DD HH:MM:SS"
    # string form is rejected with "Unparseable date".
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%dT00:00:00")
    s = str(v).strip()
    return s if s != "" else None


def _rows(path, header_row):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    headers = [(_clean(h) or f"col{i}") for i, h in enumerate(all_rows[header_row - 1])]
    for r in all_rows[header_row:]:
        if all(c is None for c in r):
            continue
        yield dict(zip(headers, r))


def ingest_projects(tracker, path, header_row):
    count = 0
    for row in _rows(path, header_row):
        pnum = _clean(row.get("Project Number"))
        if not pnum:
            continue
        extra = {
            "project_description": _clean(row.get("Project Description")),
            "project_status": _clean(row.get("Project Status")),
            "client_number": _clean(row.get("Client Number")),
            "client_name": _clean(row.get("Client Name")),
            "start_date": _clean(row.get("Start Date")),
            "finish_date": _clean(row.get("Finish Date")),
        }
        tracker.upsert_project(
            project_number=pnum,
            project_title=_clean(row.get("Project Title")),
            program=_clean(row.get("Program")),
            business_line=_clean(row.get("Business Line")),
            extra_json=json.dumps(extra),
        )
        count += 1
    tracker.log("INFO", "GLOBAL", f"Ingested {count} project(s).")
    return count


_META_COLS = [
    "Deliverable Control Log (DCL)", "PREFIX LETTER", "Identifying Number",
    "Sheet Number", "Sheet Size", "Document Title", "Plant Number",
    "Document Category", "Index", "Document Type",
    "Document Start Date", "Document Finish Date",
    "XReferences", "XReferences Path", "Folder Path",
]


def ingest_records(tracker, path, header_row):
    """SLFE metadata records - matched to files later by file name."""
    count = 0
    for row in _rows(path, header_row):
        pnum = _clean(row.get("Project Number"))
        fname = _clean(row.get("File Name"))
        if not pnum or not fname:
            continue
        meta = {k: _clean(row.get(k)) for k in _META_COLS}
        tracker.upsert_record(
            project_number=pnum,
            file_name=fname,
            revision_number=_clean(row.get("Revision Number")),
            revision_stage=_clean(row.get("Revision Stage")),
            meta_json=json.dumps(meta),
        )
        count += 1
    tracker.log("INFO", "GLOBAL", f"Ingested {count} record(s).")
    return count
