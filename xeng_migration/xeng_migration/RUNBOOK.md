# xENG Migration Tool - Runbook

A per-project, resumable, auditable migration tool for loading engineering
projects and documents into OpenText Content Management (xECM for Engineering).

It creates the **Master** and **Project** business workspaces (with metadata and
classification), forms the **Master↔Project relationship**, uploads **documents**
with category metadata into the correct workspace folders, and applies
**controlled revisions** (revision number + stage) via the Engineering REST API.
Every step is tracked in a local SQLite database so runs are resumable and
fully auditable.

---

## 1. Prerequisites

- Runs on the OpenText server (or any host with network access to the CS REST
  API **and** read access to the source files on the network drive).
- Python 3.11+ (3.13 recommended).
- Install dependencies:

  ```bash
  pip install -r requirements.txt
  ```

  This installs `pyxecm` (official OpenText Python library), `requests`,
  `openpyxl`, and `PyYAML`.

---

## 2. One-time configuration

1. Copy the example config and edit it:

   ```bash
   cp config/env.example.yaml config/env.yaml
   ```

2. Fill in `config/env.yaml`:

   - **connection**: hostname, base_path (`/otcs/cs.exe`), username. Leave
     `password` blank to be prompted securely at runtime.
   - **master / project**: the verified template/parent/category IDs and the
     attribute-key map (already pre-filled for the SLFE dev environment).
   - **project.classification_ids**: the numeric node ID of the
     `Project Workspace` classification (see Discovery below).
   - **document.category_id** and **document.attributes**: the XENG_Document
     category DefID and its attribute keys (see Discovery below).
   - **source.root**: UNC root of the project files on the network drive.
   - **revisions.control_action**: usually `changerevisionstatus`.

### Discovery (one-time, per environment)

These IDs differ per environment. Fetch them once:

- **Document category DefID + attribute IDs**: run the category-attributes
  admin report for the `XENG_Document` category (same report used for the
  Master/Project categories). Put the DefID in `document.category_id` and each
  attribute key as `"{DefID}_{AttrID}"` under `document.attributes`.
- **Project Workspace classification node ID**: open that classification in the
  UI and read its node ID (or query an existing controlled Project). Put it in
  `project.classification_ids`.
- **Revision status IDs**: usually resolved automatically at runtime from the
  register's `Revision Stage` names. To hardcode, add entries under
  `revisions.stage_status_map` (e.g. `"Issued for 30% Submittal": 123`).

---

## 3. Input files

Place the two Excel files in `input/` (paths configurable):

- `List_of_Running_Projects.xlsx` - one row per project (header row 1).
- `Document_Register.xlsx` (OPTIONAL) - SLFE metadata records, one row per document (header row 2). No Folder Path needed - files are matched by name.

The network drive is scanned to build the inventory: ALL files migrate.
The `Folder Path` column is the full relative path inside the Project
(e.g. `4000 ENG\4500 Mechanical\Drawings`); the tool resolves existing template
folders and **creates** any deeper folders automatically.

Source file for each row is resolved as:
`source.root \ <Project Number> \ <Folder Path> \ <File Name>`

---

## 4. Running (scan-driven workflow)

```bash
# 1) Load the project list (+ optional SLFE records) into the tracker
python migrate.py ingest --config config/env.yaml

# 2) Scan the network drive - builds the document inventory per project
python migrate.py scan --config config/env.yaml

# 3) Match SLFE records to files by name (writes match_report.xlsx)
python migrate.py match --config config/env.yaml

#    -> send the "Needs Review" sheet to SLFE if any ambiguities

# 4) Preflight validation of scanned files (permissions, path length)
python migrate.py validate --config config/env.yaml

# 5) Migrate ONE project end-to-end (recommended first)
python migrate.py migrate --project 560090 --config config/env.yaml

# 6) Status / audit report at any time
python migrate.py status --config config/env.yaml
python migrate.py report --out migration_report.xlsx --config config/env.yaml

# 7) Once validated, migrate everything (resumable)
python migrate.py migrate --all --config config/env.yaml
```

Key behaviors:
- The DRIVE is the source of structure: every scanned file migrates to its
  correct project/folder. No paths are needed from SLFE.
- Records are OPTIONAL and matched by FILE NAME. Matched documents get the
  record's metadata and revision. Ambiguous matches are never guessed -
  they go to the "Needs Review" sheet and migrate structure-only meanwhile.
- Documents without records migrate structure-only; metadata can be added
  later in OpenText.

## 5. Status model (audit trail)

- **Project**: `PENDING → MASTER_CREATED → PROJECT_CREATED → RELATED → COMPLETE`
- **Document**: `PENDING → LOADED → REVISION_SET → COMPLETE`
- Any step may become `FAILED` with an error message and timestamp.

The SQLite file (`migration.db`) is the source of truth and the audit record.
The Excel report is a point-in-time export of it.

---

## 6. Recommended first-run validation

1. Put **one** project and its documents in the input files.
2. `ingest`, then `migrate --project <num>`.
3. In the UI confirm: Master created, Project created with metadata and the
   Project Workspace classification, relationship present, documents in the
   correct folders with metadata, and revisions applied at the right stage.
4. `report` and review the audit output.
5. When satisfied, scale to `--all` and hand over to associates using this
   runbook.

---

## 7. Notes / limits

- Controlled revisions use the Engineering REST API (`/api/v2/xengcrt/*`).
  The exact revision `control_action` and the `Revision Stage → rev_status_id`
  mapping should be confirmed once against your revision scheme (the tool
  auto-resolves status IDs from the API; overrides live in config).
- Files present on disk but **not** in the register are not migrated; they can
  be identified by comparing the register to the folder contents.
- Set `connection.verify_ssl: false` only in dev with self-signed certificates.
