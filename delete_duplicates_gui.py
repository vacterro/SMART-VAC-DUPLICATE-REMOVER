import os
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import queue
import subprocess
import platform
import re

VERSION = "0.0.1"

LOG_FILE = "deleted_log.txt"

def hash_file(path, block_size=65536):
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(block_size):
            hasher.update(chunk)
    return hasher.hexdigest()

class ToolTip:
    def __init__(self, widget, text=''):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _):
        if self.tip_window or not self.text:
            return
        x, y, *_ = self.widget.winfo_pointerxy()
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{x + 10}+{y + 10}")
        label = tk.Label(
            tw,
            text=self.text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            justify="left",
            font=("Segoe UI", 9),
            wraplength=600
        )
        label.pack(ipadx=6, ipady=2)

    def hide(self, _):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

def shorten_path(path, maxlen=90):
    if len(path) <= maxlen:
        return path
    else:
        return path[:35] + " ... " + path[-48:]

def is_likely_original(filename):
    """Оценивает, является ли файл оригиналом по имени и пути."""
    score = 0
    copy_indicators = [r'\bcopy\b', r'\bcopia\b', r'_bak\b', r'\(\d+\)', r'\bduplicate\b', r'\d+$']
    for pattern in copy_indicators:
        if re.search(pattern, filename.lower()):
            score -= 10
    score -= len(filename) // 20
    return score

class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("💣 Уничтожитель Дубликатов v5.5 HyperThreaded")
        self.queue = queue.Queue()
        self.worker_thread = None
        self.check_vars = {}
        self.item_path_map = {}
        self._build_ui()

    def _build_ui(self):
        main_frame = tk.Frame(self.root)
        main_frame.pack(padx=10, pady=10, fill="both", expand=True)

        path_label = tk.Label(main_frame, text="Выбери папку для поиска дубликатов:")
        path_label.pack(anchor="w")
        self.path_var = tk.StringVar()
        path_entry = tk.Entry(main_frame, textvariable=self.path_var, width=80)
        path_entry.pack(fill="x", pady=3)
        browse_btn = tk.Button(
            main_frame,
            text="🔍 Обзор",
            command=self.browse_folder,
            bg="#4CAF50",
            fg="white",
            activebackground="#45a049"
        )
        browse_btn.pack(pady=3)

        self.log_var = tk.BooleanVar(value=True)
        check_log = tk.Checkbutton(main_frame, text="Вести лог удалений", variable=self.log_var)
        check_log.pack(anchor="w", pady=5)
        ToolTip(check_log, f"Все удалённые файлы будут записаны в {LOG_FILE}")

        button_frame = tk.Frame(main_frame)
        button_frame.pack(fill="x", pady=5)
        find_btn = tk.Button(
            button_frame,
            text="🚀 Найти дубликаты",
            command=self.start_finding_duplicates,
            bg="#2196F3",
            fg="white",
            activebackground="#1e88e5",
            width=20
        )
        find_btn.pack(pady=2)
        
        import webbrowser
        empty_btn = tk.Button(
            button_frame,
            text="🗑 Удалить пустые папки",
            command=self.delete_empty_folders,
            bg="#FF9800",
            fg="white",
            activebackground="#f57c00",
        )
        empty_btn.pack(side="left", padx=(5, 0))
        bmac_btn = tk.Button(
            button_frame,
            text="🤍 Support developer",
            command=lambda: webbrowser.open("https://buymeacoffee.com/vacuum34"),
            bg="#444444",
            fg="white",
            activebackground="#F3C300",
        )
        bmac_btn.pack(side="left", padx=(5, 0))

        self.status_var = tk.StringVar()
        status_label = tk.Label(main_frame, textvariable=self.status_var, anchor="w")
        status_label.pack(fill="x")

        tree_frame = tk.Frame(main_frame)
        tree_frame.pack(fill="both", expand=True, pady=5)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("Select", "FilePath"),
            show="headings",
            selectmode="browse",
            height=20
        )
        self.tree.heading("Select", text="Удалить")
        self.tree.heading("FilePath", text="Файл")
        self.tree.column("Select", width=60, anchor="center")
        self.tree.column("FilePath", width=650, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar_y.set)
        scrollbar_y.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.open_file_path)
        self.tree.bind("<Motion>", self.show_tooltip_on_long_path)

        delete_btn = tk.Button(
            main_frame,
            text="💥 Удалить выбранные файлы",
            command=self.delete_selected,
            bg="#f44336",
            fg="white",
            activebackground="#d32f2f",
            width=20
        )
        delete_btn.pack(pady=5)

        self.tree_tip = None
        self.root.after(100, self.process_queue)

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.path_var.set(folder)

    def start_finding_duplicates(self):
        self.tree.delete(*self.tree.get_children())
        self.check_vars.clear()
        self.item_path_map.clear()
        self.status_var.set("⏳ Поиск дубликатов...")
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Идёт работа", "Поиск уже выполняется!")
            return
        self.worker_thread = threading.Thread(target=self.find_duplicates_thread)
        self.worker_thread.start()

    def find_duplicates_thread(self):
        root_dir = self.path_var.get()
        if not os.path.isdir(root_dir):
            self.queue.put(("error", "Укажи корректную папку!"))
            return

        hash_map = {}
        for dirpath, _, filenames in os.walk(root_dir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                self.queue.put(("status", f"Читаю: {os.path.basename(path)}"))
                try:
                    size = os.path.getsize(path)
                    file_hash = hash_file(path)
                    key = (file_hash, size)
                    hash_map.setdefault(key, []).append(path)
                except Exception:
                    pass

        for key, files in hash_map.items():
            if len(files) < 2:
                continue
            files.sort(key=lambda f: is_likely_original(f), reverse=True)
            original = files[0]
            duplicates = files[1:]
            self.queue.put(("add_result", original, True))
            for dup in duplicates:
                self.queue.put(("add_result", dup, False))
        self.queue.put(("status", f"🔎 Завершено: обработано групп дубликатов: {sum(1 for k, v in hash_map.items() if len(v) > 1)}"))

    def process_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg[0] == "status":
                    self.status_var.set(msg[1])
                elif msg[0] == "add_result":
                    path, is_original = msg[1], msg[2]
                    var = tk.BooleanVar(value=not is_original)
                    self.check_vars[path] = var
                    display_text = f"{'📜 Оригинал: ' if is_original else '📌 Дубликат: '}{shorten_path(path)}"
                    item_id = self.tree.insert("", "end", values=("☑" if var.get() else "☐", display_text))
                    self.item_path_map[item_id] = path
                elif msg[0] == "error":
                    messagebox.showerror("Ошибка", msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if col == "#1":
            path = self.item_path_map.get(item)
            if path in self.check_vars:
                var = self.check_vars[path]
                var.set(not var.get())
                self.tree.set(item, "Select", "☑" if var.get() else "☐")

    def show_tooltip_on_long_path(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            path = self.item_path_map.get(item)
            if not path:
                return
            displayed_text = self.tree.item(item)["values"][1]
            if "..." in displayed_text:
                if self.tree_tip:
                    self.tree_tip.hide(None)
                self.tree_tip = ToolTip(self.tree, path)
                self.tree_tip.show(None)
            elif self.tree_tip:
                self.tree_tip.hide(None)

    def open_file_path(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            path = self.item_path_map.get(item)
            if not os.path.exists(path):
                messagebox.showerror("Ошибка", f"Путь не существует: {path}")
                return
            folder = os.path.dirname(path)
            try:
                if platform.system() == "Windows":
                    subprocess.run(["explorer", "/select,", os.path.normpath(path)])
                elif platform.system() == "Darwin":
                    subprocess.run(["open", "-R", path])
                else:
                    subprocess.run(["xdg-open", folder])
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось открыть путь: {e}")

    def delete_empty_folders(self):
        root_dir = self.path_var.get()
        if not os.path.isdir(root_dir):
            messagebox.showerror("Ошибка", "Укажи корректную папку!")
            return
        removed = 0
        while True:
            empty_found = False
            for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
                if not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                        removed += 1
                        empty_found = True
                        if self.log_var.get():
                            with open(LOG_FILE, "a", encoding="utf-8") as log:
                                log.write(f"Пустая папка удалена: {dirpath}\n")
                    except Exception:
                        pass
            if not empty_found:
                break
        messagebox.showinfo("Готово", f"Удалено пустых папок: {removed}")

    def delete_selected(self):
        selected = [p for p, v in self.check_vars.items() if v.get()]
        if not selected:
            messagebox.showwarning("Ничего не выбрано", "Выбери файлы для удаления.")
            return

        confirm = messagebox.askyesno("Подтвердить", f"Удалить {len(selected)} выбранных файлов?")
        if not confirm:
            return

        removed = 0
        if self.log_var.get():
            with open(LOG_FILE, "a", encoding="utf-8") as log:
                for path in selected:
                    try:
                        os.remove(path)
                        for item in self.tree.get_children():
                            if self.item_path_map.get(item) == path:
                                self.tree.delete(item)
                                break
                        log.write(f"{path}\n")
                        removed += 1
                    except Exception:
                        pass
        else:
            for path in selected:
                try:
                    os.remove(path)
                    for item in self.tree.get_children():
                        if self.item_path_map.get(item) == path:
                            self.tree.delete(item)
                            break
                    removed += 1
                except Exception:
                    pass

        self.status_var.set(f"✅ Удалено файлов: {removed}")

if __name__ == "__main__":
    root = tk.Tk()
    app = DuplicateFinderApp(root)
    root.mainloop()