#!/usr/bin/env python3
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None


PROFILE_DIR = Path.home() / ".mozilla/firefox/wl9bf19c.default-release"
STORAGE_DIR = PROFILE_DIR / "storage"

TRACKER_HINTS = (
    "doubleclick",
    "googlesyndication",
    "googletagmanager",
    "googleadservices",
    "googleads",
    "pubmatic",
    "rubiconproject",
    "smartadserver",
    "ad-score",
    "adscale",
    "adnxs",
    "taboola",
    "outbrain",
    "criteo",
    "analytics",
    "optimizely",
    "intellimize",
    "connatix",
    "aniview",
    "media.net",
    "safeframe",
    "cookiebot",
    "trustarc",
    "consent",
)

SENSITIVE_HINTS = (
    "accounts.google.com",
    "accounts.youtube.com",
    "auth.",
    "mail.google.com",
    "drive.google.com",
    "calendar.google.com",
    "contacts.google.com",
    "web.whatsapp.com",
    "web.telegram.org",
    "app.slack.com",
    "discord.com",
    "github.com",
    "gitlab.",
    "chatgpt.com",
    "claude.com",
    "notion.site",
    "cloudflare.com",
    "dash.cloudflare.com",
    "cad.onshape.com",
    "miro.com",
    "milanote.com",
)


@dataclass
class StorageItem:
    path: Path
    bucket: str
    origin_name: str
    site: str
    origin_url: str
    partition: str
    category: str
    risk: str
    data_types: str
    size: int
    modified: float
    selected: bool = False
    protected: bool = False


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def format_time(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def dir_stats(path: Path) -> tuple[int, float]:
    total = 0
    latest = 0.0
    for root, dirs, files in os.walk(path):
        try:
            latest = max(latest, Path(root).stat().st_mtime)
        except OSError:
            pass
        for name in files:
            file_path = Path(root) / name
            try:
                stat = file_path.stat()
            except OSError:
                continue
            total += stat.st_size
            latest = max(latest, stat.st_mtime)
    return total, latest


def load_extension_names(profile_dir: Path) -> dict[str, str]:
    names_by_addon_id = {}
    extensions_json = profile_dir / "extensions.json"
    try:
        data = json.loads(extensions_json.read_text(encoding="utf-8"))
        for addon in data.get("addons", []):
            addon_id = addon.get("id")
            name = (addon.get("defaultLocale") or {}).get("name")
            if addon_id and name:
                names_by_addon_id[addon_id] = name
    except (OSError, json.JSONDecodeError):
        pass

    uuid_to_name = {}
    prefs = profile_dir / "prefs.js"
    try:
        text = prefs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return uuid_to_name

    match = re.search(r'user_pref\("extensions\.webextensions\.uuids",\s*"(.+?)"\);', text)
    if not match:
        return uuid_to_name
    try:
        decoded = bytes(match.group(1), "utf-8").decode("unicode_escape")
        uuid_map = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return uuid_to_name

    for addon_id, uuid in uuid_map.items():
        uuid_to_name[uuid] = names_by_addon_id.get(addon_id, addon_id)
    return uuid_to_name


def split_partition(name: str) -> tuple[str, str]:
    marker = "^partitionKey="
    if marker not in name:
        return name, ""
    origin, raw_partition = name.split(marker, 1)
    decoded = unquote(raw_partition)
    match = re.match(r"\(([^,]+),(.+)\)", decoded)
    if match:
        scheme, site = match.groups()
        return origin, f"{scheme}://{site}"
    return origin, decoded


def decode_origin(origin: str, extension_names: dict[str, str]) -> tuple[str, str]:
    if origin == "chrome":
        return "Firefox internal chrome", "chrome://"
    if origin.startswith("indexeddb+++"):
        name = origin.removeprefix("indexeddb+++")
        return f"Firefox internal: {name}", origin
    if origin.startswith("moz-extension+++"):
        rest = origin.removeprefix("moz-extension+++")
        uuid = rest.split("^", 1)[0]
        name = extension_names.get(uuid, uuid)
        return f"Extension: {name}", f"moz-extension://{uuid}"
    if origin.startswith("file++++"):
        file_path = "/" + unquote(origin.removeprefix("file++++").replace("+", "/"))
        return file_path, f"file://{file_path}"

    match = re.match(r"^(https?|wss?)\+\+\+(.+)$", origin)
    if match:
        scheme, rest = match.groups()
        rest = unquote(rest)
        if "+" in rest:
            host, maybe_port = rest.rsplit("+", 1)
            if maybe_port.isdigit():
                site = f"{host}:{maybe_port}"
            else:
                site = rest.replace("+", ".")
        else:
            site = rest
        return site, f"{scheme}://{site}"

    return unquote(origin), unquote(origin)


def data_types(path: Path) -> str:
    names = []
    for child in sorted(path.iterdir()):
        if child.is_dir():
            names.append(child.name)
    return ", ".join(names) if names else "-"


def classify(item: StorageItem) -> tuple[str, str, bool]:
    text = f"{item.origin_name} {item.site} {item.partition}".lower()

    if item.bucket == "permanent" or item.origin_name == "chrome" or item.site.startswith("Firefox internal"):
        return "Firefox interne", "protégé", True
    if item.origin_name.startswith("moz-extension+++"):
        return "Extension Firefox", "protégé", True
    if item.origin_url.startswith("file://"):
        return "Fichier local", "manuel", False
    if any(hint in text for hint in SENSITIVE_HINTS):
        return "Compte / app importante", "sensible", False
    if any(hint in text for hint in TRACKER_HINTS):
        return "Pub / traceur / consentement", "faible", False
    if item.partition:
        return "Site tiers partitionné", "faible à moyen", False
    if item.data_types == "cache":
        return "Cache site", "faible", False
    return "Site web", "manuel", False


def iter_origin_dirs(storage_dir: Path) -> list[tuple[str, Path]]:
    entries = []
    for bucket in ("default", "permanent", "temporary"):
        root = storage_dir / bucket
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir():
                entries.append((bucket, child))
    return entries


def scan_storage(storage_dir: Path = STORAGE_DIR) -> list[StorageItem]:
    extension_names = load_extension_names(PROFILE_DIR)
    items = []
    for bucket, path in iter_origin_dirs(storage_dir):
        origin_name = path.name
        base_origin, partition = split_partition(origin_name)
        site, origin_url = decode_origin(base_origin, extension_names)
        size, modified = dir_stats(path)
        item = StorageItem(
            path=path,
            bucket=bucket,
            origin_name=origin_name,
            site=site,
            origin_url=origin_url,
            partition=partition,
            category="",
            risk="",
            data_types=data_types(path),
            size=size,
            modified=modified,
        )
        item.category, item.risk, item.protected = classify(item)
        items.append(item)
    return items


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def delete_items(items: list[StorageItem]) -> tuple[int, int]:
    deleted = 0
    freed = 0
    for item in items:
        if item.protected or not item.path.exists():
            continue
        size, _ = dir_stats(item.path)
        remove_path(item.path)
        deleted += 1
        freed += size
    return deleted, freed


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Mozilla Firefox storage cleaner")
        self.items: list[StorageItem] = []
        self.visible_items: list[StorageItem] = []
        self.item_by_row: dict[str, StorageItem] = {}
        self.filter_text = tk.StringVar()
        self.before_date_text = tk.StringVar()
        self.size_kb_text = tk.StringVar()

        controls = ttk.Frame(root, padding=10)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text=f"Profile storage: {STORAGE_DIR}").pack(anchor=tk.W)
        ttk.Label(
            controls,
            text="Conseil : fermer Firefox avant suppression. Les éléments Firefox internes et extensions sont protégés.",
        ).pack(anchor=tk.W)

        buttons = ttk.Frame(root, padding=(10, 0, 10, 8))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Scan", command=self.scan).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cocher pub/traceurs", command=self.check_trackers).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Cocher tiers partitionnés", command=self.check_partitioned).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cocher caches purs", command=self.check_cache_only).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Décocher tout", command=self.clear_checked).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Supprimer lignes cochées", command=self.delete_checked).pack(side=tk.LEFT, padx=8)

        criteria_frame = ttk.Frame(root, padding=(10, 0, 10, 8))
        criteria_frame.pack(fill=tk.X)
        ttk.Label(criteria_frame, text="Date avant:").pack(side=tk.LEFT)
        ttk.Entry(criteria_frame, textvariable=self.before_date_text, width=12).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(criteria_frame, text="AAAA-MM-JJ").pack(side=tk.LEFT)
        ttk.Button(criteria_frame, text="Cocher avant date", command=self.check_before_date).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Label(criteria_frame, text="Taille Ko:").pack(side=tk.LEFT, padx=(12, 0))
        ttk.Entry(criteria_frame, textvariable=self.size_kb_text, width=10).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(criteria_frame, text="Cocher < Ko", command=self.check_smaller_than_size).pack(side=tk.LEFT)
        ttk.Button(criteria_frame, text="Cocher > Ko", command=self.check_larger_than_size).pack(
            side=tk.LEFT, padx=8
        )

        filter_frame = ttk.Frame(root, padding=(10, 0, 10, 8))
        filter_frame.pack(fill=tk.X)
        ttk.Label(filter_frame, text="Filtre:").pack(side=tk.LEFT)
        entry = ttk.Entry(filter_frame, textvariable=self.filter_text)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        entry.bind("<KeyRelease>", lambda event: self.refresh())
        ttk.Button(filter_frame, text="Effacer", command=self.clear_filter).pack(side=tk.LEFT)

        self.summary = tk.StringVar(value="Prêt.")
        ttk.Label(root, textvariable=self.summary, padding=(10, 0, 10, 8)).pack(fill=tk.X)

        columns = (
            "selected",
            "size",
            "modified",
            "risk",
            "category",
            "site",
            "partition",
            "types",
            "bucket",
            "path",
        )
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=24)
        for column, title, width in (
            ("selected", "Sel", 45),
            ("size", "Taille", 80),
            ("modified", "Modifié", 120),
            ("risk", "Risque", 105),
            ("category", "Catégorie", 175),
            ("site", "Site touché", 280),
            ("partition", "Dans le contexte de", 220),
            ("types", "Données", 110),
            ("bucket", "Zone", 85),
            ("path", "Chemin", 520),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)

    def scan(self) -> None:
        self.items = scan_storage()
        self.refresh()

    def filtered_items(self) -> list[StorageItem]:
        text = self.filter_text.get().strip().lower()
        if not text:
            return list(self.items)
        return [
            item
            for item in self.items
            if text
            in " ".join(
                (
                    item.site,
                    item.partition,
                    item.category,
                    item.risk,
                    item.data_types,
                    item.bucket,
                    str(item.path),
                )
            ).lower()
        ]

    def refresh(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.item_by_row.clear()
        self.visible_items = sorted(self.filtered_items(), key=lambda item: item.size, reverse=True)
        for index, item in enumerate(self.visible_items):
            row_id = str(index)
            self.item_by_row[row_id] = item
            self.tree.insert(
                "",
                tk.END,
                iid=row_id,
                values=(
                    "[x]" if item.selected else "[ ]",
                    human_size(item.size),
                    format_time(item.modified),
                    item.risk,
                    item.category,
                    item.site,
                    item.partition,
                    item.data_types,
                    item.bucket,
                    str(item.path),
                ),
            )
        self.update_summary()

    def update_summary(self) -> None:
        selected = [item for item in self.items if item.selected]
        selected_size = sum(item.size for item in selected)
        protected_count = sum(1 for item in self.items if item.protected)
        total_size = sum(item.size for item in self.items)
        self.summary.set(
            f"{len(self.visible_items)} visibles / {len(self.items)} total, "
            f"{len(selected)} cochées ({human_size(selected_size)}), "
            f"{protected_count} protégées, total {human_size(total_size)}."
        )

    def update_row(self, row_id: str) -> None:
        item = self.item_by_row[row_id]
        values = list(self.tree.item(row_id, "values"))
        values[0] = "[x]" if item.selected else "[ ]"
        self.tree.item(row_id, values=values)
        self.update_summary()

    def toggle_row(self, row_id: str) -> None:
        item = self.item_by_row.get(row_id)
        if not item or item.protected:
            return
        item.selected = not item.selected
        self.update_row(row_id)

    def on_tree_click(self, event: tk.Event) -> str | None:
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row_id and column == "#1":
            self.toggle_row(row_id)
            return "break"
        return None

    def on_tree_space(self, event: tk.Event) -> str:
        for row_id in self.tree.selection():
            self.toggle_row(row_id)
        return "break"

    def ensure_scanned(self) -> bool:
        if not self.items:
            self.scan()
        return bool(self.items)

    def apply_check(self, predicate: Callable[[StorageItem], bool]) -> None:
        if not self.ensure_scanned():
            return
        for item in self.items:
            item.selected = bool(predicate(item)) and not item.protected
        self.refresh()

    def check_trackers(self) -> None:
        self.apply_check(lambda item: item.category == "Pub / traceur / consentement")

    def check_partitioned(self) -> None:
        self.apply_check(lambda item: bool(item.partition) and item.risk != "sensible")

    def check_cache_only(self) -> None:
        self.apply_check(lambda item: item.data_types == "cache" and item.risk != "sensible")

    def parse_date_cutoff(self) -> float | None:
        raw = self.before_date_text.get().strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").timestamp()
        except ValueError:
            messagebox.showerror("Date invalide", "Utilise le format AAAA-MM-JJ, par exemple 2026-01-31.")
            return None

    def parse_size_threshold(self) -> int | None:
        raw = self.size_kb_text.get().strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            messagebox.showerror("Taille invalide", "Entre une taille en Ko, par exemple 512 ou 1024.")
            return None
        if value < 0:
            messagebox.showerror("Taille invalide", "La taille doit être positive.")
            return None
        return int(value * 1024)

    def check_before_date(self) -> None:
        cutoff = self.parse_date_cutoff()
        if cutoff is None:
            return
        self.apply_check(lambda item: item.modified and item.modified < cutoff)

    def check_smaller_than_size(self) -> None:
        threshold = self.parse_size_threshold()
        if threshold is None:
            return
        self.apply_check(lambda item: item.size < threshold and item.risk != "sensible")

    def check_larger_than_size(self) -> None:
        threshold = self.parse_size_threshold()
        if threshold is None:
            return
        self.apply_check(lambda item: item.size > threshold and item.risk != "sensible")

    def clear_checked(self) -> None:
        for item in self.items:
            item.selected = False
        self.refresh()

    def clear_filter(self) -> None:
        self.filter_text.set("")
        self.refresh()

    def delete_checked(self) -> None:
        if not self.ensure_scanned():
            messagebox.showinfo("Rien à supprimer", "Aucun élément trouvé.")
            return
        checked = [item for item in self.items if item.selected and not item.protected]
        if not checked:
            messagebox.showinfo("Rien à supprimer", "Aucune ligne cochable n'est cochée.")
            return
        total_size = sum(item.size for item in checked)
        sensitive = sum(1 for item in checked if item.risk == "sensible")
        warning = ""
        if sensitive:
            warning = f"\n\nAttention : {sensitive} lignes sont marquées sensibles."
        if not messagebox.askyesno(
            "Confirmation",
            "Ferme Firefox avant de continuer.\n\n"
            f"Supprimer {len(checked)} origines et libérer environ {human_size(total_size)} ?"
            f"{warning}",
        ):
            return
        deleted, freed = delete_items(checked)
        messagebox.showinfo("Terminé", f"{deleted} origines supprimées, {human_size(freed)} libérés.")
        self.scan()


def print_scan(items: list[StorageItem]) -> None:
    for item in sorted(items, key=lambda item: item.size, reverse=True):
        flag = "PROTECT" if item.protected else "manual"
        print(
            f"{flag:7} {human_size(item.size):>8} {format_time(item.modified):16} "
            f"{item.risk:14} {item.category:26} {item.site:45} "
            f"{item.partition:30} {item.data_types:14} {item.path}"
        )
    print(f"\nTotal: {human_size(sum(item.size for item in items))} in {len(items)} origins")


def main(argv: list[str]) -> int:
    if "--scan" in argv:
        print_scan(scan_storage())
        return 0

    if tk is None:
        print("Tkinter is not available. Use --scan for CLI output.", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.geometry("1500x760")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
