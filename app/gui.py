from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .downloader import DownloaderError, download_record, ffmpeg_available, validate_record_url

_STATUS_TEXT = {
    "waiting": "Ожидание",
    "downloading": "Скачивание",
    "done": "Готово",
    "error": "Ошибка",
}
_STATUS_COLOR = {
    "waiting": "#666666",
    "downloading": "#1a5fb4",
    "done": "#26a269",
    "error": "#c01c28",
}


@dataclass
class _QueueItem:
    item_id: int
    url: str
    output_dir: str
    status: str = "waiting"
    error: str = ""
    file_paths: list[Path] = field(default_factory=list)
    frame: ttk.Frame | None = None
    url_label: ttk.Label | None = None
    status_label: ttk.Label | None = None
    progress_var: tk.DoubleVar | None = None
    progress_bar: ttk.Progressbar | None = None
    action_button: ttk.Button | None = None


class MtsLinkDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MTS Link Downloader")
        self.geometry("820x640")
        self.minsize(720, 520)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.selected_dir = tk.StringVar()
        self.url_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ожидание")

        self._queue: list[_QueueItem] = []
        self._next_item_id = 1
        self._queue_running = False
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._poll_events()
        self._log("Приложение готово.")
        if not ffmpeg_available():
            self._log("ffmpeg не найден. Для некоторых .m3u8-записей он понадобится.")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        padding = {"padx": 16, "pady": 8}

        ttk.Label(self, text="Ссылка на запись").grid(row=0, column=0, sticky="w", **padding)
        url_frame = ttk.Frame(self)
        url_frame.grid(row=1, column=0, sticky="ew", padx=16)
        url_frame.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _event: self._add_to_queue())

        folder_frame = ttk.Frame(self)
        folder_frame.grid(row=2, column=0, sticky="ew", **padding)
        folder_frame.columnconfigure(1, weight=1)
        self.choose_button = ttk.Button(folder_frame, text="Выбрать папку", command=self._choose_folder)
        self.choose_button.grid(row=0, column=0, sticky="w")
        self.folder_label = ttk.Label(folder_frame, textvariable=self.selected_dir, anchor="w")
        self.folder_label.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        add_frame = ttk.Frame(self)
        add_frame.grid(row=3, column=0, sticky="ew", **padding)
        self.add_button = ttk.Button(add_frame, text="+ В очередь", command=self._add_to_queue)
        self.add_button.grid(row=0, column=0, sticky="w")

        queue_header = ttk.Frame(self)
        queue_header.grid(row=4, column=0, sticky="ew", padx=16)
        queue_header.columnconfigure(0, weight=1)
        ttk.Label(queue_header, text="Очередь загрузок").grid(row=0, column=0, sticky="w")
        self.start_all_button = ttk.Button(queue_header, text="▶ Скачать всё", command=self._start_queue)
        self.start_all_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.clear_done_button = ttk.Button(queue_header, text="Очистить завершённые", command=self._clear_done)
        self.clear_done_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

        queue_container = ttk.Frame(self)
        queue_container.grid(row=5, column=0, sticky="nsew", padx=16, pady=(4, 8))
        self.rowconfigure(5, weight=1)
        queue_container.columnconfigure(0, weight=1)
        queue_container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(queue_container, highlightthickness=0, height=220)
        canvas.grid(row=0, column=0, sticky="nsew")
        queue_scrollbar = ttk.Scrollbar(queue_container, orient="vertical", command=canvas.yview)
        queue_scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=queue_scrollbar.set)

        self.queue_list_frame = ttk.Frame(canvas)
        self.queue_list_frame.columnconfigure(0, weight=1)
        self._queue_window = canvas.create_window((0, 0), window=self.queue_list_frame, anchor="nw")

        def _on_frame_configure(_event: object = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: object) -> None:
            canvas.itemconfigure(self._queue_window, width=event.width)  # type: ignore[attr-defined]

        self.queue_list_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        self.empty_queue_label = ttk.Label(self.queue_list_frame, text="Очередь пуста. Добавьте ссылку выше.", foreground="#888888")
        self.empty_queue_label.grid(row=0, column=0, sticky="w", pady=4)

        status_frame = ttk.Frame(self)
        status_frame.grid(row=7, column=0, sticky="ew", **padding)
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        log_frame = ttk.Frame(self)
        log_frame.grid(row=8, column=0, sticky="nsew", padx=16, pady=(4, 16))
        self.rowconfigure(8, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку для сохранения")
        if folder:
            self.selected_dir.set(folder)
            self._log(f"Выбрана папка: {folder}")

    # ---- queue management ----

    def _add_to_queue(self) -> None:
        url = self.url_var.get().strip()
        output_dir = self.selected_dir.get().strip()

        try:
            validate_record_url(url)
            if not output_dir:
                raise DownloaderError("Папка для сохранения не выбрана.")
        except DownloaderError as exc:
            messagebox.showerror("Ошибка", str(exc))
            self._log(str(exc))
            return

        item = _QueueItem(item_id=self._next_item_id, url=url, output_dir=output_dir)
        self._next_item_id += 1
        self._queue.append(item)
        self._build_queue_item_widget(item)
        self.url_var.set("")
        self._show_empty_if_needed()
        self._log(f"Добавлено в очередь: {url}")

    def _build_queue_item_widget(self, item: _QueueItem) -> None:
        row = ttk.Frame(self.queue_list_frame, relief="groove", borderwidth=1)
        row.grid(row=len(self.queue_list_frame.winfo_children()), column=0, sticky="ew", pady=4, padx=2)
        row.columnconfigure(0, weight=1)

        item.url_label = ttk.Label(row, text=item.url, anchor="w")
        item.url_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))

        progress_row = ttk.Frame(row)
        progress_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 6))
        progress_row.columnconfigure(0, weight=1)

        item.progress_var = tk.DoubleVar(value=0)
        item.progress_bar = ttk.Progressbar(progress_row, variable=item.progress_var, maximum=100)
        item.progress_bar.grid(row=0, column=0, sticky="ew")

        item.status_label = ttk.Label(progress_row, text=_STATUS_TEXT["waiting"], width=14, anchor="e")
        item.status_label.grid(row=0, column=1, padx=(8, 0))

        item.action_button = ttk.Button(progress_row, text="✕", width=3, command=lambda: self._remove_item(item))
        item.action_button.grid(row=0, column=2, padx=(8, 0))

        item.frame = row

    def _show_empty_if_needed(self) -> None:
        if self._queue:
            self.empty_queue_label.grid_remove()
        else:
            self.empty_queue_label.grid()

    def _remove_item(self, item: _QueueItem) -> None:
        if item.status == "downloading":
            return
        if item.frame is not None:
            item.frame.destroy()
        item.frame = None
        self._queue = [entry for entry in self._queue if entry.item_id != item.item_id]
        self._show_empty_if_needed()

    def _retry_item(self, item: _QueueItem) -> None:
        item.status = "waiting"
        item.error = ""
        self._update_item_ui(item)
        self._start_queue()

    def _clear_done(self) -> None:
        remaining = []
        for item in self._queue:
            if item.status == "done":
                if item.frame is not None:
                    item.frame.destroy()
                item.frame = None
                continue
            remaining.append(item)
        self._queue = remaining
        self._show_empty_if_needed()

    def _update_item_ui(self, item: _QueueItem, percent: float | None = None) -> None:
        if item.frame is None:
            return
        if item.status_label is not None:
            text = _STATUS_TEXT.get(item.status, item.status)
            if item.status == "error" and item.error:
                text = f"Ошибка: {item.error[:40]}"
            item.status_label.configure(text=text, foreground=_STATUS_COLOR.get(item.status, "#000000"))
        if percent is not None and item.progress_var is not None:
            item.progress_var.set(percent)
        if item.action_button is not None:
            if item.status == "error":
                item.action_button.configure(text="↺", command=lambda: self._retry_item(item))
            elif item.status == "downloading":
                item.action_button.configure(text="…", state="disabled")
            else:
                item.action_button.configure(text="✕", state="normal", command=lambda: self._remove_item(item))

    # ---- queue worker ----

    def _start_queue(self) -> None:
        if self._queue_running:
            return
        if not any(item.status == "waiting" for item in self._queue):
            self._log("В очереди нет задач для запуска.")
            return

        self._queue_running = True
        self.start_all_button.configure(state="disabled")
        self.worker = threading.Thread(target=self._queue_worker, daemon=True)
        self.worker.start()

    def _queue_worker(self) -> None:
        while True:
            item = next((entry for entry in self._queue if entry.status == "waiting"), None)
            if item is None:
                break

            self.events.put(("item_status", (item, "downloading", "")))
            try:
                result = download_record(
                    item.url,
                    item.output_dir,
                    lambda event, item=item: self.events.put(("item_progress", (item, event))),
                )
            except DownloaderError as exc:
                self.events.put(("item_status", (item, "error", str(exc))))
            except Exception as exc:
                self.events.put(("item_status", (item, "error", f"Неожиданная ошибка: {exc}")))
            else:
                item.file_paths = result.file_paths
                self.events.put(("item_status", (item, "done", "")))

        self.events.put(("queue_finished", None))

    def _progress_callback(self, event: dict) -> None:
        self.events.put(("progress", event))

    def _poll_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "item_status":
                    item, status, error = payload  # type: ignore[misc]
                    item.status = status
                    item.error = error
                    percent = 100.0 if status == "done" else (0.0 if status == "downloading" else None)
                    self._update_item_ui(item, percent)
                    if status == "downloading":
                        self._log(f"Скачивание начато: {item.url}")
                    elif status == "done":
                        self._log("Готово: " + item.url)
                        for path in item.file_paths:
                            self._log(f"  {path}")
                    elif status == "error":
                        self._log(f"Ошибка ({item.url}): {error}")
                elif event_type == "item_progress":
                    item, progress_event = payload  # type: ignore[misc]
                    self._handle_item_progress(item, progress_event)
                elif event_type == "queue_finished":
                    self._queue_running = False
                    self.start_all_button.configure(state="normal")
                    done_count = sum(1 for entry in self._queue if entry.status == "done")
                    error_count = sum(1 for entry in self._queue if entry.status == "error")
                    self.status_var.set(f"Очередь завершена: готово {done_count}, ошибок {error_count}")
                    self._log(f"Очередь завершена. Готово: {done_count}, ошибок: {error_count}.")
                    if error_count:
                        messagebox.showwarning(
                            "Очередь завершена",
                            f"Готово: {done_count}. С ошибками: {error_count}. Подробности — в логе.",
                        )
                    else:
                        messagebox.showinfo("Готово", "Все загрузки в очереди завершены.")
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _handle_item_progress(self, item: _QueueItem, event: dict) -> None:
        if event.get("status") == "log":
            self._log(str(event.get("message", "")))
            return

        percent = event.get("percent")
        if percent is not None:
            self._update_item_ui(item, float(percent))

        status = event.get("status")
        if status == "downloading":
            self.status_var.set(f"Скачивание: {item.url}")
        elif status == "finished":
            self._log(f"Файл загружен, выполняется финальная обработка... ({item.url})")

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    app = MtsLinkDownloaderApp()
    app.mainloop()
