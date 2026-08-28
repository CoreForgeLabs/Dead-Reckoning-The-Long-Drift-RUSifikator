# -*- coding: utf-8 -*-
"""RussifierPatcher -- standalone tool. Injects the Russian translation."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patcher_common as C          # noqa: E402
import patcher_translation as G     # noqa: E402
import patcher_gdc as GDC           # noqa: E402

EXTRA_PATH = "locale/strings_extra.ru.translation"
INVALID_UID = 0xFFFFFFFFFFFFFFFF


def data_path(name):
    return C.resource_path("data", name)


def run_russify(exe, log=print):
    """Core logic, usable from the console tool or the GUI launcher alike.
    Raises RuntimeError (in Russian) on any failure; never calls sys.exit or
    input() itself, so callers control how errors and prompts are surfaced."""
    C.backup(exe)

    log("Чтение %s ..." % exe)
    prefix, header, files = C.read_pck(exe)

    # Our own marker file. If it's already there, this exe already went
    # through RussifierPatcher (or was translated some other way) -- the
    # bundled patch data expects PRISTINE English source text, so a second
    # pass would either silently skip most entries or hard-fail partway
    # through (as it did once already: it went looking for the original
    # English colonist names and they were already Russian). Stop cleanly
    # instead of guessing.
    if any(f["path"] == EXTRA_PATH for f in files):
        log("Эта копия уже переведена (найден %s)." % EXTRA_PATH)
        log("Если нужно перевести заново -- возьмите чистую, непропатченную "
            "копию игры.")
        return False

    original = next((f for f in files if f["path"] == "locale/strings.ru.translation"), None)
    if original is None:
        raise RuntimeError("locale/strings.ru.translation не найден -- похоже, "
                            "в этой сборке игры нет слота под русскую локализацию.")

    orig_blob = original["data"]
    remap = json.load(open(data_path("remap.json"), encoding="utf-8"))
    main_blob, replaced, kept = G.rebuild_with_values(
        orig_blob, remap, "ru", G.read_uid(orig_blob))

    # rebuild_with_values keeps the original hash_table/bucket_table untouched
    # and only swaps stored strings, so every key the original table could
    # resolve is structurally guaranteed to still resolve -- no separate
    # regression scan needed here (unlike the dev-side build, which also
    # cross-checks against the full English key list to catch tooling bugs).
    log("Основная таблица: переведено %d, оставлено как есть %d" % (replaced, kept))

    extra_msgs = json.load(open(data_path("extra.json"), encoding="utf-8"))
    extra_blob = G.build_optimized_translation(extra_msgs, "ru", INVALID_UID)
    log("Дополнительная таблица: %d строк (%d байт)" % (len(extra_msgs), len(extra_blob)))

    replacements = {
        "locale/ru/labels.json": data_path("labels.json"),
        "locale/ru/narrative.json": data_path("narrative.json"),
        "locale/ru/epilogue.json": data_path("epilogue.json"),
    }
    for f in files:
        if f["path"] == "project.binary":
            entries = C.parse_project_binary(f["data"])
            if C.serialize_project_binary(entries) != f["data"]:
                raise RuntimeError("проверка формата project.binary не пройдена "
                                    "-- версия игры отличается от той, под которую "
                                    "собран этот инструмент.")
            paths = C.pb_decode_string_array(C.pb_get(entries, "locale/translations"))
            res = "res://" + EXTRA_PATH
            if res not in paths:
                paths.append(res)
            C.pb_set(entries, "locale/translations", C.pb_encode_string_array(paths))
            f["data"] = C.serialize_project_binary(entries)
            log("project.binary: зарегистрировано переводов: %d" % len(paths))
        elif f["path"] in replacements:
            with open(replacements[f["path"]], "rb") as rf:
                f["data"] = rf.read()

    n_gdc = GDC.apply_all(files, data_path("gdc_literal_fixes.json"))
    log("Патчей UI/имён в скомпилированных скриптах: %d" % n_gdc)

    original["data"] = main_blob
    added = False
    for f in files:
        if f["path"] == EXTRA_PATH:
            f["data"] = extra_blob
            added = True
    if not added:
        enc = EXTRA_PATH.encode("utf-8")
        enc += b"\x00" * ((4 - len(enc) % 4) % 4)
        files.append({"path": EXTRA_PATH, "raw_pbytes": enc, "flags": 0, "data": extra_blob})

    log("Запись переведённого файла...")
    C.write_pck(exe, prefix, header, files)
    log("Готово. Игра теперь на русском.")
    log("Оригинал сохранён как %s.backup" % exe)
    return True


def main():
    print("=" * 60)
    print("Dead Reckoning: The Long Drift -- русская локализация")
    print("=" * 60)
    print()

    exe = C.locate_game_exe()
    run_russify(exe, log=print)
    C.pause_on_exit()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        C.die("непредвиденная ошибка: %s" % e)
