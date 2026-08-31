# export_community_suggestions.py
"""Собрать принятые сообществом правки и закоммитить их отдельной веткой.

main не трогается никогда -- слияние community-suggestions в main смотрит и
делает человек, через пайплайн проекта-переводчика."""
import json
import os
import subprocess
import sys

BRANCH = "community-suggestions"
ACCEPTED_STATUSES = ("community_approved", "admin_approved")


def collect_accepted(con):
    rows = con.execute(
        "SELECT uid, text FROM suggestions WHERE status IN (?,?) "
        "ORDER BY id", ACCEPTED_STATUSES).fetchall()
    patch = {}
    for r in rows:
        comp, key = r["uid"].split("::", 1)
        patch.setdefault(comp, {})[key] = r["text"]
    return patch


def commit_branch(repo_dir, patch, branch_name=BRANCH):
    if not patch:
        return False

    starting_branch = subprocess.run(
        ["git", "-C", repo_dir, "branch", "--show-current"],
        capture_output=True, text=True, check=True).stdout.strip()

    exists = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--verify", branch_name],
        capture_output=True).returncode == 0
    if exists:
        subprocess.run(["git", "-C", repo_dir, "checkout", branch_name], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "main"], check=True,
                       capture_output=True)
    else:
        subprocess.run(["git", "-C", repo_dir, "checkout", "-b", branch_name], check=True,
                       capture_output=True)

    changed_files = []
    for comp, kv in patch.items():
        path = os.path.join(repo_dir, "rusifikator", "data", "%s.json" % comp)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.update(kv)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        changed_files.append(path)

    subprocess.run(["git", "-C", repo_dir, "add"] + changed_files, check=True)
    diff = subprocess.run(
        ["git", "-C", repo_dir, "diff", "--cached", "--quiet"])
    has_changes = diff.returncode != 0
    if has_changes:
        n = sum(len(kv) for kv in patch.values())
        subprocess.run(
            ["git", "-C", repo_dir, "commit", "-q", "-m",
             "Правки сообщества с сайта вычитки (%d строк)" % n], check=True)

    subprocess.run(
        ["git", "-C", repo_dir, "checkout", starting_branch or "main"], check=True,
        capture_output=True)
    return has_changes


def main():
    import zen_store
    base_dir = os.path.dirname(os.path.abspath(__file__))
    con = zen_store.connect(os.path.join(base_dir, "zen_review.db"))
    repo_dir = os.path.join(base_dir, "repos", "dead-reckoning")

    patch = collect_accepted(con)
    total = sum(len(kv) for kv in patch.values())
    print("к выгрузке: %d строк в %d файлах" % (total, len(patch)))
    changed = commit_branch(repo_dir, patch)
    if not changed:
        print("нечего коммитить (нет новых принятых правок с прошлой выгрузки)")
        return
    if "--push" in sys.argv:
        subprocess.run(
            ["git", "-C", repo_dir, "push", "-u", "origin", BRANCH], check=True)
        print("запушено в origin/%s" % BRANCH)
    else:
        print("закоммичено локально в %s. Для отправки на GitHub: --push" % BRANCH)


if __name__ == "__main__":
    sys.exit(main())
