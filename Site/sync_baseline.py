# sync_baseline.py
"""Снимок main из GitHub -> data/source_baseline/{en,ru}/*.json.

repos/dead-reckoning -- локальный клон CoreForgeLabs/Dead-Reckoning-The-Long-
Drift-RUSifikator. Английский текст для narrative/labels/epilogue берётся
отдельно из соседнего проекта-переводчика (публичная поставка содержит
только русский)."""
import json
import os
import subprocess
import sys

REMOTE_URL = "https://github.com/CoreForgeLabs/Dead-Reckoning-The-Long-Drift-RUSifikator.git"
SRC_EN_ENRICHED = ("narrative", "labels", "epilogue")   # берут EN из src_en/
SRC_EN_SELF_KEYED = ("extra", "remap_by_en")             # EN == ключ


def ensure_remote(repo_dir, remote_url=REMOTE_URL):
    result = subprocess.run(
        ["git", "-C", repo_dir, "remote", "get-url", "origin"],
        capture_output=True, text=True)
    if result.returncode != 0:
        subprocess.run(
            ["git", "-C", repo_dir, "remote", "add", "origin", remote_url],
            check=True)


def pull(repo_dir):
    subprocess.run(["git", "-C", repo_dir, "fetch", "origin", "main"], check=True)
    has_local_main = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--verify", "main"],
        capture_output=True).returncode == 0
    if has_local_main:
        subprocess.run(
            ["git", "-C", repo_dir, "checkout", "main"], check=True,
            capture_output=True)
    else:
        subprocess.run(
            ["git", "-C", repo_dir, "checkout", "-b", "main", "origin/main"],
            check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repo_dir, "reset", "--hard", "origin/main"], check=True)
    sha = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    return sha


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def build_baseline(repo_data_dir, src_en_dir, out_dir):
    counts = {}

    for name in SRC_EN_ENRICHED:
        ru = _load(os.path.join(repo_data_dir, "%s.json" % name))
        src_en_path = os.path.join(src_en_dir, "%s.json" % name)
        en_source = _load(src_en_path) if os.path.exists(src_en_path) else {}
        en = {k: en_source.get(k, k) for k in ru}
        _dump(os.path.join(out_dir, "en", "%s.json" % name), en)
        _dump(os.path.join(out_dir, "ru", "%s.json" % name), ru)
        counts[name] = len(ru)

    for name in SRC_EN_SELF_KEYED:
        ru = _load(os.path.join(repo_data_dir, "%s.json" % name))
        en = {k: k for k in ru}
        _dump(os.path.join(out_dir, "en", "%s.json" % name), en)
        _dump(os.path.join(out_dir, "ru", "%s.json" % name), ru)
        counts[name] = len(ru)

    gdc_path = os.path.join(repo_data_dir, "gdc_literal_fixes.json")
    if os.path.exists(gdc_path):
        items = _load(gdc_path)
        gdc_en = {it["en"]: it["en"] for it in items}
        gdc_ru = {it["en"]: it["ru"] for it in items}
        _dump(os.path.join(out_dir, "en", "gdc_literal_fixes.json"), gdc_en)
        _dump(os.path.join(out_dir, "ru", "gdc_literal_fixes.json"), gdc_ru)
        counts["gdc_literal_fixes"] = len(items)

    return counts


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.join(base_dir, "repos", "dead-reckoning")
    repo_data_dir = os.path.join(repo_dir, "rusifikator", "data")
    src_en_dir = r"F:\DEV2\research_game\Dead Reckoning Russifier\src_en"
    out_dir = os.path.join(base_dir, "data", "source_baseline")

    ensure_remote(repo_dir)
    sha = pull(repo_dir)
    counts = build_baseline(repo_data_dir, src_en_dir, out_dir)
    print("main @ %s -> %s" % (sha, counts))


if __name__ == "__main__":
    sys.exit(main())
