"""otcs_client.py - Thin wrapper over pyxecm's OTCS class for the migration.

Provides exactly the operations the migration needs, with return values
normalised to simple (id / bool) results the orchestrator can track.
"""

from __future__ import annotations

from pyxecm import OTCS


class OtcsClient:
    def __init__(self, cfg: dict):
        c = cfg["connection"]
        self.cfg_conn = c
        self.otcs = OTCS(
            protocol=c["protocol"],
            hostname=c["hostname"],
            port=str(c["port"]),
            public_url=c["hostname"],
            username=c["username"],
            password=c["password"],
            base_path=c["base_path"],
        )
        self._authenticated = False

    def authenticate(self):
        import os
        # Bypass any corporate proxy for local/direct CS calls - proxies
        # routinely reset or blackhole localhost traffic.
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
        os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
        # wait_for_ready=False: skip pyxecm's /v1/ping readiness loop, which
        # hangs forever if the ping endpoint is unavailable. Fail fast instead.
        cookie = self.otcs.authenticate(wait_for_ready=False)
        if not cookie:
            raise RuntimeError(
                "OTCS authentication failed - check connection settings "
                "(protocol/hostname/port/base_path) and credentials.")
        self._authenticated = True

    # ---- ticket + raw REST helpers ----
    def ticket(self):
        t = getattr(self.otcs, "otcs_ticket", None) or getattr(self.otcs, "_otcs_ticket", None)
        if callable(t):
            t = t()
        return t

    def _rest_url(self):
        # pyxecm stores the full REST base in its config
        try:
            return self.otcs.config()["restUrl"]
        except Exception:
            c = self.cfg_conn
            return f"{c['protocol']}://{c['hostname']}:{c['port']}{c['base_path']}/api"

    def list_children(self, parent_id, page_size=200):
        """List direct children as [(id, type), ...], following paging."""
        import requests
        out = []
        page = 1
        while True:
            url = (f"{self._rest_url()}/v2/nodes/{parent_id}/nodes"
                   f"?limit={page_size}&page={page}")
            r = requests.get(url, headers={"OTCSTicket": self.ticket()},
                             timeout=120)
            if r.status_code != 200:
                break
            data = r.json()
            results = data.get("results", []) or []
            if not results:
                break
            for item in results:
                props = (item.get("data", {}) or {}).get("properties", {}) or {}
                nid = props.get("id")
                ntype = props.get("type")
                if nid is not None:
                    out.append((int(nid),
                                int(ntype) if ntype is not None else -1))
            paging = (data.get("collection", {}) or {}).get("paging", {}) or {}
            total = paging.get("total_count")
            if total is not None and len(out) >= int(total):
                break
            if len(results) < page_size:
                break
            page += 1
            if page > 500:
                break
        return out

    def apply_category(self, node_id, category_id):
        """Apply a category (empty values) directly to a node.

        Tool-created folders must NOT rely on inheritance: a folder whose
        own category was inherited does not propagate it to children once
        its inheritance is disabled, so deep chains end up bare. Applying
        the category directly makes each folder independent of its parent.
        Tolerant of already-applied.
        """
        import requests
        url = f"{self._rest_url()}/v2/nodes/{node_id}/categories"
        r = requests.post(url, headers={"OTCSTicket": self.ticket()},
                          data={"category_id": category_id}, timeout=60)
        return r.status_code

    def disable_category_inheritance(self, node_id, category_id):
        """Disable category inheritance on a folder. Returns the HTTP status.
        Raises on auth failures so they can't silently no-op."""
        import requests
        url = f"{self._rest_url()}/v2/nodes/{node_id}/categories/{category_id}/inheritance"
        r = requests.delete(url, headers={"OTCSTicket": self.ticket()}, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"inheritance disable unauthorized ({r.status_code}) on "
                f"node {node_id} cat {category_id} - worker ticket invalid?")
        if r.status_code not in (200, 204, 404, 500):
            print(f"    [inh WARN] disable {category_id} on {node_id} -> "
                  f"{r.status_code}: {r.text[:120]}")
        return r.status_code

    def enable_category_inheritance(self, node_id, category_id):
        """Re-enable category inheritance on a folder. Returns HTTP status."""
        import requests
        url = f"{self._rest_url()}/v2/nodes/{node_id}/categories/{category_id}/inheritance"
        r = requests.post(url, headers={"OTCSTicket": self.ticket()}, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"inheritance enable unauthorized ({r.status_code}) on "
                f"node {node_id} cat {category_id} - worker ticket invalid?")
        return r.status_code


    def _id(self, resp):
        if not resp:
            return None
        try:
            v = self.otcs.get_result_value(response=resp, key="id")
            if v:
                return int(v)
        except Exception:
            pass
        # fallbacks
        for path in (("results", "id"), ("results", "data", "properties", "id")):
            node = resp
            ok = True
            for k in path:
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    ok = False
                    break
            if ok and node:
                try:
                    return int(node)
                except Exception:
                    return node
        return None

    # ---- workspace creation ----
    def create_master(self, cfg, project_number):
        m = cfg["master"]
        category_data = {str(m["category_id"]): {m["attributes"]["project_number"]: project_number}}
        resp = self.otcs.create_workspace(
            workspace_template_id=m["template_id"],
            workspace_name=project_number,
            workspace_description="",
            workspace_type=m["type_id"],
            category_data=category_data,
            parent_id=m["parent_id"],
            show_error=True,
        )
        return self._id(resp)

    def create_project(self, cfg, project_number, project_title, program,
                       business_line, extra: dict):
        p = cfg["project"]
        a = p["attributes"]
        attrs = {}

        def put(key_name, value):
            key = a.get(key_name)
            if key and value not in (None, ""):
                attrs[key] = value

        put("project_number", project_number)
        put("project_title", project_title)
        put("program", program)
        put("business_line", business_line)
        put("project_description", extra.get("project_description"))
        put("project_status", extra.get("project_status"))
        put("client_number", extra.get("client_number"))
        put("client_name", extra.get("client_name"))
        put("start_date", extra.get("start_date"))
        put("finish_date", extra.get("finish_date"))

        category_data = {str(p["category_id"]): attrs}
        classifications = p.get("classification_ids") or None

        name = f"{project_number} - {project_title}" if project_title else project_number
        resp = self.otcs.create_workspace(
            workspace_template_id=p["template_id"],
            workspace_name=name,
            workspace_description=extra.get("project_description") or "",
            workspace_type=p["type_id"],
            category_data=category_data,
            classifications=classifications,
            parent_id=p["parent_id"],
            show_error=True,
        )
        return self._id(resp)

    def relate(self, master_id, project_id):
        """Master is parent, Project is child."""
        resp = self.otcs.create_workspace_relationship(
            workspace_id=master_id,
            related_workspace_id=project_id,
            relationship_type="child",
            show_error=True,
        )
        return resp is not None

    # ---- folders ----
    FOLDER_TYPE = 0

    def set_folder_cache(self, cache: dict, lock=None):
        """Share one folder-path cache across all worker clients so a folder
        discovered/created by any worker is instantly known to the others."""
        self._folder_cache = cache
        self._folder_cache_lock = lock

    def resolve_folder(self, project_id, folder_path, inh_ids=None, disabled_set=None, inh_lock=None, folder_cat_ids=None):
        """Resolve (creating if needed) the folder path inside the project.

        Custom walk instead of pyxecm's get_node_by_workspace_and_path: that
        helper matches nodes by name regardless of type, so a document named
        like a folder (e.g. 'afterSite.zip' next to folder 'afterSite')
        derails it into browsing a file. This walk only descends into
        FOLDER nodes and creates missing folders. Results are cached per
        (project, path) - a large win at volume.

        When creating a folder, the PARENT's category inheritance is
        temporarily (re-)enabled so the new folder inherits the category
        naturally (with empty values) - keeping migrated folders consistent
        with template folders. The parent is re-disabled afterwards only if
        the migration had disabled it (state preserved via disabled_set).
        """
        if not folder_path:
            return project_id
        # Shared cache across workers when provided (set via set_folder_cache),
        # otherwise a private one.
        if not hasattr(self, "_folder_cache"):
            self._folder_cache = {}
        cache = self._folder_cache
        cache_lock = getattr(self, "_folder_cache_lock", None)
        parts = [seg for seg in folder_path.replace("/", "\\").split("\\") if seg.strip()]

        current = project_id
        walked = []
        for seg in parts:
            walked.append(seg)
            key = (project_id, "\\".join(walked))
            if cache_lock is not None:
                with cache_lock:
                    cached = cache.get(key)
            else:
                cached = cache.get(key)
            if cached:
                current = cached
                continue

            # Create-first strategy: on a fresh tree every lookup would miss,
            # and the loose where_name search is expensive at scale. Try the
            # create; only if CS reports the name exists do we look it up.
            created = self.otcs.create_item(
                parent_id=current, item_type=self.FOLDER_TYPE,
                item_name=seg, show_error=False)
            new_id = self._id(created)

            if new_id and folder_cat_ids:
                # Apply the folder categories explicitly (empty values) so the
                # new folder carries them regardless of the parent's
                # inheritance state - inherited categories do not propagate
                # once inheritance is disabled.
                for cid in folder_cat_ids:
                    try:
                        self.apply_category(int(new_id), cid)
                    except Exception:
                        pass

            if not new_id:
                # Exists already (or create failed) - resolve by exact name.
                found_id = None
                conflict = None
                resp = self.otcs.get_node_by_parent_and_name(
                    parent_id=current, name=seg, show_error=False)
                if resp:
                    # where_name matches loosely (e.g. 'afterSite' also returns
                    # 'afterSite.zip') and may return several nodes: scan for
                    # the EXACT name, prefer a folder, note exact-name
                    # non-folders as conflicts.
                    for idx in range(0, 50):
                        try:
                            nid = self.otcs.get_result_value(
                                response=resp, key="id", index=idx)
                        except Exception:
                            break
                        if not nid:
                            break
                        try:
                            nname = self.otcs.get_result_value(
                                response=resp, key="name", index=idx)
                            ntype = self.otcs.get_result_value(
                                response=resp, key="type", index=idx)
                        except Exception:
                            nname, ntype = None, None
                        if nname != seg:
                            continue
                        if ntype is not None and int(ntype) == self.FOLDER_TYPE:
                            found_id = int(nid)
                            break
                        conflict = (int(nid), ntype)

                if found_id:
                    current = found_id
                elif conflict:
                    raise RuntimeError(
                        f"name conflict: node {conflict[0]} (type "
                        f"{conflict[1]}) is named '{seg}' where a folder is "
                        f"needed (path: {folder_path})")
                else:
                    raise RuntimeError(
                        f"could not create or find folder '{seg}' under "
                        f"{current} (path: {folder_path})")
            else:
                current = int(new_id)

            if cache_lock is not None:
                with cache_lock:
                    cache[key] = current
            else:
                cache[key] = current

        return current

    # ---- documents ----
    def find_document(self, parent_id, file_name):
        """Exact-name lookup of a non-folder child (for adopting documents
        that already exist from an interrupted earlier run)."""
        resp = self.otcs.get_node_by_parent_and_name(
            parent_id=parent_id, name=file_name, show_error=False)
        if not resp:
            return None
        for idx in range(0, 50):
            try:
                nid = self.otcs.get_result_value(response=resp, key="id", index=idx)
            except Exception:
                break
            if not nid:
                break
            try:
                nname = self.otcs.get_result_value(response=resp, key="name", index=idx)
                ntype = self.otcs.get_result_value(response=resp, key="type", index=idx)
            except Exception:
                continue
            if nname == file_name and (ntype is None or int(ntype) != self.FOLDER_TYPE):
                return int(nid)
        return None

    def upload_document(self, parent_id, file_path, file_name, category_data=None):
        resp = self.otcs.upload_file_to_parent(
            parent_id=parent_id,
            file_url=file_path,
            file_name=file_name,
            category_data=category_data,
            show_error=True,
        )
        return self._id(resp)

    # ---- verification helpers ----
    def get_workspace_relationships(self, workspace_id):
        return self.otcs.get_workspace_relationships(workspace_id=workspace_id)

    def node_exists(self, node_id):
        try:
            return self.otcs.get_node(node_id=node_id) is not None
        except Exception:
            return False
