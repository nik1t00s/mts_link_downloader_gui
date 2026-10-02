"""Local Tk/ttk theme without extra dependencies."""
import tkinter as tk
from tkinter import ttk

def apply_theme(root):
    root.configure(bg="#101722")
    root.option_add("*Font", "{Segoe UI} 10")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background="#101722", foreground="#e7edf7", font=("Segoe UI", 10))
    style.configure("TFrame", background="#101722")
    style.configure("TLabel", background="#101722", foreground="#a7bbd1", padding=3)
    style.configure("TButton", background="#23384e", foreground="#e7edf7", padding=(14, 9), borderwidth=0)
    style.map("TButton", background=[("active", "#34516b"), ("disabled", "#1a2838")], foreground=[("disabled", "#7a8ba0")])
    style.configure("TEntry", fieldbackground="#182536", foreground="#e7edf7", padding=9, insertcolor="#e7edf7")
    style.configure("Horizontal.TProgressbar", troughcolor="#23384e", background="#65d6bb", borderwidth=0)
    def walk(widget):
        if isinstance(widget, (tk.Frame, tk.LabelFrame)) and not isinstance(widget, ttk.Widget):
            widget.configure(bg="#101722")
        elif isinstance(widget, tk.Label):
            widget.configure(bg="#101722", fg="#a7bbd1")
        elif isinstance(widget, tk.Text):
            widget.configure(bg="#182536", fg="#e7edf7", insertbackground="#e7edf7", relief="flat", padx=14, pady=12)
        elif isinstance(widget, tk.Button):
            widget.configure(bg="#23384e", fg="#e7edf7", activebackground="#34516b", activeforeground="#ffffff", relief="flat", borderwidth=0, padx=12, pady=9)
        for child in widget.winfo_children():
            walk(child)
    walk(root)
