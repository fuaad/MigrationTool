"""backfill.py - repair folders that are missing their categories.

Folders created before the explicit-category fix (or by any interrupted run)
can be bare: a folder whose own category was inherited stops propagating it
to children once its inheritance is disabled, so whole sub-trees end up with
no category at all.

This walks the project workspace, and for every FOLDER missing one of the
configured categories, applies it (empty values) and leaves inheritance
enabled so future user uploads inherit normally.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout


def _node_categories(otcs, node_id):
    import requests
    url = f"{otcs._rest_url()}/v2/nodes/{node_id}/categories"
    r = requests.get(url, headers={"OTCSTicket": otcs.ticket()}, timeout=60)
    if r.status_code != 200:
        return None
    found = set()
    for block in r.json().get("results", []):
        cats = (block.get("data", {}) or {}).get("categories", {}) or {}
        for key in cats:
            found.add(str(key).split("_")[0])
    return found


def backfill(pool, project_id, cat_ids, workers=8, progress_every=200,
             dry_run=False, heartbeat_s=30):
    """Walk the workspace; apply missing categories to folders."""
    cat_ids = [int(c) for c in (cat_ids or [])]
    if not cat_ids:
        return {"folders": 0, "fixed": 0, "already_ok": 0, "errors": 0}

    work = queue.Queue()
    work.put(project_id)
    seen = set([project_id])
    lock = threading.Lock()
    stats = {"folders": 0, "fixed": 0, "already_ok": 0, "errors": 0}
    active = [0]

    idle_rounds = [0]

    def worker():
        otcs, _x = pool.get()
        try:
            while True:
                try:
                    nid = work.get(timeout=1.0)
                except queue.Empty:
                    # Terminate only when the queue has been empty AND no
                    # worker has been active for several consecutive checks -
                    # a single worker walking a huge container must not cause
                    # the others to exit early.
                    with lock:
                        if active[0] == 0 and work.empty():
                            idle_rounds[0] += 1
                            done = idle_rounds[0] >= 5
                        else:
                            idle_rounds[0] = 0
                            done = False
                    if done:
                        return
                    continue
                with lock:
                    idle_rounds[0] = 0
                    active[0] += 1
                try:
                    for cid, ctype in otcs.list_children(nid):
                        if ctype != 0:      # documents: skip
                            continue
                        with lock:
                            if cid in seen:
                                continue
                            seen.add(cid)
                            stats["folders"] += 1
                            n = stats["folders"]
                        work.put(cid)

                        have = _node_categories(otcs, cid)
                        if have is None:
                            with lock:
                                stats["errors"] += 1
                            continue
                        missing = [c for c in cat_ids if str(c) not in have]
                        if not missing:
                            with lock:
                                stats["already_ok"] += 1
                        else:
                            if not dry_run:
                                for c in missing:
                                    try:
                                        otcs.apply_category(cid, c)
                                    except Exception:
                                        with lock:
                                            stats["errors"] += 1
                            with lock:
                                stats["fixed"] += 1
                        if progress_every and n % progress_every == 0:
                            print(f"  [backfill {n} folders] fixed="
                                  f"{stats['fixed']} ok={stats['already_ok']} "
                                  f"errors={stats['errors']}")
                finally:
                    with lock:
                        active[0] -= 1
        finally:
            pool.put((otcs, _x))

    # Heartbeat so a long walk is distinguishable from a hang.
    import time as _time
    stop_hb = threading.Event()

    def _heartbeat():
        last = -1
        while not stop_hb.wait(heartbeat_s):
            with lock:
                n = stats["folders"]
                q = work.qsize()
                a = active[0]
            if n == last:
                print(f"  [backfill working... {n} folders seen, "
                      f"queue={q}, active workers={a}]")
            last = n

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    n_workers = max(1, min(workers, pool.qsize()))
    ex = ThreadPoolExecutor(max_workers=n_workers)
    futs = [ex.submit(worker) for _ in range(n_workers)]
    for f in futs:
        while True:
            try:
                f.result(timeout=0.5)
                break
            except FuturesTimeout:
                continue
    ex.shutdown(wait=True)
    stop_hb.set()
    return stats
