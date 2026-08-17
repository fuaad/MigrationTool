"""checkchain.py - walk a folder's ancestor chain and report category state.

Usage:
    py checkchain.py <node_id>
    py checkchain.py            (auto-picks a deep leaf folder from the tracker)

For each level from the node up to the project it prints:
    node id, name, whether the categories are present, and their values.
Use it to find the level where categories stop being applied.
"""

import sys
import sqlite3
import requests

BASE = "http://localhost/otcs/cs.exe/api"
USER = "Admin"
PWD = "OpenTextDev@2026"
CATS = (29603, 134376)


def ticket():
    r = requests.post(f"{BASE}/v1/auth", data={"username": USER, "password": PWD})
    r.raise_for_status()
    return r.json()["ticket"]


def node_info(h, nid):
    r = requests.get(f"{BASE}/v2/nodes/{nid}", headers=h, timeout=60)
    if r.status_code != 200:
        return None
    p = r.json()["results"]["data"]["properties"]
    return {"id": p.get("id"), "name": p.get("name"),
            "parent_id": p.get("parent_id"), "type": p.get("type")}


def node_categories(h, nid):
    r = requests.get(f"{BASE}/v2/nodes/{nid}/categories", headers=h, timeout=60)
    if r.status_code != 200:
        return None
    found = {}
    for block in r.json().get("results", []):
        cats = (block.get("data", {}) or {}).get("categories", {}) or {}
        for key in cats:
            cid = str(key).split("_")[0]
            found.setdefault(cid, 0)
            found[cid] += 1
    return found


def pick_leaf():
    """Deepest folder path that received documents, from the tracker."""
    con = sqlite3.connect("migration.db")
    row = con.execute(
        "SELECT target_folder_id, folder_path FROM documents "
        "WHERE target_folder_id IS NOT NULL AND status='COMPLETE' "
        "ORDER BY LENGTH(folder_path) DESC LIMIT 1").fetchone()
    con.close()
    return row


def main():
    h = {"OTCSTicket": ticket()}

    if len(sys.argv) > 1:
        start = int(sys.argv[1])
        print(f"Starting at node {start}\n")
    else:
        row = pick_leaf()
        if not row:
            print("No completed documents with a folder id in migration.db")
            return
        start = row[0]
        print(f"Auto-picked deepest folder: {start}")
        print(f"  path: {row[1]}\n")

    nid = start
    level = 0
    while nid and level < 25:
        info = node_info(h, nid)
        if not info:
            print(f"  (cannot read node {nid} - stopping)")
            break
        cats = node_categories(h, nid)
        if cats is None:
            state = "categories: <error reading>"
        elif not cats:
            state = "categories: NONE"
        else:
            present = ", ".join(f"{c}({n} attrs)" for c, n in sorted(cats.items()))
            missing = [str(c) for c in CATS if str(c) not in cats]
            state = f"categories: {present}"
            if missing:
                state += f"  MISSING: {','.join(missing)}"
        print(f"[{level}] id={info['id']:<8} type={info['type']:<4} "
              f"{str(info['name'])[:40]:<40} {state}")
        nid = info["parent_id"]
        level += 1


if __name__ == "__main__":
    main()
