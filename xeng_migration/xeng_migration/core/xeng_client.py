"""xeng_client.py - Controlled Revision Tracking via the Engineering REST API.

Uses the /api/v2/xengcrt/* endpoints (confirmed in the Engineering REST API
v2 spec) to control documents and set their revision status. Auth is the same
OTCSTicket used by pyxecm; we reuse the ticket from the authenticated OTCS
session so there is a single login.

Endpoints used:
  GET  /v2/xengcrt/loadsheet/revisionstatus   -> resolve stage name to rev_status_id
  GET  /v2/xengcrt/nodes/{id}/iscontrolleddocument
  POST /v2/xengcrt/nodes/revisions/validate
  POST /v2/xengcrt/revisions                  -> perform the revision action
  GET  /v2/xengcrt/nodes/{id}/revisions       -> read revisions (verify)
"""

from __future__ import annotations

import requests


class XengClient:
    def __init__(self, cfg: dict, otcs_ticket: str):
        self.base = cfg["engineering_api"]["base_url"].rstrip("/")
        self.verify_ssl = cfg["connection"].get("verify_ssl", True)
        self.headers = {"OTCSTicket": otcs_ticket}
        self._status_cache = {}  # stage name -> rev_status_id
        # optional overrides from config
        self.overrides = (cfg.get("revisions", {}) or {}).get("stage_status_map", {}) or {}

    # ---------- status resolution ----------
    def resolve_status_id(self, stage_name, action, document_id=None):
        if stage_name is None:
            return None
        if stage_name in self.overrides:
            return int(self.overrides[stage_name])
        if stage_name in self._status_cache:
            return self._status_cache[stage_name]

        url = f"{self.base}/v2/xengcrt/loadsheet/revisionstatus"
        params = {"name": stage_name, "action": action}
        if document_id:
            params["documentid"] = document_id
        r = requests.get(url, headers=self.headers, params=params,
                         verify=self.verify_ssl, timeout=60)
        r.raise_for_status()
        data = r.json()
        status_id = self._extract_status_id(data, stage_name)
        if status_id is not None:
            self._status_cache[stage_name] = status_id
        return status_id

    @staticmethod
    def _extract_status_id(data, stage_name):
        # Response shape varies; search common containers for a matching name->id.
        def walk(obj):
            if isinstance(obj, dict):
                # direct id/name pair
                name = obj.get("name") or obj.get("status_name") or obj.get("revision_status_name")
                sid = obj.get("id") or obj.get("rev_status_id") or obj.get("status_id")
                if name and sid and str(name).strip().lower() == stage_name.strip().lower():
                    return int(sid)
                for v in obj.values():
                    res = walk(v)
                    if res is not None:
                        return res
            elif isinstance(obj, list):
                for v in obj:
                    res = walk(v)
                    if res is not None:
                        return res
            return None
        return walk(data)

    # ---------- checks ----------
    def is_controlled(self, node_id):
        url = f"{self.base}/v2/xengcrt/nodes/{node_id}/iscontrolleddocument"
        r = requests.get(url, headers=self.headers, verify=self.verify_ssl, timeout=60)
        if r.status_code != 200:
            return False
        data = r.json()
        # Try to find a boolean-ish result
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if "control" in k.lower() and isinstance(v, bool):
                        return v
                    res = walk(v)
                    if res is not None:
                        return res
            elif isinstance(o, list):
                for v in o:
                    res = walk(v)
                    if res is not None:
                        return res
            return None
        val = walk(data)
        return bool(val)

    def validate(self, node_id, action, rev_status_id=None, project_id=None):
        url = f"{self.base}/v2/xengcrt/nodes/revisions/validate"
        body = {"ids": str(node_id), "action": action}
        if rev_status_id is not None:
            body["rev_status_id"] = rev_status_id
        if project_id is not None:
            body["project_id"] = project_id
        r = requests.post(url, headers=self.headers, data=body,
                          verify=self.verify_ssl, timeout=60)
        return r.status_code == 200, (r.text if r.status_code != 200 else "")

    # ---------- action ----------
    def perform_revision(self, node_id, action, rev_status_id=None,
                         master_workspace_id=None, project_id=None):
        url = f"{self.base}/v2/xengcrt/revisions"
        body = {"node_id": node_id, "action": action}
        if rev_status_id is not None:
            body["rev_status_id"] = rev_status_id
        if master_workspace_id is not None:
            body["master_workspace_id"] = master_workspace_id
        if project_id is not None:
            body["project_id"] = project_id
        r = requests.post(url, headers=self.headers, data=body,
                          verify=self.verify_ssl, timeout=120)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"revision action failed ({r.status_code}): {r.text}")
        return r.json() if r.text else {}

    def get_revisions(self, node_id, revision_type="all"):
        url = f"{self.base}/v2/xengcrt/nodes/{node_id}/revisions"
        r = requests.get(url, headers=self.headers,
                         params={"revision_type": revision_type},
                         verify=self.verify_ssl, timeout=60)
        if r.status_code != 200:
            return None
        return r.json()
