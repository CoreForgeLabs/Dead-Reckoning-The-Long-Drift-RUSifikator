# test_export_community_suggestions.py
import sqlite3
import subprocess
import tempfile
import shutil
import os
import json
import unittest

import zen_store
import export_community_suggestions as ecs


class TestCollectAccepted(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_only_accepted_statuses_included(self):
        sid1 = zen_store.add_suggestion(self.con, "narrative::x", "t1", "принято", "")
        zen_store.set_suggestion_status(self.con, sid1, "admin_approved", "admin", "admin_approved")
        sid2 = zen_store.add_suggestion(self.con, "narrative::y", "t1", "открыто", "")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch["narrative"], {"x": "принято"})

    def test_component_split_by_uid_prefix(self):
        sid = zen_store.add_suggestion(self.con, "labels::z", "t1", "метка", "")
        zen_store.set_suggestion_status(self.con, sid, "community_approved", "t2", "community_approved")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch, {"labels": {"z": "метка"}})

    def test_empty_when_nothing_accepted(self):
        zen_store.add_suggestion(self.con, "narrative::x", "t1", "открыто", "")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch, {})


class TestCommitBranch(unittest.TestCase):
    def setUp(self):
        self.repo_dir = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.repo_dir], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "branch", "-m", "main"], check=True)
        os.makedirs(os.path.join(self.repo_dir, "rusifikator", "data"))
        with open(os.path.join(self.repo_dir, "rusifikator", "data", "narrative.json"), "w", encoding="utf-8") as f:
            json.dump({"x": "старый текст"}, f, ensure_ascii=False, indent=1)
        subprocess.run(["git", "-C", self.repo_dir, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "commit", "-q", "-m", "init"], check=True)

    def tearDown(self):
        shutil.rmtree(self.repo_dir, onerror=self._on_rm_error)

    @staticmethod
    def _on_rm_error(func, path, exc_info):
        import stat
        os.chmod(path, stat.S_IWRITE)
        func(path)

    def test_commit_creates_branch_with_patched_file(self):
        patch = {"narrative": {"x": "новый текст от сообщества"}}
        changed = ecs.commit_branch(self.repo_dir, patch, "community-suggestions")
        self.assertTrue(changed)

        show = subprocess.run(
            ["git", "-C", self.repo_dir, "show",
             "community-suggestions:rusifikator/data/narrative.json"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertEqual(json.loads(show), {"x": "новый текст от сообщества"})

        branch = subprocess.run(
            ["git", "-C", self.repo_dir, "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(branch, "main")  # commit_branch не должен оставлять нас на ветке

    def test_commit_returns_false_when_nothing_changed(self):
        patch = {"narrative": {"x": "старый текст"}}
        changed = ecs.commit_branch(self.repo_dir, patch, "community-suggestions")
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
