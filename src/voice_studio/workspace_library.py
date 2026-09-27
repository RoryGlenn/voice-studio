"""Discover sibling audiobook jobs without changing their worker state."""

import json
import threading
from urllib.parse import quote

from voice_studio.audiobook_workspace import Workspace


class Library:
    def __init__(self, primary):
        self.primary = primary
        self.root = primary.job.parent.resolve()
        self.workspaces = {primary.job.name: primary}
        self.lock = threading.Lock()

    def jobs(self):
        return {
            p.parent.name: p.parent
            for p in self.root.glob("*/plan.json")
            if p.parent.resolve().parent == self.root
            and not p.parent.is_symlink()
            and (p.parent / "status.json").is_file()
        }

    def get(self, key):
        with self.lock:
            if key in self.workspaces:
                return self.workspaces[key]
            job = self.jobs().get(key)
            if job is None:
                raise ValueError("Unknown book job")
            # Only explicitly registered jobs may use service controls/output paths.
            config_path = job / "dashboard-config.json"
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
            from pathlib import Path

            workspace = Workspace(
                job,
                Path(config.get("output", job / "exports")),
                self.primary.progress_reader,
                self.primary.monitor,
                config.get("service"),
            )
            workspace.controls_enabled = bool(config)
            workspace.refresh()
            self.workspaces[key] = workspace
            threading.Thread(target=workspace.run, daemon=True).start()
            return workspace

    def listing(self):
        rows = []
        for key, job in sorted(self.jobs().items()):
            try:
                plan = json.loads((job / "plan.json").read_text())
                data = self.primary.progress_reader(job, job / "exports")
                if (job / "paused").exists() and data["state"] == "stopped":
                    data["state"] = "paused"
                rows.append(
                    dict(
                        id=key,
                        title=plan["title"],
                        author=plan.get("author", ""),
                        state=data["state"],
                        generated=data["generated"],
                        total=data["total"],
                        checked=data["checked"],
                        held=data["held"],
                        url="/jobs/" + quote(key, safe="") + "/",
                    )
                )
            except (OSError, ValueError, KeyError, TypeError):
                rows.append(dict(id=key, title=key, state="unavailable", url=None))
        return rows
