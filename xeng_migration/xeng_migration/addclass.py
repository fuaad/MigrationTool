#!/usr/bin/env python3
"""addclass.py - find a call that ADDS one classification and changes nothing else.

Run against a THROWAWAY folder (create an empty folder anywhere you can
delete afterwards):

    py addclass.py --config config/env_Prod.yaml --node <test_folder_id>

For each candidate call it prints the node's full classification state before
and after - id, management_type, inherit_flag, parent_managed - so we can see
exactly what changed.  Stops at the first call that adds ONLY the closed
classification and leaves every other entry byte-identical.
"""

import json
import argparse

import yaml
import requests

requests.packages.urllib3.disable_warnings()  # noqa


def state(s, base, nid):
    """Full per-entry state, or None if unreadable."""
    for ver in ("v1", "v2"):
        r = s.get(f"{base}/{ver}/nodes/{nid}/classifications", timeout=60)
        if r.status_code == 200:
            try:
                out = {}
                for e in r.json().get("data", []) or []:
                    if isinstance(e, dict) and e.get("id"):
                        out[int(e["id"])] = {
                            "name": e.get("name"),
                            "management_type": e.get("management_type"),
                            "inherit_flag": e.get("inherit_flag"),
                            "parent_managed": e.get("parent_managed"),
                        }
                return out
            except Exception:
                return None
    return None


def show(label, st):
    print(f"    {label}")
    if st is None:
        print("      <unreadable>")
        return
    if not st:
        print("      (none)")
    for cid, v in sorted(st.items()):
        print(f"      {cid:<8} {str(v['name'])[:26]:<26} "
              f"mgmt={v['management_type']} inherit={v['inherit_flag']} "
              f"parent_managed={v['parent_managed']}")


def diff(before, after, want):
    """Verdict on whether only `want` was added."""
    if before is None or after is None:
        return "cannot verify (unreadable)"
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = [c for c in set(before) & set(after) if before[c] != after[c]]
    bits = []
    if added:
        bits.append(f"added {sorted(added)}")
    if removed:
        bits.append(f"REMOVED {sorted(removed)}")
    if changed:
        bits.append(f"ALTERED {sorted(changed)}")
    if not bits:
        return "no change"
    verdict = ", ".join(bits)
    if added == {want} and not removed and not changed:
        verdict += "   <== CLEAN ADD"
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/env_Prod.yaml")
    ap.add_argument("--node", type=int, required=True)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    c = cfg["connection"]
    port = str(c.get("port") or "")
    proto = c.get("protocol") or ("https" if port == "443" else "http")
    netloc = c["hostname"] if port in ("", "80", "443") \
        else f"{c['hostname']}:{port}"
    base = f"{proto}://{netloc}{c.get('base_path', '/otcs/cs.exe')}/api"
    want = int((cfg.get("closed_projects", {}) or {})
               .get("classification_ids", [50552])[0])

    r = requests.post(f"{base}/v1/auth",
                      data={"username": c["username"], "password": c["password"]},
                      verify=bool(c.get("verify_ssl", False)), timeout=60)
    r.raise_for_status()
    s = requests.Session()
    s.headers.update({"OTCSTicket": r.json()["ticket"]})
    s.verify = bool(c.get("verify_ssl", False))

    nid = args.node
    print(f"node {nid}, adding classification {want}\n")

    # What actions does the API itself advertise for a classification?
    for ver in ("v1", "v2"):
        ar = s.get(f"{base}/{ver}/nodes/{nid}/classifications/{want}/actions",
                   timeout=60)
        print(f"GET {ver} classifications/{want}/actions -> HTTP "
              f"{ar.status_code}  {ar.text[:260]}")
    print()

    candidates = [
        ("POST  v1 .../classifications/{cid}", "POST",
         f"{base}/v1/nodes/{nid}/classifications/{want}", None),
        ("PUT   v1 .../classifications/{cid}", "PUT",
         f"{base}/v1/nodes/{nid}/classifications/{want}", None),
        ("POST  v1 .../classifications/{cid} (body)", "POST",
         f"{base}/v1/nodes/{nid}/classifications/{want}",
         {"body": json.dumps({"inherit_flag": False})}),
        ("POST  v2 .../classifications/{cid}", "POST",
         f"{base}/v2/nodes/{nid}/classifications/{want}", None),
        ("POST  v1 list, single id, inherit_flag false", "POST",
         f"{base}/v1/nodes/{nid}/classifications",
         {"body": json.dumps({"class_id": [want], "inherit_flag": False,
                              "apply_to_sub_items": False})}),
    ]

    for label, method, url, data in candidates:
        before = state(s, base, nid)
        try:
            rr = s.request(method, url, data=data, timeout=300)
        except Exception as e:
            print(f"  {label}\n    EXCEPTION {e}\n")
            continue
        after = state(s, base, nid)
        print(f"  {label}  ->  HTTP {rr.status_code}  {rr.text[:120]}")
        show("before:", before)
        show("after: ", after)
        print(f"    VERDICT: {diff(before, after, want)}\n")
        if (before is not None and after is not None
                and set(after) - set(before) == {want}
                and not set(before) - set(after)
                and not [x for x in set(before) & set(after)
                         if before[x] != after[x]]):
            print(f"USE THIS ONE: {method} {url}"
                  + (f"  data={data}" if data else ""))
            return

    print("No candidate produced a clean add. Paste this output and I will "
          "work from the advertised actions instead.")


if __name__ == "__main__":
    main()
