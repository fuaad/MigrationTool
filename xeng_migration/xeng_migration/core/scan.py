r"""scan.py - Walk the network drive and build the document inventory.

The drive is the source of truth for WHAT exists and WHERE. For every
project in the tracker, scan <source.root>\<ProjectNumber>\** and register
each file as a document (structure-only until matched with a record).

Folder Path is derived from the file's location relative to the project
folder (e.g. '4000 ENG\4500 Mechanical\Drawings').
"""

from __future__ import annotations

import os


def scan(tracker, cfg, project_number=None):
    root = cfg["source"]["root"]
    by_number = cfg["source"].get("project_folder_is_number", True)

    if not os.path.isdir(root):
        raise FileNotFoundError(f"source.root not found: {root}")

    projects = ([tracker.get_project(project_number)] if project_number
                else tracker.all_projects())
    projects = [p for p in projects if p]

    total_new = total_seen = 0
    missing_folders = []

    # optional excludes (temp files etc.)
    exclude_ext = {e.lower() for e in cfg["source"].get(
        "exclude_extensions", [".tmp", ".bak", ".db", ".lnk"])}
    exclude_prefix = tuple(cfg["source"].get("exclude_prefixes", ["~$", "."]))

    for p in projects:
        pnum = p["project_number"]
        base = os.path.join(root, pnum) if by_number else root
        if not os.path.isdir(base):
            missing_folders.append((pnum, base))
            tracker.log("WARN", pnum, f"Project folder not found: {base}")
            continue

        for dirpath, _dirs, files in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            folder_path = "" if rel == "." else rel.replace("/", "\\")
            for fn in files:
                if fn.startswith(exclude_prefix):
                    continue
                if os.path.splitext(fn)[1].lower() in exclude_ext:
                    continue
                source_path = os.path.join(dirpath, fn)
                try:
                    fsize = os.path.getsize(source_path)
                except OSError:
                    fsize = None
                _id, is_new = tracker.upsert_scanned_document(
                    pnum, fn, folder_path, source_path, file_size=fsize)
                total_seen += 1
                if is_new:
                    total_new += 1

        tracker.log("INFO", pnum, "Scan complete.")

    return {"seen": total_seen, "new": total_new,
            "missing_project_folders": missing_folders}
