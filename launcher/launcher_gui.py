# -*- coding: utf-8 -*-
"""GUI launcher for RussifierPatcher -- replaces the console window with a
minimal window: game path picker, one button, a log, and enough branding
(image + credits + Boosty link) that this doesn't read as an anonymous tool.

Stdlib only (tkinter, incl. its built-in PNG support via PhotoImage) so this
freezes into a single exe with PyInstaller exactly like the console tools do.
"""
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox

# Without this, Windows treats the window as DPI-unaware and upscales it as
# a bitmap on any scaled display -- that's what makes the text look blurry
# ("размазанный") instead of crisp. Must run before the Tk root is created.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patcher_common as C          # noqa: E402
import patcher_russify as RUSS      # noqa: E402

BOOSTY_URL = "https://boosty.to/coreforgelabs"
BG = "#12141a"
PANEL = "#1b1e27"
FG = "#e8e6e0"
MUTED = "#8b8f9c"
ACCENT = "#3d7bfd"
ACCENT_HOVER = "#5b90ff"
GOOD = "#57c26a"
BAD = "#e05a5a"
MONO = ("Consolas", 10)
CREDITS = (
    ("Шейх", "Сергей Коршунов"),
    ("Адмиралы", "Миша Аверин, Игорь Мирошниченко, Nevermid"),
    ("Капитаны", "Gundyar, Сергей Примаков, Zurics Game"),
    ("Юнга", "GreyViS, Pavel Beźik, LunarGoat, jard, languin, Анна Плагиатор"),
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dead Reckoning: The Long Drift — русская локализация")
        self.configure(bg=BG)
        self.resizable(False, False)
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        self.exe_path = None
        self.busy = False
        self.log_q = queue.Queue()

        self._build()
        self._auto_detect()
        self.after(80, self._drain_log)

    # --- layout ---------------------------------------------------------

    def _build(self):
        root = tk.Frame(self, bg=BG, padx=18, pady=16)
        root.pack()

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x")

        img_path = os.path.join(HERE, "assets", "astronaut_ru.png")
        self._photo = None
        if os.path.exists(img_path):
            try:
                self._photo = tk.PhotoImage(file=img_path).subsample(3, 3)
            except tk.TclError:
                self._photo = None
        if self._photo:
            tk.Label(header, image=self._photo, bg=BG).pack(side="left", padx=(0, 16))

        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left", fill="both", expand=True, anchor="n")
        tk.Label(title_box, text="Dead Reckoning: The Long Drift", bg=BG, fg=FG,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(title_box, text="Русская локализация", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(title_box,
                 text="Мод живёт благодаря вашей поддержке! Подписывайтесь на "
                      "Boosty — будет ещё много интересного) Также там можно "
                      "предложить свою игру или сообщить о баге.",
                 bg=BG, fg=FG, font=("Segoe UI", 11, "bold"), wraplength=440,
                 justify="left").pack(anchor="w", pady=(8, 2))
        link = tk.Label(title_box, text="❤ boosty.to/coreforgelabs", bg=BG, fg=ACCENT,
                         font=("Segoe UI", 12, "bold", "underline"), cursor="hand2")
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(BOOSTY_URL))

        # --- path picker ---
        path_box = tk.Frame(root, bg=BG, pady=14)
        path_box.pack(fill="x")
        tk.Label(path_box, text="Путь к игре", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        row = tk.Frame(path_box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        self.path_var = tk.StringVar(value="Не найдено — укажите вручную")
        self.path_entry = tk.Entry(row, textvariable=self.path_var, bg=PANEL, fg=FG,
                                    insertbackground=FG, relief="flat", font=MONO,
                                    width=54)
        self.path_entry.pack(side="left", ipady=6, fill="x", expand=True)
        self._btn(row, "Обзор…", self._browse).pack(side="left", padx=(8, 0))

        # --- action ---
        self.action_btn = self._btn(root, "Перевести игру на русский",
                                     self._start, big=True)
        self.action_btn.pack(fill="x", pady=(4, 12), ipady=8)

        # --- log ---
        log_frame = tk.Frame(root, bg=PANEL)
        log_frame.pack(fill="both")
        self.log_text = tk.Text(log_frame, height=10, width=64, bg=PANEL, fg=FG,
                                 insertbackground=FG, relief="flat", font=MONO,
                                 padx=10, pady=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both")
        self.log_text.tag_config("ok", foreground=GOOD)
        self.log_text.tag_config("err", foreground=BAD)

        # --- credits ---
        tk.Label(root, text="❤ Благодарности экипажу", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), pady=10).pack(anchor="w")
        credits_frame = tk.Frame(root, bg=BG)
        credits_frame.pack(fill="x", pady=(0, 4))
        for rank, names in CREDITS:
            line = tk.Frame(credits_frame, bg=BG)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=rank + ":", bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold"),
                     width=10, anchor="w").pack(side="left")
            tk.Label(line, text=names, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                     anchor="w", wraplength=420, justify="left").pack(side="left", fill="x")

    def _btn(self, parent, text, cmd, big=False):
        b = tk.Button(parent, text=text, command=cmd, bg=ACCENT, fg="white",
                      activebackground=ACCENT_HOVER, activeforeground="white",
                      relief="flat", font=("Segoe UI", 10 if not big else 11, "bold"),
                      bd=0, cursor="hand2", padx=14)
        b.bind("<Enter>", lambda e: b.config(bg=ACCENT_HOVER))
        b.bind("<Leave>", lambda e: b.config(bg=ACCENT))
        return b

    # --- behavior ---------------------------------------------------------

    def _auto_detect(self):
        found = C.find_game_exe_via_steam()
        if found:
            self.exe_path = found
            self.path_var.set(found)
            self._append("Найдено через Steam: %s\n" % found)

    def _browse(self):
        raw = filedialog.askopenfilename(
            title="Выберите dead_reckoning_windows.exe",
            filetypes=[("Исполняемый файл", "*.exe")])
        if not raw:
            raw = filedialog.askdirectory(title="Или укажите папку с игрой")
            if raw:
                found = C._find_exe_in_dir(raw)
                if not found:
                    messagebox.showerror("Не найдено", "В этой папке нет подходящего .exe игры.")
                    return
                raw = found
        if raw:
            self.exe_path = raw
            self.path_var.set(raw)

    def _append(self, text, tag=None):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text, tag or ())
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _start(self):
        if self.busy:
            return
        exe = self.path_var.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showwarning("Игра не выбрана", "Укажите .exe игры или папку с ней.")
            return
        self.exe_path = exe
        self.busy = True
        self.action_btn.config(state="disabled", text="Переводим…")
        threading.Thread(target=self._run_worker, args=(exe,), daemon=True).start()

    def _run_worker(self, exe):
        def log(msg):
            self.log_q.put(("info", msg))
        try:
            changed = RUSS.run_russify(exe, log=log)
            if changed:
                self.log_q.put(("ok", "\nГотово! Игра переведена на русский."))
            else:
                self.log_q.put(("ok", "\nЭта копия уже была переведена — ничего делать не пришлось."))
        except Exception as e:
            self.log_q.put(("err", "\nОшибка: %s" % e))
        finally:
            self.log_q.put(("__done__", ""))

    def _drain_log(self):
        try:
            while True:
                kind, text = self.log_q.get_nowait()
                if kind == "__done__":
                    self.busy = False
                    self.action_btn.config(state="normal", text="Перевести игру на русский")
                    continue
                self._append(text + "\n", kind if kind in ("ok", "err") else None)
        except queue.Empty:
            pass
        self.after(80, self._drain_log)


if __name__ == "__main__":
    C.setup_console()
    App().mainloop()
