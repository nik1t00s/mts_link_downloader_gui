from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .downloader import DownloaderError, download_record, ffmpeg_available, validate_record_url


class MtsLinkDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MTS Link Downloader")
        self.geometry("760x520")
        self.minsize(680, 460)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.selected_dir = tk.StringVar()
        self.url_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ожидание")
        self.progress_var = tk.DoubleVar(value=0)
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._poll_events()
        self._log("Приложение готово.")
        if not ffmpeg_available():
            self._log("ffmpeg не найден. Для некоторых .m3u8-записей он понадобится.")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        padding = {"padx": 16, "pady": 8}

        ttk.Label(self, text="Ссылка на запись").grid(row=0, column=0, sticky="w", **padding)
        url_frame = ttk.Frame(self)
        url_frame.grid(row=1, column=0, sticky="ew", padx=16)
        url_frame.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")

        folder_frame = ttk.Frame(self)
        folder_frame.grid(row=2, column=0, sticky="ew", **padding)
        folder_frame.columnconfigure(1, weight=1)
        self.choose_button = ttk.Button(folder_frame, text="Выбрать папку", command=self._choose_folder)
        self.choose_button.grid(row=0, column=0, sticky="w")
        self.folder_label = ttk.Label(folder_frame, textvariable=self.selected_dir, anchor="w")
        self.folder_label.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        action_frame = ttk.Frame(self)
        action_frame.grid(row=3, column=0, sticky="ew", **padding)
        action_frame.columnconfigure(1, weight=1)
        self.download_button = ttk.Button(action_frame, text="Скачать", command=self._start_download)
        self.download_button.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(action_frame, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)
        ttk.Label(action_frame, textvariable=self.status_var).grid(row=0, column=2, sticky="e")

        log_frame = ttk.Frame(self)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=16, pady=(4, 16))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку для сохранения")
        if folder:
            self.selected_dir.set(folder)
            self._log(f"Выбрана папка: {folder}")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.download_button.configure(state=state)
        self.choose_button.configure(state=state)
        self.url_entry.configure(state=state)

    def _start_download(self) -> None:
        url = self.url_var.get().strip()
        output_dir = self.selected_dir.get().strip()

        try:
            validate_record_url(url)
            if not output_dir:
                raise DownloaderError("Папка для сохранения не выбрана.")
        except DownloaderError as exc:
            self.status_var.set("Ошибка")
            messagebox.showerror("Ошибка", str(exc))
            self._log(str(exc))
            return

        self.progress_var.set(0)
        self.status_var.set("Скачивание")
        self._set_busy(True)
        self._log("Задача запущена.")

        self.worker = threading.Thread(
            target=self._download_worker,
            args=(url, output_dir),
            daemon=True,
        )
        self.worker.start()

    def _download_worker(self, url: str, output_dir: str) -> None:
        try:
            result = download_record(url, output_dir, self._progress_callback)
        except DownloaderError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            self.events.put(("error", f"Неожиданная ошибка: {exc}"))
        else:
            self.events.put(("done", result.file_paths))

    def _progress_callback(self, event: dict) -> None:
        self.events.put(("progress", event))

    def _poll_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "progress":
                    self._handle_progress(payload)  # type: ignore[arg-type]
                elif event_type == "done":
                    self.progress_var.set(100)
                    self.status_var.set("Завершено")
                    self._set_busy(False)
                    paths = payload if isinstance(payload, list) else [payload]
                    self._log("Скачивание завершено:")
                    for path in paths:
                        self._log(f"  {path}")
                    messagebox.showinfo("Готово", "Скачивание завершено")
                elif event_type == "error":
                    self.status_var.set("Ошибка")
                    self._set_busy(False)
                    self._log(str(payload))
                    messagebox.showerror("Ошибка", str(payload))
        except queue.Empty:
            pass
        self.after(150, self._poll_events)

    def _handle_progress(self, event: dict) -> None:
        if event.get("status") == "log":
            self._log(str(event.get("message", "")))
            return

        percent = event.get("percent")
        if percent is not None:
            self.progress_var.set(float(percent))

        status = event.get("status")
        if status == "downloading":
            parts = []
            if percent is not None:
                parts.append(f"{float(percent):.1f}%")
            if event.get("eta") is not None:
                parts.append(f"осталось {event['eta']} сек.")
            self.status_var.set("Скачивание")
            if parts:
                self._log("Прогресс: " + ", ".join(parts))
        elif status == "finished":
            self.status_var.set("Завершение")
            self._log("Файл загружен, выполняется финальная обработка...")

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    app = MtsLinkDownloaderApp()
    app.mainloop()
