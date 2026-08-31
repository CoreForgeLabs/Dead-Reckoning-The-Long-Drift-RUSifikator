# test_sync_baseline.py
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import sync_baseline


def _rmtree_windows_safe(path):
    """git marks .git/objects/* read-only; shutil.rmtree chokes on that on
    Windows. Clear the flag and retry once per failing path."""
    def on_error(func, p, exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=on_error)


class TestBuildBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo_data = os.path.join(self.tmp, "repo_data")
        self.src_en = os.path.join(self.tmp, "src_en")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.repo_data)
        os.makedirs(self.src_en)

        def write(d, name, obj):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

        write(self.repo_data, "narrative.json", {"k1": "русский текст"})
        write(self.src_en, "narrative.json", {"k1": "english text"})
        write(self.repo_data, "labels.json", {"k2": "метка"})
        write(self.src_en, "labels.json", {"k2": "label"})
        write(self.repo_data, "epilogue.json", {"k3": "эпилог"})
        write(self.src_en, "epilogue.json", {"k3": "epilogue"})
        write(self.repo_data, "extra.json", {"literal one": "буквально один"})
        write(self.repo_data, "remap_by_en.json", {"UI STRING": "СТРОКА UI"})
        write(self.repo_data, "gdc_literal_fixes.json", [
            {"file": "a.gdc", "en": "Hello", "ru": "Привет"},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_narrative_gets_real_english(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "narrative.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["k1"], "english text")
        with open(os.path.join(self.out, "ru", "narrative.json"), encoding="utf-8") as f:
            ru = json.load(f)
        self.assertEqual(ru["k1"], "русский текст")

    def test_extra_english_is_key(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "extra.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["literal one"], "literal one")

    def test_remap_english_is_key(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "remap_by_en.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["UI STRING"], "UI STRING")

    def test_gdc_literal_fixes_split_into_en_ru(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "gdc_literal_fixes.json"), encoding="utf-8") as f:
            en = json.load(f)
        with open(os.path.join(self.out, "ru", "gdc_literal_fixes.json"), encoding="utf-8") as f:
            ru = json.load(f)
        self.assertEqual(en["Hello"], "Hello")
        self.assertEqual(ru["Hello"], "Привет")

    def test_missing_src_en_key_falls_back_to_key(self):
        # к narrative.json добавлен ключ, которого нет в src_en
        with open(os.path.join(self.repo_data, "narrative.json"), "w", encoding="utf-8") as f:
            json.dump({"k1": "текст", "k_new": "новый текст"}, f, ensure_ascii=False)
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "narrative.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["k_new"], "k_new")

    def test_counts_returned(self):
        counts = sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        self.assertEqual(counts["narrative"], 1)
        self.assertEqual(counts["gdc_literal_fixes"], 1)


class TestPull(unittest.TestCase):
    """Воспроизводит реальную находку: клон стоит на локальной ветке
    master (без локальной main), а origin/main существует только в
    удалённых ссылках -- checkout('main') раньше падал в этом случае."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.upstream = os.path.join(self.tmp, "upstream")
        self.clone = os.path.join(self.tmp, "clone")
        os.makedirs(self.upstream)

        def run(*args, cwd):
            subprocess.run(["git", "-C", cwd] + list(args), check=True,
                            capture_output=True)

        run("init", "-q", "-b", "main", cwd=self.upstream)
        run("config", "user.email", "t@t", cwd=self.upstream)
        run("config", "user.name", "t", cwd=self.upstream)
        with open(os.path.join(self.upstream, "f.txt"), "w") as f:
            f.write("v1")
        run("add", ".", cwd=self.upstream)
        run("commit", "-q", "-m", "init", cwd=self.upstream)

        subprocess.run(
            ["git", "clone", "-q", "-b", "main", self.upstream, self.clone],
            check=True, capture_output=True)
        run("checkout", "-q", "-b", "master", cwd=self.clone)
        run("branch", "-q", "-D", "main", cwd=self.clone)

        with open(os.path.join(self.upstream, "f.txt"), "w") as f:
            f.write("v2")
        run("commit", "-q", "-am", "update", cwd=self.upstream)

    def tearDown(self):
        _rmtree_windows_safe(self.tmp)

    def test_pull_creates_local_main_when_missing(self):
        sync_baseline.pull(self.clone)
        branch = subprocess.run(
            ["git", "-C", self.clone, "branch", "--show-current"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(branch, "main")
        with open(os.path.join(self.clone, "f.txt")) as f:
            self.assertEqual(f.read(), "v2")


if __name__ == "__main__":
    unittest.main()
