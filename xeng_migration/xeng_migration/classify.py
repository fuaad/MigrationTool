#!/usr/bin/env python3
"""core/classify.py - extra classification for CLOSED projects.

When a project's status in the Projects Excel is "Closed", the Project
workspace and EVERY folder and document underneath it get an additional
classification (e.g. "Closed Project").

Config block (config/env.yaml / env_Prod.yaml):

    closed_projects:
      enabled: true
      status_column: "Project Status"        # column in List of Running Projects
      closed_values: ["Closed", "Close", "Completed"]
      classification_ids: [20918]            # node id(s) of the classification
      apply_to_master: false                 # also classify the Master workspace
      apply_to_sub_items: true               # try one server-side recursive call
      merge_existing: true                   # keep classifications already set
      skip_documents: false                  # true = folders only

Resumable: every node id that has been classified is written to the
`classified` table in the tracker db, so re-runs skip finished work.
Called automatically at the end of a successful migrate, and available
standalone as:  py migrate.py classify --project 500208
"""

from __future__ import annotations

import json
import time
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import requests

# Node types we classify.  0=folder, 848=business workspace, 144=document,
# 736=compound doc, 899=virtual folder.  Shortcuts/aliases are skipped.
FOLDER_TYPES = {0, 848, 899, 202}
DOC_TYPES = {144, 736, 749}


# ---------------------------------------------------------------- config
def _blk(cfg):
    return cfg.get("closed_projects", {}) or {}


def enabled(cfg):
    blk = _blk(cfg)
    return bool(blk.get("enabled")) and bool(blk.get("classification_ids"))


def api_base(cfg):
    c = cfg.get("connection", {}) or {}
    host = c.get("hostname") or c.get("host") or "localhost"
    base = c.get("base_path") or "/otcs/cs.exe"
    port = c.get("port")
    proto = c.get("protocol") or ("https" if str(port) == "443" else "http")
    netloc = host if not port or str(port) in ("80", "443") else f"{host}:{port}"
    return f"{proto}://{netloc}{base}/api"


_OWN_TICKET = {"value": None}


_AUTH_WARNED = {"value": False}


def coerce_ticket(val):
    """Accept only a real ticket string.

    pyxecm exposes `otcs_ticket` as a method, so a bare getattr hands back a
    bound method; using it as a header raises "Header part ... must be of type
    str or bytes" on every single call.
    """
    if callable(val):
        try:
            val = val()
        except Exception:
            return None
    if isinstance(val, bytes):
        val = val.decode("utf-8", "ignore")
    return val if isinstance(val, str) and val.strip() else None


def _auth_ticket(cfg, quiet=False):
    """Mint a ticket from /v1/auth as a fallback when none was handed in."""
    if _OWN_TICKET["value"]:
        return _OWN_TICKET["value"]
    c = cfg.get("connection", {}) or {}
    user = c.get("username")
    pwd = c.get("password")
    if not user or not pwd:
        if not quiet and not _AUTH_WARNED["value"]:
            _AUTH_WARNED["value"] = True
            print("  [warn] no username/password in config['connection'] - "
                  "cannot self-authenticate for classification")
        return None
    try:
        r = requests.post(f"{api_base(cfg)}/v1/auth",
                          data={"username": user, "password": pwd},
                          verify=bool(c.get("verify_ssl", False)), timeout=60)
        if r.status_code == 200:
            _OWN_TICKET["value"] = coerce_ticket(r.json().get("ticket"))
        elif not quiet and not _AUTH_WARNED["value"]:
            _AUTH_WARNED["value"] = True
            print(f"  [warn] /v1/auth returned HTTP {r.status_code}: "
                  f"{r.text[:160]}")
    except Exception as e:
        if not quiet and not _AUTH_WARNED["value"]:
            _AUTH_WARNED["value"] = True
            print(f"  [warn] /v1/auth failed: {e}")
        return None
    return _OWN_TICKET["value"]


def _session(cfg, ticket=None):
    # A ticket handed in by migrate.py wins, but only if it is a real string.
    tk = coerce_ticket(ticket) or _auth_ticket(cfg)
    s = requests.Session()
    if tk:
        s.headers.update({"OTCSTicket": tk})
    s.verify = bool((cfg.get("connection", {}) or {}).get("verify_ssl", False))
    return s


def is_closed(cfg, prow):
    """True when the project row's status column says the project is closed."""
    blk = _blk(cfg)
    col = blk.get("status_column") or "Project Status"
    wanted = {str(v).strip().lower() for v in
              (blk.get("closed_values") or ["closed"])}
    try:
        extra = json.loads(prow["extra_json"] or "{}")
    except Exception:
        extra = {}
    val = extra.get(col)
    if val is None:                      # tolerate header drift
        for k, v in extra.items():
            if "status" in str(k).lower():
                val = v
                break
    return str(val or "").strip().lower() in wanted


# ---------------------------------------------------------------- state
def _ensure_table(tracker):
    tracker.conn.execute(
        "CREATE TABLE IF NOT EXISTS classified ("
        " node_id INTEGER PRIMARY KEY,"
        " project_number TEXT,"
        " class_ids TEXT,"
        " ts TEXT)")
    tracker.conn.commit()


def _already(tracker, pnum):
    rows = tracker.conn.execute(
        "SELECT node_id FROM classified WHERE project_number=?",
        (str(pnum),)).fetchall()
    return {r[0] for r in rows}


def _record(tracker, pnum, node_ids, class_ids, lock):
    with lock:
        tracker.conn.executemany(
            "INSERT OR REPLACE INTO classified"
            " (node_id, project_number, class_ids, ts) VALUES (?,?,?,?)",
            [(int(n), str(pnum), json.dumps(class_ids),
              time.strftime("%Y-%m-%d %H:%M:%S")) for n in node_ids])
        tracker.conn.commit()


# ---------------------------------------------------------------- REST
def _find_ids(obj, out):
    """Recursively pull every numeric id out of a classifications payload."""
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


def _parse_classifications(j, own_only):
    """Ids from a classifications payload.

    own_only=True keeps ONLY classifications assigned directly to the node.
    This matters enormously: the API also reports classifications INHERITED
    from ancestors, and POSTing those back would turn an inherited entry into
    an explicit assignment on every child - pushing the Project workspace's
    classifications down the whole tree.
    """
    entries = j.get("data")
    if not isinstance(entries, list):
        res = j.get("results", j)
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            entries = res["data"]
    if not isinstance(entries, list):
        # unknown shape - fall back to a broad scan (never own_only-safe)
        return None if own_only else _find_ids(j.get("results", j), set())
    out = set()
    for e in entries:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        if own_only:
            mtype = str(e.get("management_type") or "manual").lower()
            if mtype not in ("manual", "none", ""):
                continue          # inherited / automatic / system-managed
            if e.get("parent_managed"):
                continue          # managed by an ancestor
        try:
            out.add(int(e["id"]))
        except (TypeError, ValueError):
            pass
    return out


def _get_classifications(s, base, nid, own_only=False):
    """Ids classified on a node, or None when NO endpoint answered.

    None must never be read as "the node has none": a POST replaces the whole
    list, so an unreadable node has to be skipped rather than overwritten.
    """
    for ver in ("v1", "v2"):
        try:
            r = s.get(f"{base}/{ver}/nodes/{nid}/classifications", timeout=60)
        except Exception:
            continue
        if r.status_code == 200:
            try:
                return _parse_classifications(r.json(), own_only)
            except Exception:
                return None
    return None


# Payload shapes accepted by different CS builds.  The first one that is
# proven to work (by reading the node back) is cached and reused.
# CONFIRMED on SLFE prod (CS 25.4): only v1 with a JSON "body" field works.
# /v2/.../classifications is not in the mappings registry (400) and v1 with
# flat form fields returns 500.  v1-body is therefore tried first; the others
# stay as fallbacks in case another environment differs.
_VARIANTS = [
    ("v1-body", "v1", lambda ids, sub: {"body": json.dumps(
        {"class_id": ids, "apply_to_sub_items": bool(sub)})}),
    ("v2-body", "v2", lambda ids, sub: {"body": json.dumps(
        {"class_id": ids, "apply_to_sub_items": bool(sub)})}),
    ("v2-flat", "v2", lambda ids, sub: {
        "class_id": ids, "apply_to_sub_items": str(bool(sub)).lower()}),
    ("v1-flat", "v1", lambda ids, sub: {
        "class_id": ids, "apply_to_sub_items": str(bool(sub)).lower()}),
]

_GOOD_VARIANT = {"value": _VARIANTS[0]}  # pinned; re-pins if it ever fails


def _post_variant(s, base, nid, ids, sub_items, variant):
    name, ver, build = variant
    try:
        r = s.post(f"{base}/{ver}/nodes/{nid}/classifications",
                   data=build(ids, sub_items), timeout=600)
    except Exception as e:
        return False, f"{name}: {e}"
    return (r.status_code in (200, 201),
            f"{name}: HTTP {r.status_code}"
            + ("" if r.status_code in (200, 201) else f" {r.text[:160]}"))


def _assign(s, base, nid, class_ids, sub_items=False, verify=True):
    """Apply class_ids to a node.  Verified by read-back, never assumed."""
    ids = [int(c) for c in class_ids]
    wanted = set(ids)
    tried = []

    pinned = _GOOD_VARIANT["value"]
    order = ([pinned] if pinned else []) + [v for v in _VARIANTS if v != pinned]

    for variant in order:
        ok, msg = _post_variant(s, base, nid, ids, sub_items, variant)
        tried.append(msg)
        if not ok:
            continue
        if not verify:
            _GOOD_VARIANT["value"] = variant
            return True, msg
        back = _get_classifications(s, base, nid)
        if back is None:
            # POST succeeded but the node is not readable - accept it rather
            # than retrying variants that would each re-POST.
            _GOOD_VARIANT["value"] = variant
            return True, msg + " (unverified: read-back unavailable)"
        if wanted.issubset(back):
            _GOOD_VARIANT["value"] = variant
            return True, msg + " (verified)"
        tried.append(f"{variant[0]}: accepted but not applied")
        if pinned and variant == pinned:
            _GOOD_VARIANT["value"] = None      # pin went stale, keep trying
    return False, " | ".join(tried[-4:])


_CHILD_VER = {"value": None}     # v2 mappings are incomplete on some builds
_CHILD_PARAMS = {"value": None}
_LAST_CHILD_ERR = {"value": None}


def _children(s, base, nid, page_size=200):
    """Yield child property dicts of a container, following paging.

    Tries /v2/nodes/{id}/nodes then /v1/nodes/{id}/nodes; whichever answers
    is remembered for the rest of the run.
    """
    page = 1
    while True:
        j = None
        last = None
        vers = ([_CHILD_VER["value"]] if _CHILD_VER["value"] else []) + ["v2", "v1"]
        param_sets = [
            {"limit": page_size, "page": page, "fields": "properties"},
            {"limit": page_size, "page": page},
            {"page": page},
        ]
        for ver in vers:
            for prm in param_sets:
                try:
                    r = s.get(f"{base}/{ver}/nodes/{nid}/nodes",
                              params=prm, timeout=120)
                except Exception as e:
                    last = f"{ver} {prm}: {e}"
                    continue
                if r.status_code != 200:
                    last = f"{ver} {prm}: HTTP {r.status_code} {r.text[:120]}"
                    continue
                try:
                    j = r.json()
                except Exception as e:
                    last = f"{ver} {prm}: parse {e}"
                    continue
                # v1 answers 200 with an empty result set on this build,
                # so only pin a version that actually returned children.
                try:
                    n_res = len(j.get("results", []) or [])
                except Exception:
                    n_res = 0
                if n_res:
                    _CHILD_VER["value"] = ver
                    _CHILD_PARAMS["value"] = prm
                elif ver != vers[-1]:
                    j = None          # try the next version before believing it
                    continue
                break
            if j is not None:
                break
        if j is None:
            if page == 1 and last:
                _LAST_CHILD_ERR["value"] = last
            return
        results = j.get("results", []) or []
        if not results:
            return
        for block in results:
            d = block.get("data", block) or {}
            props = d.get("properties", d) or {}
            if props.get("id"):
                yield props
        paging = ((j.get("collection", {}) or {}).get("paging", {}) or {})
        total_pages = paging.get("page_total") or paging.get("total_pages")
        if total_pages and page >= int(total_pages):
            return
        if len(results) < page_size:
            return
        page += 1


# ---------------------------------------------------------------- walk
def _node_type(s, base, nid):
    """Type of a single node - used only when a listing omits it."""
    for ver in ("v2", "v1"):
        try:
            r = s.get(f"{base}/{ver}/nodes/{nid}", timeout=60)
        except Exception:
            continue
        if r.status_code == 200:
            try:
                pr = r.json()["results"]["data"]["properties"]
                return int(pr.get("type") or -1), bool(pr.get("container", False))
            except Exception:
                return -1, False
    return -1, False


def _collect(s, base, root_id, skip_documents=False, progress=None):
    """Breadth-first collect of every folder and document under root_id.

    Child listings on this build do NOT return 'type', so anything whose type
    is unknown is treated as a container: it gets classified AND descended
    into.  Listing the children of a document simply comes back empty, so the
    walk still terminates - it just costs one extra GET per document.  When
    skip_documents is set the type is resolved explicitly, because then the
    folder/document distinction actually matters.
    """
    folders, docs = [], []
    q = [int(root_id)]
    seen = {int(root_id)}
    while q:
        nid = q.pop(0)
        for p in _children(s, base, nid):
            cid = int(p["id"])
            if cid in seen:
                continue
            seen.add(cid)
            t = p.get("type")
            if t is None:
                t = p.get("subtype", p.get("node_type"))
            ntype = int(t) if t is not None else -1
            has_flag = "container" in p
            is_container = bool(p.get("container")) if has_flag \
                else ntype in FOLDER_TYPES
            if ntype == -1 and not has_flag:
                if skip_documents:
                    ntype, is_container = _node_type(s, base, cid)
                else:
                    is_container = True      # classify it and look inside
            if is_container:
                folders.append(cid)
                q.append(cid)
            elif not skip_documents:
                docs.append(cid)
            if progress and (len(folders) + len(docs)) % 2000 == 0:
                progress(len(folders), len(docs))
    return folders, docs


# ---------------------------------------------------------------- main
def classify_project(cfg, tracker, ticket, pnum, dry_run=False,
                     workers=None, print_fn=print, folders_only=None):
    """Apply the closed-project classification to a whole project subtree.

    folders_only=True classifies the workspace and every folder (template
    folders included) but leaves documents alone - useful straight after the
    workspaces are built, before uploads have run.
    """
    blk = _blk(cfg)
    class_ids = [int(c) for c in (blk.get("classification_ids") or [])]
    if not class_ids:
        print_fn("  [skip] closed_projects.classification_ids is empty")
        return {"nodes": 0, "ok": 0, "fail": 0}

    prow = tracker.get_project(pnum)
    if not prow or not prow["project_id"]:
        print_fn(f"  [skip] {pnum}: no project_id (not migrated yet)")
        return {"nodes": 0, "ok": 0, "fail": 0}

    ticket = coerce_ticket(ticket) or _auth_ticket(cfg)
    if not ticket:
        print_fn("  [FAIL] no usable OTCSTicket - cannot classify. Add "
                 "connection.username/password to the config so this step "
                 "can authenticate for itself.")
        return {"nodes": 0, "ok": 0, "fail": 0}
    _ensure_table(tracker)
    base = api_base(cfg)
    s = _session(cfg, ticket)
    project_id = int(prow["project_id"])
    workers = int(workers or cfg.get("runtime", {}).get("workers", 8))
    # A classifications POST REPLACES the list, so existing classifications
    # (Project Workspace, Bulk Upload, ...) must always be sent back with it.
    if folders_only is None:
        folders_only = bool(blk.get("skip_documents"))
    merge = True
    if not blk.get("merge_existing", True):
        print_fn("  [note] merge_existing:false ignored - a POST replaces the "
                 "whole classification list, so existing ids are preserved")
    t0 = time.time()

    targets = [project_id]
    if blk.get("apply_to_master") and prow["master_id"]:
        targets.append(int(prow["master_id"]))

    # ---- fast path: let Content Server cascade to sub-items itself.
    if blk.get("apply_to_sub_items", True) and not folders_only:
        ok_all = True
        for tid in targets:
            if dry_run:
                print_fn(f"  [dry-run] would classify {tid} "
                         f"+sub-items with {class_ids}")
                continue
            wanted = class_ids
            if merge:
                back = _get_classifications(s, base, tid, own_only=True)
                if back is None:
                    print_fn(f"  [skip] node {tid}: cannot read existing "
                             f"classifications - refusing to POST, it would "
                             f"replace them")
                    ok_all = False
                    continue
                wanted = sorted(back | set(class_ids))
            ok, msg = _assign(s, base, tid, wanted, sub_items=True)
            print_fn(f"  {'[OK]' if ok else '[FAIL]'} recursive classify "
                     f"node {tid}: {msg}")
            ok_all = ok_all and ok
        if ok_all and not dry_run:
            # Proof, not trust.  Probe a live child node - a folder will do,
            # and unlike an uploaded document one always exists (the template
            # folders are there from the moment the workspace is created).
            probes = []
            row = tracker.conn.execute(
                "SELECT document_id FROM documents WHERE project_number=? "
                "AND document_id IS NOT NULL LIMIT 1", (str(pnum),)).fetchone()
            if row and row[0]:
                probes.append(("document", int(row[0])))
            for ch in _children(s, base, project_id):
                if ch.get("id"):
                    probes.append(("child node", int(ch["id"])))
                    break
            if not probes:
                print_fn("  [warn] cannot verify the cascade - no child node "
                         "found under the project; walking the tree instead")
                ok_all = False
            for lbl, probe_id in probes:
                got = _get_classifications(s, base, probe_id)
                if got is None:
                    print_fn(f"  [warn] {lbl} {probe_id} unreadable - cannot "
                             f"confirm the cascade; walking the tree instead")
                    ok_all = False
                elif not set(class_ids).issubset(got):
                    print_fn(f"  [warn] {lbl} {probe_id} did NOT inherit the "
                             f"classification (has {sorted(got)}) - "
                             f"apply_to_sub_items does not propagate on this "
                             f"build; walking the tree instead")
                    ok_all = False
        if ok_all:
            if not dry_run:
                _record(tracker, pnum, targets, class_ids, threading.Lock())
                tracker.log("INFO", pnum,
                            f"Closed-project classification {class_ids} applied "
                            f"recursively to {targets} (cascade verified on a "
                            f"child node)")
            print_fn(f"  Classification done in "
                     f"{int(time.time() - t0)}s (server-side recursion)")
            return {"nodes": len(targets), "ok": len(targets), "fail": 0}
        print_fn("  Recursive apply not accepted - falling back to per-node walk")

    # ---- fallback: walk the tree and classify each node.
    print_fn(f"  Walking project {project_id} "
             f"({'folders only' if folders_only else 'folders + documents'}) ...")

    def _walk_progress(nf, nd):
        print_fn(f"    [walk] folders={nf} documents={nd}")

    folders, docs = _collect(s, base, project_id,
                             skip_documents=folders_only,
                             progress=_walk_progress)
    if not folders and not docs:
        print_fn(f"  [warn] no child nodes found under {project_id}. "
                 f"Last listing error: {_LAST_CHILD_ERR['value'] or 'none'}")
    all_nodes = targets + folders + docs
    done = _already(tracker, pnum)
    todo = [n for n in all_nodes if n not in done]
    print_fn(f"  Nodes: {len(all_nodes)} total ({len(folders)} folders, "
             f"{len(docs)} documents) | {len(done)} already classified | "
             f"{len(todo)} to do")
    if dry_run:
        return {"nodes": len(all_nodes), "ok": 0, "fail": 0,
                "would_do": len(todo)}

    work = queue.Queue()
    for n in todo:
        work.put(n)
    counters = {"ok": 0, "fail": 0}
    c_lock = threading.Lock()
    db_lock = threading.Lock()
    fail_msgs = []
    f_lock = threading.Lock()
    batch, b_lock = [], threading.Lock()
    w0 = time.time()

    def _worker():
        ws = _session(cfg, ticket)
        while True:
            try:
                nid = work.get_nowait()
            except queue.Empty:
                return
            wanted = class_ids
            if merge:
                back = _get_classifications(ws, base, nid, own_only=True)
                if back is None:
                    with c_lock:
                        counters["fail"] += 1
                    with f_lock:
                        if len(fail_msgs) < 5:
                            fail_msgs.append(
                                f"node {nid}: existing classifications "
                                f"unreadable, skipped to avoid stripping them")
                    continue
                wanted = sorted(back | set(class_ids))
            ok, msg = _assign(ws, base, nid, wanted)
            with c_lock:
                counters["ok" if ok else "fail"] += 1
                n_done = counters["ok"] + counters["fail"]
            if ok:
                with b_lock:
                    batch.append(nid)
                    flush = batch[:] if len(batch) >= 200 else None
                    if flush:
                        del batch[:]
                if flush:
                    _record(tracker, pnum, flush, class_ids, db_lock)
            else:
                tracker.log("ERROR", pnum, f"Classify node {nid} failed: {msg}")
                with f_lock:
                    if len(fail_msgs) < 5:
                        fail_msgs.append(f"node {nid}: {msg}")
            if n_done % 500 == 0:
                el = max(time.time() - w0, 0.001)
                rate = n_done / el
                eta = int((len(todo) - n_done) / rate) if rate else 0
                print_fn(f"  [{n_done}/{len(todo)}] ok={counters['ok']} "
                         f"fail={counters['fail']} rate={rate:.1f}/s "
                         f"eta={eta // 60}m{eta % 60:02d}s")

    ex = ThreadPoolExecutor(max_workers=max(workers, 1))
    futs = [ex.submit(_worker) for _ in range(max(workers, 1))]
    try:
        for f in futs:
            while True:
                try:
                    f.result(timeout=0.5)
                    break
                except FuturesTimeout:
                    continue
    except KeyboardInterrupt:
        print_fn("\n  [interrupt] stopping classification (progress is saved)")
        ex.shutdown(wait=True, cancel_futures=True)
        if batch:
            _record(tracker, pnum, batch, class_ids, db_lock)
        raise SystemExit(1)
    ex.shutdown(wait=True)
    if batch:
        _record(tracker, pnum, batch, class_ids, db_lock)

    tracker.log("INFO", pnum,
                f"Closed-project classification {class_ids}: "
                f"ok={counters['ok']} fail={counters['fail']}")
    print_fn(f"  Classification: ok={counters['ok']} fail={counters['fail']} "
             f"in {int(time.time() - t0)}s")
    for m in fail_msgs:
        print_fn(f"    [classify FAIL] {m}")
    return {"nodes": len(all_nodes), **counters}


def maybe_classify_closed(cfg, tracker, ticket, pnum, print_fn=print,
                          folders_only=False):
    """Hook used during migrate - no-op unless the project is Closed.

    Called twice per project:
      * right after the workspaces are related, with folders_only=True, so the
        Project workspace and its template folders are flagged immediately;
      * again at COMPLETE, covering the tool-created folders and every
        uploaded document.
    Classification is NOT inherited by nodes created later, which is why the
    second pass is required rather than optional.
    """
    if not enabled(cfg):
        return
    prow = tracker.get_project(pnum)
    if not prow or not is_closed(cfg, prow):
        return
    what = "workspace + template folders" if folders_only else "everything"
    print_fn(f"  Project {pnum} is CLOSED - applying classification "
             f"{_blk(cfg).get('classification_ids')} ({what})")
    classify_project(cfg, tracker, ticket, pnum, print_fn=print_fn,
                     folders_only=folders_only)


# ------------------------------------------------ creation-time strategy
def inject_classification(cfg, tracker, pnum, print_fn=print):
    """Return a cfg whose workspace creation already includes the closed id.

    The REST API on this build has no additive classification call - the only
    write replaces a node's entire list.  So for a CLOSED project the id is
    added to `project.classification_ids` (and master's, if that key is used)
    BEFORE the workspace is created.  Content Server applies it as part of
    creation, which means:

      * no existing classification is ever re-posted, altered or removed;
      * the workspace is never written to after creation;
      * folders and documents created underneath inherit it automatically.

    Returns (cfg_to_use, injected: bool).  The original cfg is not mutated.
    """
    blk = _blk(cfg)
    class_ids = [int(c) for c in (blk.get("classification_ids") or [])]
    if not enabled(cfg) or not class_ids:
        return cfg, False
    prow = tracker.get_project(pnum)
    if not prow or not is_closed(cfg, prow):
        return cfg, False

    out = dict(cfg)
    for section in ("project", "master"):
        sect = dict(cfg.get(section, {}) or {})
        if section == "project" or "classification_ids" in sect:
            existing = [int(c) for c in (sect.get("classification_ids") or [])]
            merged = existing + [c for c in class_ids if c not in existing]
            sect["classification_ids"] = merged
            out[section] = sect
            print_fn(f"  {section}.classification_ids for this CLOSED project: "
                     f"{existing} -> {merged}")
    return out, True


def report_workspace_classifications(cfg, ticket, node_id, label="workspace",
                                     print_fn=print):
    """Read-only: show what a freshly created node ended up with."""
    s = _session(cfg, coerce_ticket(ticket))
    got = _get_classifications(s, api_base(cfg), int(node_id))
    if got is None:
        print_fn(f"  [warn] cannot read classifications on {label} {node_id}")
        return None
    print_fn(f"  {label} {node_id} classifications: {sorted(got)}")
    return got


# --------------------------------------------------- inheritance strategy
def apply_workspace(cfg, tracker, ticket, pnum, print_fn=print,
                    include_existing=True):
    """Put the closed classification on the Master + Project workspace ONLY.

    On this build classifications are inherited by children AT CREATION TIME
    (a new folder picks up every ancestor classification whose inherit_flag is
    set).  So classifying the workspace before Phase A creates any folder means
    every folder and document inherits it automatically - no per-node writes,
    and nothing else on the tree is rewritten.

    The one gap is folders that already exist when we get here: the template
    folders, created together with the workspace.  include_existing=True gives
    those the same treatment (their displayed list plus the closed id), which
    is the only option the API offers.

    Must run BEFORE folders are created.  Nodes created earlier do not pick up
    a classification added later.
    """
    blk = _blk(cfg)
    class_ids = [int(c) for c in (blk.get("classification_ids") or [])]
    if not class_ids:
        return {"ok": 0, "fail": 0}
    ticket = coerce_ticket(ticket) or _auth_ticket(cfg)
    if not ticket:
        print_fn("  [FAIL] no usable OTCSTicket - cannot classify")
        return {"ok": 0, "fail": 0}
    prow = tracker.get_project(pnum)
    if not prow or not prow["project_id"]:
        return {"ok": 0, "fail": 0}

    _ensure_table(tracker)
    base = api_base(cfg)
    s = _session(cfg, ticket)
    ok = fail = 0
    touched = []

    targets = [("Project", int(prow["project_id"]))]
    if blk.get("apply_to_master") and prow["master_id"]:
        targets.append(("Master", int(prow["master_id"])))

    if include_existing:
        # template folders that already exist under the project
        for ch in _children(s, base, int(prow["project_id"])):
            if ch.get("id"):
                targets.append(("template folder", int(ch["id"])))

    for label, nid in targets:
        current = _get_classifications(s, base, nid)
        if current is None:
            print_fn(f"  [skip] {label} {nid}: classifications unreadable - "
                     f"not writing (a POST would replace them)")
            fail += 1
            continue
        if set(class_ids).issubset(current):
            print_fn(f"  [ok]   {label} {nid}: already has {class_ids}")
            touched.append(nid)
            ok += 1
            continue
        wanted = sorted(current | set(class_ids))
        good, msg = _assign(s, base, nid, wanted, sub_items=False)
        if good:
            print_fn(f"  [OK]   {label} {nid}: {sorted(current)} -> {wanted}")
            touched.append(nid)
            ok += 1
        else:
            print_fn(f"  [FAIL] {label} {nid}: {msg}")
            fail += 1

    if touched:
        _record(tracker, pnum, touched, class_ids, threading.Lock())
    tracker.log("INFO", pnum,
                f"Closed classification {class_ids} on workspace/template "
                f"nodes: ok={ok} fail={fail} (children inherit at creation)")
    return {"ok": ok, "fail": fail}


def verify_inherited(cfg, ticket, node_id, print_fn=print):
    """Spot-check that a node created after the workspace inherited the id."""
    blk = _blk(cfg)
    class_ids = {int(c) for c in (blk.get("classification_ids") or [])}
    if not class_ids or not node_id:
        return None
    s = _session(cfg, coerce_ticket(ticket))
    got = _get_classifications(s, api_base(cfg), int(node_id))
    if got is None:
        print_fn(f"  [warn] cannot read classifications on {node_id}")
        return None
    good = class_ids.issubset(got)
    print_fn(f"  Inheritance check on folder {node_id}: {sorted(got)} "
             f"-> {'INHERITED OK' if good else 'NOT INHERITED'}")
    return good


def strategy(cfg):
    """create   - add the id at workspace creation; never write afterwards.
    inherit  - create normally, then POST the workspace (rewrites its list).
    per_node - POST every folder and document explicitly."""
    return str((_blk(cfg).get("strategy") or "create")).lower()


# ------------------------------------------------------- inline classifier
class Classifier:
    """Applies the closed classification to nodes AS THEY ARE CREATED.

    Used by migrate.py: every folder prepared in Phase A and every document
    uploaded in Phase B is classified immediately, which removes the need to
    re-walk a 100k-node tree afterwards.

    Thread-safe: one requests session per worker thread, buffered writes to
    the tracker.  A classification failure never fails the document - it is
    counted and logged, and `migrate.py classify --project N` sweeps up
    whatever was missed (successes are recorded, so the sweep only retries
    the gaps).
    """

    def __init__(self, cfg, tracker, ticket, pnum, active=False):
        self.cfg = cfg
        self.tracker = tracker
        self.ticket = coerce_ticket(ticket) or _auth_ticket(cfg)
        self.pnum = str(pnum)
        self.active = bool(active and self.ticket)
        if active and not self.ticket:
            print("  [warn] closed classification disabled: no usable "
                  "OTCSTicket")
        blk = _blk(cfg)
        self.class_ids = [int(c) for c in (blk.get("classification_ids") or [])]
        self.base = api_base(cfg)
        self.merge_folders = bool(blk.get("merge_existing", True))
        self._local = threading.local()
        self._lock = threading.Lock()
        self._buf = []
        self.ok = 0
        self.fail = 0
        if self.active and not self.class_ids:
            self.active = False
        if self.active:
            _ensure_table(tracker)
            self._done = _already(tracker, pnum)
        else:
            self._done = set()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = _session(self.cfg, self.ticket)
            self._local.s = s
        return s

    def apply(self, node_id, merge=None):
        """Classify one node.  Safe to call on anything; no-op when inactive."""
        if not self.active or not node_id:
            return True
        nid = int(node_id)
        if nid in self._done:
            return True
        s = self._session()
        if merge is None:
            merge = self.merge_folders
        ids = self.class_ids
        if merge:
            # Folders from the template already carry classifications; a POST
            # replaces the list, so send the existing ones back too.
            back = _get_classifications(s, self.base, nid, own_only=True)
            if back is None:
                with self._lock:
                    self.fail += 1
                self.tracker.log("WARN", self.pnum,
                                 f"Classify node {nid} skipped: existing "
                                 f"classifications unreadable (a POST would "
                                 f"strip them)")
                return False
            ids = sorted(back | set(self.class_ids))
        ok, msg = _assign(s, self.base, nid, ids, sub_items=False)
        with self._lock:
            if ok:
                self.ok += 1
                self._done.add(nid)
                self._buf.append(nid)
                flush = self._buf[:] if len(self._buf) >= 200 else None
                if flush:
                    del self._buf[:]
            else:
                self.fail += 1
                flush = None
        if not ok:
            self.tracker.log("WARN", self.pnum,
                             f"Classify node {nid} failed: {msg}")
        elif flush:
            _record(self.tracker, self.pnum, flush, self.class_ids,
                    threading.Lock())
        return ok

    def flush(self):
        if not self.active:
            return
        with self._lock:
            pending, self._buf = self._buf[:], []
        if pending:
            _record(self.tracker, self.pnum, pending, self.class_ids,
                    threading.Lock())
        self.tracker.log("INFO", self.pnum,
                         f"Inline classification {self.class_ids}: "
                         f"ok={self.ok} fail={self.fail}")


def make_classifier(cfg, tracker, ticket, pnum, print_fn=print):
    """Build a Classifier, active only for CLOSED projects with inline on.

    Under the default 'inherit' strategy this is deliberately INACTIVE: new
    folders and documents pick the classification up from the workspace on
    their own, so touching each node would only rewrite lists needlessly.
    """
    blk = _blk(cfg)
    if strategy(cfg) == "inherit":
        return Classifier(cfg, tracker, ticket, pnum, active=False)
    if not enabled(cfg) or not blk.get("inline", True):
        return Classifier(cfg, tracker, ticket, pnum, active=False)
    prow = tracker.get_project(pnum)
    active = bool(prow and is_closed(cfg, prow))
    c = Classifier(cfg, tracker, ticket, pnum, active=active)
    if c.active:
        print_fn(f"  Project {pnum} is CLOSED - classification "
                 f"{c.class_ids} will be applied inline to every folder and "
                 f"document as it is created")
    return c
