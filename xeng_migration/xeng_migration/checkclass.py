#!/usr/bin/env python3
"""checkclass.py - diagnose why the Closed classification is not applied.

Usage:
    py checkclass.py --config config/env_Prod.yaml --project 500208
    py checkclass.py --config config/env_Prod.yaml --node 12345   (single node)

It checks, in order:
  1. Login + ticket
  2. That the configured classification_ids really are classification nodes
  3. Whether the tracker thinks the project is CLOSED (status column reading)
  4. Classifications currently on the Project workspace
  5. Every known POST payload variant, verified by reading the node back
  6. Whether apply_to_sub_items actually reached a child folder + document

Nothing is left half-applied: whatever variant works is reported so the
migration tool can be pinned to it.
"""

from __future__ import annotations

import os
import json
import sqlite3
import argparse

import yaml
import requests

requests.packages.urllib3.disable_warnings()  # noqa


# ------------------------------------------------------------------ helpers
def api_base(cfg):
    c = cfg.get("connection", {}) or {}
    host = c.get("hostname") or "localhost"
    base = c.get("base_path") or "/otcs/cs.exe"
    port = str(c.get("port") or "")
    proto = c.get("protocol") or ("https" if port == "443" else "http")
    netloc = host if port in ("", "80", "443") else f"{host}:{port}"
    return f"{proto}://{netloc}{base}/api"


def login(cfg, base):
    c = cfg["connection"]
    r = requests.post(f"{base}/v1/auth",
                      data={"username": c["username"], "password": c["password"]},
                      verify=bool(c.get("verify_ssl", False)), timeout=60)
    r.raise_for_status()
    return r.json()["ticket"]


def node_props(s, base, nid):
    r = s.get(f"{base}/v2/nodes/{nid}", timeout=60)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} {r.text[:160]}"
    try:
        return r.json()["results"]["data"]["properties"], None
    except Exception as e:
        return None, f"parse: {e}"


def _find_ids(obj, out):
    """Recursively pull every numeric 'id' out of a classifications payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "class_id") and isinstance(v, (int, str)):
                try:
                    out.add(int(v))
                except (TypeError, ValueError):
                    pass
            else:
                _find_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_ids(v, out)
    return out


def get_class(s, base, nid):
    """Return (set_of_ids, raw_text) for whichever endpoint answers."""
    for ver in ("v2", "v1"):
        r = s.get(f"{base}/{ver}/nodes/{nid}/classifications", timeout=60)
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                return set(), r.text[:300]
            return _find_ids(j.get("results", j), set()), json.dumps(j)[:400]
    return set(), f"no classifications endpoint answered 200 (last {r.status_code})"


_CHILD_VER = {"value": None}


def children(s, base, nid, verbose=False):
    """Child (id, name, type) tuples.  Tries v2 then v1 - this build's v2
    mappings registry is incomplete, so the fallback matters."""
    vers = ([_CHILD_VER["value"]] if _CHILD_VER["value"] else []) + ["v2", "v1"]
    for ver in vers:
        try:
            r = s.get(f"{base}/{ver}/nodes/{nid}/nodes",
                      params={"limit": 200, "fields": "properties"}, timeout=120)
        except Exception as e:
            if verbose:
                print(f"    children via {ver}: EXCEPTION {e}")
            continue
        if r.status_code != 200:
            if verbose:
                print(f"    children via {ver}: HTTP {r.status_code} "
                      f"{r.text[:120]}")
            continue
        out = []
        try:
            for b in r.json().get("results", []) or []:
                d = b.get("data", b) or {}
                pr = d.get("properties", d) or {}
                if pr.get("id"):
                    out.append((int(pr["id"]), pr.get("name"),
                                int(pr.get("type") or -1)))
        except Exception as e:
            if verbose:
                print(f"    children via {ver}: parse error {e}")
            continue
        if verbose:
            print(f"    children via {ver}: HTTP 200, {len(out)} child node(s)")
            for k in out[:8]:
                print(f"        id={k[0]:<10} type={k[2]:<5} {str(k[1])[:44]}")
        _CHILD_VER["value"] = ver
        return out
    return []


VARIANTS = [
    ("v2 body-wrapped",
     lambda ids, sub: ("v2", {"body": json.dumps(
         {"class_id": [int(i) for i in ids], "apply_to_sub_items": bool(sub)})})),
    ("v2 flat form",
     lambda ids, sub: ("v2", {"class_id": [int(i) for i in ids],
                              "apply_to_sub_items": str(bool(sub)).lower()})),
    ("v2 flat form (int flag)",
     lambda ids, sub: ("v2", {"class_id": [int(i) for i in ids],
                              "apply_to_sub_items": 1 if sub else 0})),
    ("v1 flat form",
     lambda ids, sub: ("v1", {"class_id": [int(i) for i in ids],
                              "apply_to_sub_items": str(bool(sub)).lower()})),
    ("v1 body-wrapped",
     lambda ids, sub: ("v1", {"body": json.dumps(
         {"class_id": [int(i) for i in ids], "apply_to_sub_items": bool(sub)})})),
]


def try_variants(s, base, nid, class_ids, sub_items, merge_with=None):
    """POST each payload shape until a read-back proves it stuck."""
    wanted = set(int(c) for c in class_ids)
    for name, build in VARIANTS:
        ids = sorted(wanted | set(merge_with or []))
        ver, data = build(ids, sub_items)
        print(f"      sending class_id={ids} to /{ver}/nodes/{nid}"
              f"/classifications")
        url = f"{base}/{ver}/nodes/{nid}/classifications"
        try:
            r = s.post(url, data=data, timeout=600)
        except Exception as e:
            print(f"    {name:26} EXCEPTION {e}")
            continue
        after, _ = get_class(s, base, nid)
        http_ok = r.status_code in (200, 201)
        present = wanted.issubset(after)
        stuck = http_ok and present
        shown = str(sorted(after)) if after else "[]"
        if stuck:
            verdict = "APPLIED"
        elif present and not http_ok:
            verdict = "rejected (ids were already there from an earlier run)"
        else:
            verdict = "no effect"
        note = "" if http_ok else "  body=" + r.text[:140]
        print(f"    {name:26} HTTP {r.status_code:<4} "
              f"read-back={shown:<24} {verdict}{note}")
        if stuck:
            return name, ver, sub_items
    return None, None, None


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/env_Prod.yaml")
    ap.add_argument("--project")
    ap.add_argument("--node", type=int)
    ap.add_argument("--check", action="store_true",
                    help="read-only: report state, POST nothing")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    base = api_base(cfg)
    blk = cfg.get("closed_projects", {}) or {}
    class_ids = [int(c) for c in (blk.get("classification_ids") or [])]

    print("=" * 68)
    print(f"API base        : {base}")
    print(f"classification  : {class_ids}")
    print(f"enabled         : {blk.get('enabled')}")
    print("=" * 68)

    ticket = login(cfg, base)
    s = requests.Session()
    s.headers.update({"OTCSTicket": ticket})
    s.verify = bool(cfg["connection"].get("verify_ssl", False))
    print(f"[1] Login OK as {cfg['connection']['username']}\n")

    # ---- 2. are the configured ids really classifications?
    print("[2] Classification node check")
    for cid in class_ids:
        p, err = node_props(s, base, cid)
        if not p:
            print(f"    {cid}: CANNOT READ - {err}")
            print(f"        -> wrong id, or MigrationTool has no rights on it")
            continue
        print(f"    {cid}: name={p.get('name')!r} type={p.get('type')} "
              f"({p.get('type_name')}) parent={p.get('parent_id')}")
        if str(p.get("type")) not in ("199", "196", "198"):
            print("        -> WARNING: not a Classification node type. A "
                  "classification is normally type 199 inside a "
                  "Classification Tree (198).")
    print()

    # ---- 3. status detection from the tracker
    target_nodes = []
    if args.project:
        db = cfg.get("runtime", {}).get("tracker_db", "migration.db")
        print(f"[3] Status detection  (tracker {db})")
        if not os.path.isfile(db):
            print(f"    tracker not found: {db}")
        else:
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM projects WHERE project_number=?",
                              (str(args.project),)).fetchone()
            if not row:
                print(f"    project {args.project} not in tracker")
            else:
                keys = row.keys()
                print(f"    status={row['status']} "
                      f"project_id={row['project_id']} "
                      f"master_id={row['master_id']}")
                extra = {}
                if "extra_json" in keys:
                    try:
                        extra = json.loads(row["extra_json"] or "{}")
                    except Exception:
                        extra = {}
                col = blk.get("status_column") or "Project Status"
                closed_vals = {str(v).strip().lower()
                               for v in (blk.get("closed_values") or ["closed"])}
                print(f"    extra_json keys: {list(extra.keys())}")
                cand = {k: v for k, v in extra.items()
                        if "status" in str(k).lower()}
                print(f"    status-ish columns: {cand or 'NONE FOUND'}")
                val = extra.get(col)
                if val is None and cand:
                    val = list(cand.values())[0]
                verdict = str(val or "").strip().lower() in closed_vals
                print(f"    configured status_column={col!r} -> value={val!r}")
                print(f"    closed_values={sorted(closed_vals)}")
                print(f"    IS_CLOSED = {verdict}")
                if not verdict:
                    print("        -> this alone stops the tool from ever "
                           "classifying this project")
                if row["project_id"]:
                    target_nodes.append(("Project workspace",
                                         int(row["project_id"])))
                if blk.get("apply_to_master") and row["master_id"]:
                    target_nodes.append(("Master workspace",
                                         int(row["master_id"])))
                # a child folder and a document, for the sub-items check
                d = con.execute(
                    "SELECT target_folder_id, document_id, file_name FROM "
                    "documents WHERE project_number=? AND document_id IS NOT "
                    "NULL AND target_folder_id IS NOT NULL LIMIT 1",
                    (str(args.project),)).fetchone()
                sub_check = (int(d["target_folder_id"]), int(d["document_id"]),
                             d["file_name"]) if d else None
            con.close()
        print()
    else:
        sub_check = None

    if args.node:
        target_nodes = [("node", args.node)]
        sub_check = None

    if not target_nodes:
        print("Nothing to test against - pass --project or --node.")
        return

    # ---- 4/5. current state, then payload variants
    for label, nid in target_nodes:
        print(f"[4] {label} {nid}: current classifications")
        before, raw = get_class(s, base, nid)
        print(f"    ids={sorted(before) if before else '[]'}")
        print(f"    raw={raw[:200]}")
        if args.check:
            print("    (--check: skipping POST)\n")
            continue
        print(f"[5] {label} {nid}: trying payload variants "
              f"(apply_to_sub_items={blk.get('apply_to_sub_items', True)})")
        name, ver, sub = try_variants(
            s, base, nid, class_ids,
            sub_items=bool(blk.get("apply_to_sub_items", True)),
            merge_with=before if blk.get("merge_existing", True) else None)
        if name:
            print(f"    => WORKING VARIANT: {name} ({ver})")
        else:
            print("    => NO variant applied the classification.")
            print("       Most likely: MigrationTool lacks 'Edit "
                  "classifications' rights, or the classification is not "
                  "permitted on this item type.")
        print()

    # ---- 6. did sub-items really get it?
    if not sub_check and target_nodes:
        # No uploaded document in the tracker yet - probe the live tree for a
        # child folder and, if there is one, a document inside it.
        root = target_nodes[0][1]
        kids = children(s, base, root, verbose=True)
        folder = next((k for k in kids if k[2] in (0, 848, 899)), None)
        doc = next((k for k in kids if k[2] == 144), None)
        if folder and not doc:
            for k in children(s, base, folder[0]):
                if k[2] == 144:
                    doc = k
                    break
        if not folder:
            print("[6] Sub-items check: no child folder found under "
                  f"{root} - cannot verify cascade")
        if folder:
            sub_check = (folder[0], doc[0] if doc else None,
                         doc[1] if doc else "(no document found)")

    if sub_check:
        fid, did, fname = sub_check
        print("[6] Sub-items check (did apply_to_sub_items cascade?)")
        pairs = [("child folder", fid)]
        if did:
            pairs.append((f"document {str(fname)[:24]}", did))
        for lbl, nid in pairs:
            ids, _ = get_class(s, base, nid)
            hit = set(class_ids).issubset(ids)
            shown = str(sorted(ids)) if ids else "[]"
            print(f"    {lbl:34} {nid:<10} ids={shown:<24} "
                  f"{'OK' if hit else 'NOT CLASSIFIED'}")
        probe = did or fid
        if not set(class_ids).issubset(get_class(s, base, probe)[0]):
            print("        -> apply_to_sub_items did NOT cascade. Set "
                  "apply_to_sub_items: false so the tool walks the tree "
                  "and classifies each node itself.")
        else:
            print("        -> cascade works; one recursive call is enough.")


if __name__ == "__main__":
    main()
