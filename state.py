"""Shared runtime state for the companion (live status, profile override)."""

import json
from pathlib import Path

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class State:
    """In-memory state shared by every component."""

    def __init__(self, cfg=None):
        self.mode = (cfg or load_config()).get("profiles", {}).get("mode", "casual")

    @property
    def profile_name(self):
        return self.mode

    def system_prompt(self, cfg):
        profiles = cfg.get("profiles", {})
        prof = profiles.get(self.profile_name, {})
        return prof.get("systemPrompt") or cfg.get("ai", {}).get("systemPrompt", "")
