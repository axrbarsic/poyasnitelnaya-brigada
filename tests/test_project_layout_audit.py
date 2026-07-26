from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import project_layout_audit


class ProjectLayoutAuditTests(unittest.TestCase):
    def make_root(self, base: Path) -> Path:
        root = base / "project"
        root.mkdir()
        for relative in project_layout_audit.REQUIRED_PATHS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test\n", encoding="utf-8")
        (root / "skill-backup/x-twitter-operator/SKILL.md").write_text(
            "skill\n",
            encoding="utf-8",
        )
        return root

    def write_config(self, root: Path, browser_owner_cwd: str) -> Path:
        path = root / "config.json"
        path.write_text(
            json.dumps({"browser_owner_cwd": browser_owner_cwd}),
            encoding="utf-8",
        )
        return path

    def test_accepts_canonical_root_and_matching_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = self.make_root(base)
            config = self.write_config(root, ".")
            installed = base / "installed-skill"
            installed.mkdir()
            (installed / "SKILL.md").write_text("skill\n", encoding="utf-8")

            result = project_layout_audit.audit_layout(
                root=root,
                config_path=config,
                installed_skill=installed,
                require_installed_skill=True,
            )

            self.assertTrue(result["complete"])
            self.assertTrue(result["browser_owner_cwd_ok"])
            self.assertTrue(result["installed_skill_matches"])

    def test_rejects_external_browser_workspace_and_skill_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = self.make_root(base)
            config = self.write_config(root, str(base / "other-workspace"))
            installed = base / "installed-skill"
            installed.mkdir()
            (installed / "SKILL.md").write_text("different\n", encoding="utf-8")

            result = project_layout_audit.audit_layout(
                root=root,
                config_path=config,
                installed_skill=installed,
                require_installed_skill=True,
            )

            self.assertFalse(result["complete"])
            self.assertFalse(result["browser_owner_cwd_ok"])
            self.assertFalse(result["installed_skill_matches"])
            self.assertIn(
                "browser_owner_cwd_outside_canonical_root",
                result["errors"],
            )
            self.assertIn(
                "installed_skill_differs_from_repository_backup",
                result["errors"],
            )
