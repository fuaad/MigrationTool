#!/usr/bin/env python3
"""migrate.py - xENG Migration Tool CLI (scan-driven).

Workflow:
  ingest                     Load Projects (+ optional Records) Excel.
  scan   [--project N]       Walk the network drive, build document inventory.
  match  [--project N]       Match records to files by name (never guesses).
  validate                   Preflight checks on scanned documents.
  migrate --project N|--all  Create workspaces, upload docs, apply revisions.
  status                     Print status summary.
  report [--out file.xlsx]   Export the Excel audit report.
  classify --project N|--all Apply the Closed-project classification to a
                             project workspace and everything under it.

Documents WITHOUT a matched record migrate structure-only.
Documents WITH a matched record get its metadata; revision is applied only
when the record has a Revision Stage.
All steps are resumable; COMPLETE work is skipped, FAILED retried.
"""

from __future__ import annotations

import os
import sys
import json
import time
import queue
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from core.config import load_config
from core.tracker import Tracker
from core.ingest import ingest_projects, ingest_records
from core.reporting import export_report


def _throttle(cfg):
    ms = cfg.get("runtime", {}).get("throttle_ms", 0)
    if ms:
        time.sleep(ms / 1000.0)


def _fmt_dur(seconds):
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _winpath(p):
    """Return a path safe for Windows long-path (>260 char) file access."""
    if p and os.name == "nt":
        p = os.path.abspath(p)
        if not p.startswith("\\\\?\\"):
            # UNC paths need \\?\UNC\server\share..., local need \\?\C:\...
            if p.startswith("\\\\"):
                p = "\\\\?\\UNC" + p[1:]
            else:
                p = "\\\\?\\" + p
    return p


_LAST_TICKET = {"value": None}


def _coerce_ticket(val):
    """pyxecm exposes otcs_ticket as a METHOD - call it, and only accept a
    string.  Returning the bound method makes every REST header invalid."""
    if callable(val):
        try:
            val = val()
        except Exception:
            return None
    if isinstance(val, bytes):
        try:
            val = val.decode("utf-8", "ignore")
        except Exception:
            return None
    return val if isinstance(val, str) and val.strip() else None


def _get_ticket(otcs):
    """Extract the raw OTCSTicket string from an authenticated client."""
    ticket = None
    for attr in ("otcs_ticket", "_otcs_ticket", "ticket"):
        ticket = _coerce_ticket(getattr(otcs.otcs, attr, None))
        if ticket:
            break
    if not ticket:
        try:
            ticket = _coerce_ticket(otcs.otcs.cookie().get("OTCSTicket"))
        except Exception:
            ticket = None
    if ticket:
        _LAST_TICKET["value"] = ticket
    return ticket


def _make_clients(cfg):
    from core.otcs_client import OtcsClient
    otcs = OtcsClient(cfg)
    otcs.authenticate()
    ticket = _get_ticket(otcs)
    if not ticket:
        print("  [warn] could not read a usable OTCSTicket string from the "
              "client - revision + classification steps may be skipped")
    from core.xeng_client import XengClient
    xeng = XengClient(cfg, ticket) if ticket else None
    return otcs, xeng


# ---------------------------------------------------------------------
def migrate_project(cfg, tracker, pool, pnum):
    p = tracker.get_project(pnum)
    if not p:
        print(f"  [skip] {pnum}: not in tracker")
        return
    if p["status"] == "COMPLETE":
        print(f"  [done] {pnum}: already COMPLETE")
        return

    extra = json.loads(p["extra_json"] or "{}")
    # CLOSED projects: put the closed classification into the creation payload
    # so the workspaces are born with it.  Nothing is written afterwards, and
    # no other classification is touched.
    _injected = False
    try:
        from core.classify import (inject_classification, strategy as
                                   _cls_strategy0)
        if _cls_strategy0(cfg) == "create":
            cfg, _injected = inject_classification(cfg, tracker, pnum)
    except Exception as _ie:
        print(f"  [warn] closed classification injection skipped: {_ie}")
    _proj_t0 = time.time()
    _phase_times = {}
    _now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    # Migration start = the first run that touched this project; keep it
    # across resumed runs so the report shows the true start.
    try:
        if not p["migration_start"]:
            tracker.set_project(pnum, migration_start=_now_str)
    except (KeyError, IndexError):
        pass
    print(f"  Started at    : {_now_str}")

    otcs, _xeng0 = pool.get()   # borrow one client for the workspace phase
    pool.put((otcs, _xeng0))    # returned immediately; single-threaded phase

    # 1. Master
    master_id = p["master_id"]
    try:
        if not master_id:
            master_id = otcs.create_master(cfg, pnum)
            if not master_id:
                raise RuntimeError("create_master returned no id")
            tracker.set_project(pnum, master_id=master_id, status="MASTER_CREATED")
            tracker.log("INFO", pnum, f"Master created id={master_id}")
            _throttle(cfg)
    except Exception as e:
        tracker.set_project(pnum, status="FAILED", error=f"master: {e}")
        tracker.log("ERROR", pnum, f"Master failed: {e}")
        print(f"  [FAIL] {pnum}: master: {e}")
        return

    # 2. Project
    project_id = p["project_id"]
    try:
        if not project_id:
            project_id = otcs.create_project(
                cfg, pnum, p["project_title"], p["program"], p["business_line"], extra)
            if not project_id:
                raise RuntimeError("create_project returned no id")
            tracker.set_project(pnum, project_id=project_id, status="PROJECT_CREATED")
            tracker.log("INFO", pnum, f"Project created id={project_id}")
            if _injected:
                try:
                    from core.classify import report_workspace_classifications
                    report_workspace_classifications(
                        cfg, _LAST_TICKET["value"], project_id,
                        "Project workspace")
                    if master_id:
                        report_workspace_classifications(
                            cfg, _LAST_TICKET["value"], master_id,
                            "Master workspace")
                except Exception:
                    pass
            _throttle(cfg)
    except Exception as e:
        tracker.set_project(pnum, status="FAILED", error=f"project: {e}")
        tracker.log("ERROR", pnum, f"Project failed: {e}")
        print(f"  [FAIL] {pnum}: project: {e}")
        return

    # 3. Relationship
    try:
        cur = tracker.get_project(pnum)["status"]
        if cur not in ("RELATED", "VERIFIED", "COMPLETE"):
            if otcs.relate(master_id, project_id):
                tracker.set_project(pnum, status="RELATED", error=None)
                tracker.log("INFO", pnum, "Relationship created (Master->child Project)")
                _throttle(cfg)
            else:
                raise RuntimeError("relate returned falsy")
    except Exception as e:
        tracker.set_project(pnum, status="FAILED", error=f"relate: {e}")
        tracker.log("ERROR", pnum, f"Relate failed: {e}")
        print(f"  [FAIL] {pnum}: relate: {e}")
        return

    # 3b. Closed projects: classify the workspace NOW, before Phase A creates
    #     any folder.  Children inherit an ancestor's classification at the
    #     moment they are created, so ordering is what makes this work - a
    #     classification added later is not picked up retroactively.
    try:
        from core.classify import (enabled as _cls_enabled, is_closed as
                                   _cls_is_closed, strategy as _cls_strategy,
                                   apply_workspace, maybe_classify_closed)
        if (_cls_strategy(cfg) != "create" and _cls_enabled(cfg)
                and _cls_is_closed(cfg, tracker.get_project(pnum))):
            blk = cfg.get("closed_projects", {}) or {}
            if _cls_strategy(cfg) == "inherit":
                print(f"  Project {pnum} is CLOSED - classifying workspace "
                      f"{blk.get('classification_ids')}; folders and documents "
                      f"created below will inherit it")
                apply_workspace(cfg, tracker, _LAST_TICKET["value"], pnum,
                                include_existing=bool(blk.get(
                                    "include_existing_folders", True)))
            else:
                maybe_classify_closed(cfg, tracker, _LAST_TICKET["value"],
                                      pnum, folders_only=True)
    except Exception as ce:
        tracker.log("WARN", pnum, f"Closed classification (structure) "
                                  f"skipped: {ce}")
        print(f"  [warn] closed classification (structure) skipped: {ce}")

    # 3c. Inline classifier: folders and documents get the closed
    #     classification at the moment they are created.
    try:
        from core.classify import make_classifier
        clf = make_classifier(cfg, tracker, _LAST_TICKET["value"], pnum)
    except Exception as ce:
        print(f"  [warn] inline classifier unavailable: {ce}")

        class _NoClf:
            active = False
            ok = fail = 0

            def apply(self, *a, **k):
                return True

            def flush(self):
                pass

        clf = _NoClf()

    # 4. Documents (from scan) - parallel workers.
    docs = tracker.documents_for(pnum, statuses=["PENDING", "FAILED", "LOADED"])
    all_docs = tracker.documents_for(pnum)
    total = len(all_docs)
    done = total - len(docs)
    doc_action = (cfg.get("revisions", {}) or {}).get("control_action", "changerevisionstatus")
    rev_enabled = (cfg.get("revisions", {}) or {}).get("enabled", True)
    docmeta_map = cfg.get("document", {}).get("attributes", {})
    doc_cat_id = cfg.get("document", {}).get("category_id")
    inh_ids = cfg.get("document", {}).get("disable_inheritance_categories", []) or []

    inh_disabled = set()
    inh_lock = threading.Lock()
    counters = {"ok": 0, "fail": 0}
    cnt_lock = threading.Lock()
    t0 = time.time()
    progress_every = int(cfg.get("runtime", {}).get("progress_every", 25))
    last_printed = [-1]

    if docs:
        print(f"  Documents: {total} total | {done} already complete | "
              f"{len(docs)} to process | workers={pool.qsize()}")

    def _progress(force=False):
        with cnt_lock:
            processed = counters["ok"] + counters["fail"]
            if not force and (progress_every <= 0 or processed % progress_every != 0):
                return
            if processed == last_printed[0]:
                return
            last_printed[0] = processed
            ok, fail = counters["ok"], counters["fail"]
        elapsed = max(time.time() - t0, 0.001)
        rate = processed / elapsed
        remaining_n = len(docs) - processed
        eta_s = int(remaining_n / rate) if rate > 0 else 0
        eta = f"{eta_s // 3600}h{(eta_s % 3600) // 60:02d}m" if eta_s >= 3600 \
            else f"{eta_s // 60}m{eta_s % 60:02d}s"
        pct = (done + processed) * 100.0 / total if total else 100.0
        print(f"  [{done + processed}/{total} {pct:5.1f}%] "
              f"ok={ok} fail={fail} rate={rate:.1f}/s eta={eta}")

    def _disable_inheritance(otcs, folder_id, force=False):
        """Disable category inheritance on an upload-target folder.

        Only the bookkeeping set is guarded; the REST calls run in parallel
        (safe: creating subfolders no longer touches parent inheritance).
        """
        if not inh_ids:
            return
        with inh_lock:
            if not force and folder_id in inh_disabled:
                return
            inh_disabled.add(folder_id)
        codes = {}
        for cid in inh_ids:
            codes[cid] = otcs.disable_category_inheritance(folder_id, cid)
        tracker.log("INFO", pnum,
                    f"Disable inheritance on folder {folder_id}: {codes}")

    def _process_doc(d):
        did = d["id"]
        otcs_w, xeng_w = pool.get()
        try:
            document_id = d["document_id"]
            target_folder_id = d["target_folder_id"]
            if not document_id:
                src_p = _winpath(d["source_path"])
                if not d["source_path"] or not os.path.isfile(src_p):
                    raise FileNotFoundError(f"source not found: {d['source_path']}")
                if not target_folder_id:
                    target_folder_id = path_ids.get(d["folder_path"] or "")
                    if not target_folder_id:
                        raise RuntimeError(
                            f"folder not prepared: {d['folder_path']}")
                    tracker.set_document(did, target_folder_id=target_folder_id)

                category_data = _build_doc_category(
                    d, docmeta_map, doc_cat_id,
                    cfg.get("document", {}).get("defaults", {}))
                document_id = otcs_w.upload_document(
                    parent_id=target_folder_id,
                    file_path=src_p,
                    file_name=d["file_name"],
                    category_data=category_data,
                )
                if not document_id:
                    # Likely uploaded by an interrupted earlier run before the
                    # tracker could record it - adopt the existing node.
                    document_id = otcs_w.find_document(
                        target_folder_id, d["file_name"])
                    if document_id:
                        tracker.log("INFO", pnum,
                                    f"Adopted existing {d['file_name']} "
                                    f"id={document_id}")
                    else:
                        # Possibly a lost race on folder inheritance (another
                        # worker re-enabled the parent while creating a
                        # subfolder) - force re-disable and retry once.
                        _disable_inheritance(otcs_w, target_folder_id,
                                             force=True)
                        document_id = otcs_w.upload_document(
                            parent_id=target_folder_id,
                            file_path=src_p,
                            file_name=d["file_name"],
                            category_data=category_data,
                        )
                if not document_id:
                    raise RuntimeError("upload returned no id")
                tracker.set_document(did, document_id=document_id,
                                     status="LOADED", error=None)
                # freshly uploaded: nothing to preserve, so skip the read-back
                clf.apply(document_id, merge=False)

            needs_revision = (rev_enabled and xeng_w and
                              d["match_status"] == "MATCHED" and d["revision_stage"])
            if needs_revision:
                rev_status_id = d["rev_status_id"]
                if not rev_status_id:
                    rev_status_id = xeng_w.resolve_status_id(
                        d["revision_stage"], doc_action, document_id)
                    if rev_status_id is not None:
                        tracker.set_document(did, rev_status_id=rev_status_id)
                ok_v, msg = xeng_w.validate(document_id, doc_action,
                                            rev_status_id, project_id)
                if not ok_v:
                    raise RuntimeError(f"revision validate failed: {msg}")
                xeng_w.perform_revision(
                    node_id=document_id, action=doc_action,
                    rev_status_id=rev_status_id,
                    master_workspace_id=master_id, project_id=project_id)
                tracker.set_document(did, status="REVISION_SET", error=None)

            tracker.set_document(did, status="COMPLETE", error=None)
            with cnt_lock:
                counters["ok"] += 1
            _progress()
        except Exception as e:
            tracker.set_document(did, status="FAILED", error=str(e))
            tracker.log("ERROR", pnum, f"Doc {d['file_name']} failed: {e}")
            print(f"    [doc FAIL] {d['file_name']}: {e}")
            with cnt_lock:
                counters["fail"] += 1
            _progress()
        finally:
            pool.put((otcs_w, xeng_w))

    # ---- Phase A: prepare all folders (partitioned by top-level segment,
    #      one worker per subtree - folder creation and inheritance toggling
    #      finish completely before any upload starts).
    path_ids = {}
    if docs:
        need_paths = sorted({(d["folder_path"] or "") for d in docs
                             if not d["target_folder_id"]})
        # carry over ids already resolved in previous runs
        for d in docs:
            if d["target_folder_id"]:
                path_ids[d["folder_path"] or ""] = d["target_folder_id"]
        if need_paths:
            print(f"  Folders: preparing {len(need_paths)} folder path(s) "
                  f"with {pool.qsize()} worker(s)...")
        pid_lock = threading.Lock()
        prep_errors = []

        # One folder cache shared by every worker client: a folder found or
        # created by one worker is immediately known to all the others
        # (avoids re-walking and duplicate create attempts on shared parents).
        shared_cache = {}
        cache_lock = threading.Lock()
        borrowed = []
        while not pool.empty():
            borrowed.append(pool.get())
        for _o, _x in borrowed:
            _o.set_folder_cache(shared_cache, cache_lock)
        for item in borrowed:
            pool.put(item)

        # Shortest paths first: parents are created before their children,
        # so deep paths resolve from cache instead of walking from the root.
        work_q = queue.Queue()
        for fp in sorted(need_paths, key=lambda p: (p.count("\\"), p)):
            work_q.put(fp)
        prep_done = [0]
        prep_t0 = time.time()
        n_paths = len(need_paths)

        def _prep_worker():
            otcs_w, x_w = pool.get()
            try:
                while True:
                    try:
                        fp = work_q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        if fp == "":
                            fid = project_id
                        else:
                            fid = otcs_w.resolve_folder(
                                project_id, fp, folder_cat_ids=inh_ids)
                        if not fid:
                            raise RuntimeError("no folder id")
                        _disable_inheritance(otcs_w, fid)
                        clf.apply(fid)
                        with pid_lock:
                            path_ids[fp] = fid
                    except Exception as pe:
                        with pid_lock:
                            prep_errors.append((fp, str(pe)))
                    with pid_lock:
                        prep_done[0] += 1
                        n = prep_done[0]
                    if n % 200 == 0 or n == n_paths:
                        el = max(time.time() - prep_t0, 0.001)
                        r = n / el
                        eta_s = int((n_paths - n) / r) if r > 0 else 0
                        eta = (f"{eta_s // 3600}h{(eta_s % 3600) // 60:02d}m"
                               if eta_s >= 3600 else
                               f"{eta_s // 60}m{eta_s % 60:02d}s")
                        print(f"  [folders {n}/{n_paths}] "
                              f"rate={r:.1f}/s eta={eta}")
            finally:
                pool.put((otcs_w, x_w))

        if need_paths:
            fex = ThreadPoolExecutor(max_workers=pool.qsize())
            ffuts = [fex.submit(_prep_worker) for _ in range(pool.qsize())]
            try:
                for f in ffuts:
                    while True:
                        try:
                            f.result(timeout=0.5)
                            break
                        except FuturesTimeout:
                            continue
            except KeyboardInterrupt:
                print("\n  [interrupt] stopping during folder preparation...")
                fex.shutdown(wait=True, cancel_futures=True)
                raise SystemExit(1)
            fex.shutdown(wait=True)
            _phase_times["folders"] = time.time() - _proj_t0
            print(f"  Folders ready: {len(path_ids)} prepared, "
                  f"{len(prep_errors)} error(s) "
                  f"in {_fmt_dur(_phase_times['folders'])}")
            # Spot-check one newly created folder: did it inherit the closed
            # classification?  Cheap, and catches the ordering mistake early.
            try:
                from core.classify import (verify_inherited, strategy as
                                           _cls_strategy2, enabled as
                                           _cls_enabled2, is_closed as
                                           _cls_closed2)
                if (_cls_enabled2(cfg)
                        and _cls_strategy2(cfg) in ("inherit", "create")
                        and _cls_closed2(cfg, tracker.get_project(pnum))):
                    _sample = next((v for k, v in path_ids.items() if k), None)
                    if _sample:
                        verify_inherited(cfg, _LAST_TICKET["value"], _sample)
            except Exception as _ve:
                print(f"  [warn] inheritance check skipped: {_ve}")
            for fp, pe in prep_errors[:10]:
                print(f"    [folder FAIL] {fp}: {pe}")

    # ---- Phase B: pure parallel uploads (no folder ops, no races).
    if docs:
        workers = pool.qsize()
        ex = ThreadPoolExecutor(max_workers=workers)
        futures = [ex.submit(_process_doc, d) for d in docs]
        try:
            for f in futures:
                # short-timeout polling keeps the main thread responsive
                while True:
                    try:
                        f.result(timeout=0.5)
                        break
                    except TimeoutError:
                        continue
                    except FuturesTimeout:
                        continue
        except KeyboardInterrupt:
            print("\n  [interrupt] stopping... waiting for in-flight uploads "
                  "to finish (completed work is saved; run again to resume)")
            ex.shutdown(wait=True, cancel_futures=True)
            _progress(force=True)
            raise SystemExit(1)
        ex.shutdown(wait=True)
        _phase_times["documents"] = (time.time() - _proj_t0
                                     - _phase_times.get("folders", 0))
        _progress(force=True)
        print(f"  Documents phase: {_fmt_dur(_phase_times['documents'])}")

    remaining = tracker.documents_for(pnum, statuses=["PENDING", "FAILED", "LOADED"])
    if not remaining:
        # Re-enable inheritance on every folder we uploaded into (distinct
        # folder ids also heal folders left disabled by interrupted runs).
        if inh_ids:
            folder_ids = {d["target_folder_id"] for d in tracker.documents_for(pnum)
                          if d["target_folder_id"]}
            print(f"  Restoring category inheritance on {len(folder_ids)} "
                  f"folder(s)...")
            rq = queue.Queue()
            for fid in folder_ids:
                rq.put(fid)

            def _restore_worker():
                o_w, x_w = pool.get()
                try:
                    while True:
                        try:
                            fid = rq.get_nowait()
                        except queue.Empty:
                            return
                        for cid in inh_ids:
                            try:
                                o_w.enable_category_inheritance(fid, cid)
                            except Exception:
                                pass
                finally:
                    pool.put((o_w, x_w))

            rex = ThreadPoolExecutor(max_workers=pool.qsize())
            rfuts = [rex.submit(_restore_worker) for _ in range(pool.qsize())]
            for f in rfuts:
                while True:
                    try:
                        f.result(timeout=0.5)
                        break
                    except FuturesTimeout:
                        continue
            rex.shutdown(wait=True)
            _phase_times["restore"] = (time.time() - _proj_t0
                                       - _phase_times.get("folders", 0)
                                       - _phase_times.get("documents", 0))
            print(f"  Restore phase : {_fmt_dur(_phase_times['restore'])}")
            tracker.log("INFO", pnum,
                        f"Re-enabled inheritance for {inh_ids} on {len(folder_ids)} folder(s)")
        _end_str = time.strftime("%Y-%m-%d %H:%M:%S")
        tracker.set_project(pnum, status="COMPLETE", error=None,
                            migration_end=_end_str)
        _elapsed = time.time() - _proj_t0
        tracker.log("INFO", pnum,
                    f"Project completed in {_fmt_dur(_elapsed)} "
                    f"(phases: {', '.join(f'{k}={_fmt_dur(v)}' for k, v in _phase_times.items())})")
        print(f"  [OK]   {pnum}: COMPLETE  (Master={master_id} Project={project_id})")
        # Closed-project classification: applied inline above, so the tree
        # walk only runs when inline was off or something was missed.
        try:
            clf.flush()
            if clf.active:
                print(f"  Classification: ok={clf.ok} fail={clf.fail} "
                      f"(applied inline)")
            from core.classify import strategy as _cls_strategy3
            if _cls_strategy3(cfg) in ("inherit", "create"):
                pass          # children inherited it; no sweep needed
            elif clf.active and not clf.fail:
                pass
            else:
                from core.classify import maybe_classify_closed
                if clf.active:
                    print("  Sweeping the tree for nodes the inline pass "
                          "missed...")
                maybe_classify_closed(cfg, tracker, _LAST_TICKET["value"], pnum)
        except Exception as ce:
            tracker.log("WARN", pnum, f"Closed classification skipped: {ce}")
            print(f"  [warn] closed classification skipped: {ce}")
        print(f"  Finished at   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Total elapsed : {_fmt_dur(_elapsed)}")
    else:
        try:
            clf.flush()
            if clf.active:
                print(f"  Classification: ok={clf.ok} fail={clf.fail} "
                      f"(applied inline)")
        except Exception:
            pass
        _elapsed = time.time() - _proj_t0
        print(f"  [warn] {pnum}: {len(remaining)} document(s) still pending/failed")
        print(f"  Finished at   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Elapsed       : {_fmt_dur(_elapsed)}")


def _build_doc_category(d, docmeta_map, doc_cat_id, defaults=None):
    if not doc_cat_id or not docmeta_map:
        return None
    meta = json.loads(d["meta_json"] or "{}") if d["match_status"] == "MATCHED" else {}
    colmap = {
        "deliverable_control_log": "Deliverable Control Log (DCL)",
        "prefix_letter": "PREFIX LETTER",
        "identifying_number": "Identifying Number",
        "sheet_number": "Sheet Number",
        "sheet_size": "Sheet Size",
        "document_title": "Document Title",
        "plant_number": "Plant Number",
        "document_category": "Document Category",
        "index": "Index",
        "document_type": "Document Type",
        "document_start_date": "Document Start Date",
        "document_finish_date": "Document Finish Date",
    }
    attrs = {}
    # 1) defaults first (satisfy mandatory inherited attributes)
    for cfg_key, default_val in (defaults or {}).items():
        attr_key = docmeta_map.get(cfg_key)
        if attr_key and default_val not in (None, ""):
            attrs[attr_key] = default_val
    # 2) matched record metadata overrides defaults
    for cfg_key, attr_key in docmeta_map.items():
        if not attr_key:
            continue
        col = colmap.get(cfg_key)
        val = meta.get(col) if col else None
        if val not in (None, ""):
            attrs[attr_key] = val
    if not attrs:
        return None
    return {str(doc_cat_id): attrs}


# ---------------------------------------------------------------------
def cmd_ingest(cfg, tracker):
    inp = cfg["input"]
    np = ingest_projects(tracker, inp["projects_xlsx"], inp["projects_header_row"])
    msg = f"Ingested {np} project(s)."
    rec_path = inp.get("records_xlsx") or inp.get("register_xlsx")
    if rec_path and os.path.isfile(rec_path):
        nr = ingest_records(tracker, rec_path,
                            inp.get("records_header_row", inp.get("register_header_row", 2)))
        msg += f" Ingested {nr} record(s)."
    else:
        msg += " No records file (optional) - documents will migrate structure-only until records are provided."
    print(msg)


def cmd_scan(cfg, tracker, project=None):
    from core.scan import scan
    res = scan(tracker, cfg, project)
    print(f"Scan complete. Files seen: {res['seen']}  new: {res['new']}")
    if res["missing_project_folders"]:
        print("Projects with no folder on the drive:")
        for pnum, path in res["missing_project_folders"]:
            print(f"  {pnum}: {path}")


def cmd_match(cfg, tracker, project=None, out=None):
    from core.match import match, write_match_report
    res = match(tracker, project)
    s = res["stats"]
    print(f"Match complete. Matched: {s['matched']}  "
          f"Ambiguous: {s['ambiguous_records']}  "
          f"Records without file: {s['unmatched_records']}")
    out = out or "match_report.xlsx"
    write_match_report(res, tracker, out)
    print(f"Match report: {out}")


def cmd_migrate(cfg, tracker, project=None, do_all=False):
    run_t0 = time.time()
    print(f"Run started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    workers = int(cfg.get("runtime", {}).get("workers", 8))
    print(f"Connecting {workers} worker client(s)...")
    pool = queue.Queue()
    for _ in range(max(workers, 1)):
        pool.put(_make_clients(cfg))
    if project:
        print(f"Migrating project {project} ...")
        migrate_project(cfg, tracker, pool, project)
    elif do_all:
        for p in tracker.all_projects():
            if p["status"] != "COMPLETE":
                migrate_project(cfg, tracker, pool, p["project_number"])
    _run_elapsed = time.time() - run_t0
    print(f"\nMigration run finished: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total run time: {_fmt_dur(_run_elapsed)}")


def cmd_status(tracker):
    proj, doc, match_sum = tracker.summary()
    print("Projects :", proj)
    print("Documents:", doc)
    print("Matching :", match_sum)


def cmd_report(cfg, tracker, out):
    out = out or "migration_report.xlsx"
    path = export_report(tracker, out)
    print(f"Report written: {path}")


def main():
    ap = argparse.ArgumentParser(description="xENG Migration Tool (scan-driven)")
    ap.add_argument("command",
                    choices=["ingest", "scan", "match", "validate",
                             "migrate", "verify", "status", "report",
                             "summary", "backfill-categories",
                             "backfill-sizes", "classify"])
    ap.add_argument("--config", default="config/env.yaml")
    ap.add_argument("--project")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true",
                    help="backfill-categories/classify: report only, "
                         "change nothing")
    ap.add_argument("--force", action="store_true",
                    help="classify: apply even if the project status is not "
                         "Closed")
    ap.add_argument("--folders-only", action="store_true",
                    help="classify: workspace + folders (template folders "
                         "included), leave documents alone")
    ap.add_argument("--verify-live", action="store_true",
                    help="summary: also count folders/documents live in ECM")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tracker = Tracker(cfg.get("runtime", {}).get("tracker_db", "migration.db"))

    try:
        if args.command == "ingest":
            cmd_ingest(cfg, tracker)
        elif args.command == "scan":
            cmd_scan(cfg, tracker, args.project)
        elif args.command == "match":
            cmd_match(cfg, tracker, args.project, args.out)
        elif args.command == "validate":
            from core.validate import validate as run_validate, write_report as write_val
            v = run_validate(tracker, cfg)
            print("Preflight validation:")
            for k, val in sorted(v["per_status"].items()):
                print(f"  {k:24} {val}")
            print(f"  {'DUPLICATE_ROWS':24} {len(v['duplicates'])}")
            print(f"  {'UNREGISTERED_FILES':24} {len(v['unregistered'])}")
            out = args.out or "validation_report.xlsx"
            write_val(v, out)
            print(f"Detailed report: {out}")
        elif args.command == "migrate":
            if not args.project and not args.all:
                print("Specify --project <num> or --all"); sys.exit(2)
            cmd_migrate(cfg, tracker, args.project, args.all)
        elif args.command == "backfill-categories":
            from core.backfill import backfill
            if not args.project:
                print("Specify --project <num>"); sys.exit(2)
            p = tracker.get_project(args.project)
            if not p or not p["project_id"]:
                print("Project not migrated yet (no project_id)"); sys.exit(2)
            cat_ids = cfg.get("document", {}).get(
                "disable_inheritance_categories", []) or []
            workers = int(cfg.get("runtime", {}).get("workers", 8))
            pool = queue.Queue()
            for _ in range(max(workers, 1)):
                pool.put(_make_clients(cfg))
            print(f"Backfilling categories {cat_ids} under project "
                  f"{p['project_id']}"
                  f"{' (DRY RUN)' if args.dry_run else ''}...")
            st = backfill(pool, p["project_id"], cat_ids, workers=workers,
                          progress_every=int(cfg.get("runtime", {}).get(
                              "progress_every", 200)),
                          dry_run=args.dry_run)
            print(f"\nFolders walked : {st['folders']}")
            print(f"Already correct: {st['already_ok']}")
            print(f"Fixed          : {st['fixed']}"
                  f"{' (would fix)' if args.dry_run else ''}")
            print(f"Errors         : {st['errors']}")
        elif args.command == "backfill-sizes":
            rows = tracker.conn.execute(
                "SELECT id, source_path FROM documents WHERE "
                "(file_size IS NULL) AND source_path IS NOT NULL"
                + (" AND project_number=?" if args.project else ""),
                (args.project,) if args.project else ()).fetchall()
            print(f"Filling size for {len(rows)} document(s)...")
            done = missing = 0
            for i, row in enumerate(rows, 1):
                try:
                    sz = os.path.getsize(_winpath(row["source_path"]))
                    tracker.set_document(row["id"], file_size=sz)
                    done += 1
                except OSError:
                    missing += 1
                if i % 5000 == 0:
                    print(f"  [{i}/{len(rows)}] sized={done} missing={missing}")
            print(f"Done. sized={done} missing/unreadable={missing}")
        elif args.command == "summary":
            from core.summary_report import build, write, verify_live
            summaries = build(tracker, args.project)
            for s in summaries:
                print(f"\nProject {s['project_number']} - {s['status']}")
                print(f"  Workspace ids : Master={s['master_id']} "
                      f"Project={s['project_id']}")
                print(f"  Folders       : {s['folders_total']} total "
                      f"({s['folders_with_documents']} contain documents)")
                print(f"  Documents     : {s['total_documents']} scanned | "
                      f"{s['migrated']} migrated | {s['failed']} failed")
                _tg = s.get('total_bytes', 0) / (1024 ** 3)
                _mg = s.get('migrated_bytes', 0) / (1024 ** 3)
                print(f"  Storage       : {_tg:.2f} GB scanned | "
                      f"{_mg:.2f} GB migrated")
                print(f"  Metadata      : {s['matched']} matched | "
                      f"{s['ambiguous']} ambiguous | {s['unmatched']} no record")
                if s.get("first_activity"):
                    print(f"  First activity: {s['first_activity']}")
                    print(f"  Last activity : {s['last_activity']}")
                if s.get("completion_note"):
                    print(f"  Timing        : {s['completion_note']}")
                print("  By first-level folder:")
                print(f"    {'Folder':<28} {'Sub-folders':>11} "
                      f"{'Documents':>10} {'Migrated':>9} {'Failed':>7} "
                      f"{'Size(GB)':>10}")
                for top, v in s["by_top"].items():
                    _g = v.get('bytes', 0) / (1024 ** 3)
                    print(f"    {top[:28]:<28} {v['folders']:>11} "
                          f"{v['docs']:>10} {v['done']:>9} {v['failed']:>7} "
                          f"{_g:>10.2f}")
                if args.verify_live and s["project_id"]:
                    otcs, _x = _make_clients(cfg)
                    live = verify_live(otcs, s["project_id"])
                    print(f"  LIVE IN ECM   : {live['folders']} folders, "
                          f"{live['documents']} documents")
            out = args.out or "migration_summary.xlsx"
            write(summaries, out)
            print(f"\nSummary workbook: {out}")
        elif args.command == "classify":
            from core.classify import (classify_project, is_closed, enabled,
                                       _blk)
            if not enabled(cfg):
                print("closed_projects is disabled or has no "
                      "classification_ids in the config"); sys.exit(2)
            if not args.project and not args.all:
                print("Specify --project <num> or --all"); sys.exit(2)
            otcs = None
            if not _LAST_TICKET["value"]:
                otcs, _x = _make_clients(cfg)
            ticket = _LAST_TICKET["value"]
            if not ticket:
                print("Could not obtain an OTCSTicket"); sys.exit(2)
            if args.project:
                rows = [tracker.get_project(args.project)]
            else:
                rows = [r for r in tracker.all_projects()
                        if r["project_id"]]
            targets = [r for r in rows if r and (args.force or is_closed(cfg, r))]
            print(f"Closed projects to classify: {len(targets)} "
                  f"(classification {_blk(cfg).get('classification_ids')})"
                  f"{' (DRY RUN)' if args.dry_run else ''}")
            for r in targets:
                print(f"\nProject {r['project_number']}:")
                classify_project(cfg, tracker, ticket, r["project_number"],
                                 dry_run=args.dry_run,
                                 folders_only=args.folders_only or None)
            if not targets:
                print("Nothing to do - no project matched a closed status "
                      "(use --force to classify regardless of status).")
        elif args.command == "status":
            cmd_status(tracker)
        elif args.command == "report":
            cmd_report(cfg, tracker, args.out)
        elif args.command == "verify":
            print("Verify: use 'report' for the audit export; live verify runs during migrate.")
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
