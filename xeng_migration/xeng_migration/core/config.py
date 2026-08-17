"""config.py - Load YAML config and prompt for password if blank."""

from __future__ import annotations

import getpass
import yaml


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    conn = cfg.get("connection", {})
    if not conn.get("password"):
        conn["password"] = getpass.getpass(
            f"Password for {conn.get('username')}@{conn.get('hostname')}: ")
    return cfg
