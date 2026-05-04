#!/usr/bin/env python3
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None


DEFAULT_VSIX_DIR = Path.home() / ".config/Code/CachedExtensionVSIXs"
DEFAULT_WEBSTORAGE_DIR = Path.home() / ".config/Code/WebStorage"
DEFAULT_EXTENSIONS_DIR = Path.home() / ".vscode/extensions"

PLATFORMS = (
    "linux-x64",
    "linux-arm64",
    "win32-x64",
    "win32-arm64",
    "darwin-x64",
    "darwin-arm64",
    "alpine-x64",
    "alpine-arm64",
    "web",
    "universal",
)

VSIX_RE = re.compile(
    r"^(.+)-(\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.]+)?)(?:-("
    + "|".join(re.escape(p) for p in PLATFORMS)
    + r"))?$"
)
EXTENSION_DIR_RE = re.compile(rb"/home/[^/\x00]+/\.vscode/extensions/([^/\x00]+)")


@dataclass
class Item:
    path: Path
    size: int
    group: str
    version: str
    kind: str
    action: str
    reason: str
    selected: bool = False


def version_key(version: str) -> tuple:
    parts = re.split(r"([0-9]+)", version)
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part))
    return tuple(key)


def get_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def parse_vsix_name(path: Path) -> tuple[str, str] | None:
    match = VSIX_RE.match(path.name)
    if not match:
        return None
    extension_id, version, platform = match.groups()
    platform_suffix = f" [{platform}]" if platform else ""
    return f"{extension_id}{platform_suffix}", version


def scan_vsix_dir(path: Path) -> list[Item]:
    if not path.exists():
        return []

    parsed = []
    unknown = []
    for child in sorted(path.iterdir()):
        if not child.is_file():
            continue
        match = parse_vsix_name(child)
        size = get_size(child)
        if match:
            group, version = match
            parsed.append(Item(child, size, group, version, "VSIX", "keep", "latest version"))
        else:
            unknown.append(Item(child, size, "unknown", "", "VSIX", "keep", "name not recognized"))

    latest_by_group = {}
    for item in parsed:
        current = latest_by_group.get(item.group)
        if current is None or version_key(item.version) > version_key(current.version):
            latest_by_group[item.group] = item

    result = []
    for item in parsed:
        latest = latest_by_group[item.group]
        if item.version == latest.version:
            item.action = "keep"
            item.reason = "latest version for this extension"
        else:
            item.action = "delete"
            item.reason = f"older than {latest.version}"
        result.append(item)
    result.extend(unknown)
    return result


def sample_files(path: Path, limit: int = 64) -> Iterable[Path]:
    count = 0
    for root, dirs, files in os.walk(path):
        dirs.sort()
        files.sort()
        for name in files:
            yield Path(root) / name
            count += 1
            if count >= limit:
                return


def detect_webstorage_extension(path: Path) -> tuple[str, str] | None:
    for file_path in sample_files(path):
        try:
            with file_path.open("rb") as handle:
                data = handle.read(1024 * 1024)
        except OSError:
            continue
        match = EXTENSION_DIR_RE.search(data)
        if match:
            extension_dir = match.group(1).decode("ascii", errors="replace")
            return parse_vsix_name(Path(extension_dir))
    return None


def webstorage_cache_dirs(path: Path) -> list[Path]:
    cache_root_dirs = sorted(path.glob("*/CacheStorage/*"))
    return [p for p in cache_root_dirs if p.is_dir()]


def scan_webstorage_dir(path: Path) -> list[Item]:
    if not path.exists():
        return []

    parsed = []
    unknown = []
    for cache_dir in webstorage_cache_dirs(path):
        detected = detect_webstorage_extension(cache_dir)
        size = get_size(cache_dir)
        if detected:
            group, version = detected
            parsed.append(Item(cache_dir, size, group, version, "WebStorage", "keep", "latest version"))
        else:
            unknown.append(Item(cache_dir, size, "unknown", "", "WebStorage", "keep", "no extension version detected"))

    latest_by_group = {}
    for item in parsed:
        current = latest_by_group.get(item.group)
        if current is None or version_key(item.version) > version_key(current.version):
            latest_by_group[item.group] = item

    result = []
    for item in parsed:
        latest = latest_by_group[item.group]
        if item.version == latest.version:
            item.action = "keep"
            item.reason = "latest detected webview cache"
        else:
            item.action = "delete"
            item.reason = f"older than {latest.version}"
        result.append(item)
    result.extend(unknown)
    return result


def scan_installed_extensions_dir(path: Path) -> list[Item]:
    if not path.exists():
        return []

    parsed = []
    unknown = []
    for child in sorted(path.iterdir()):
        if not child.is_dir():
            continue
        match = parse_vsix_name(child)
        size = get_size(child)
        if match:
            group, version = match
            parsed.append(Item(child, size, group, version, "Installed", "keep", "installed extension"))
        else:
            unknown.append(Item(child, size, "unknown", "", "Installed", "keep", "directory name not recognized"))

    latest_by_group = {}
    for item in parsed:
        current = latest_by_group.get(item.group)
        if current is None or version_key(item.version) > version_key(current.version):
            latest_by_group[item.group] = item

    result = []
    for item in parsed:
        latest = latest_by_group[item.group]
        if item.version == latest.version:
            item.action = "keep"
            item.reason = "installed latest version"
        else:
            item.action = "delete"
            item.reason = f"installed version older than {latest.version}"
        result.append(item)
    result.extend(unknown)
    return result


def scan(selected_vsix: bool, selected_webstorage: bool, selected_installed: bool = False) -> list[Item]:
    items = []
    if selected_vsix:
        items.extend(scan_vsix_dir(DEFAULT_VSIX_DIR))
    if selected_webstorage:
        items.extend(scan_webstorage_dir(DEFAULT_WEBSTORAGE_DIR))
    if selected_installed:
        items.extend(scan_installed_extensions_dir(DEFAULT_EXTENSIONS_DIR))
    for item in items:
        item.selected = item.action == "delete"
    return items


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def delete_items(items: list[Item], delete_all: bool = False) -> tuple[int, int]:
    deleted = 0
    freed = 0
    for item in items:
        if not delete_all and item.action != "delete":
            continue
        if not item.path.exists():
            continue
        size = get_size(item.path)
        remove_path(item.path)
        deleted += 1
        freed += size
    return deleted, freed


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("VS Code cache cleaner")
        self.items: list[Item] = []
        self.item_by_row: dict[str, Item] = {}

        self.use_vsix = tk.BooleanVar(value=True)
        self.use_webstorage = tk.BooleanVar(value=True)
        self.use_installed = tk.BooleanVar(value=False)

        controls = ttk.Frame(root, padding=10)
        controls.pack(fill=tk.X)

        ttk.Checkbutton(
            controls,
            text=f"CachedExtensionVSIXs ({DEFAULT_VSIX_DIR})",
            variable=self.use_vsix,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            controls,
            text=f"WebStorage ({DEFAULT_WEBSTORAGE_DIR})",
            variable=self.use_webstorage,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            controls,
            text=f"Extensions installées ({DEFAULT_EXTENSIONS_DIR})",
            variable=self.use_installed,
        ).pack(anchor=tk.W)

        buttons = ttk.Frame(root, padding=(10, 0, 10, 10))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Scan", command=self.scan).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Supprimer anciennes versions", command=self.delete_old).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(buttons, text="Supprimer lignes cochées", command=self.delete_checked).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="Cocher anciennes", command=self.check_old).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Décocher tout", command=self.clear_checked).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Tout supprimer", command=self.delete_all).pack(side=tk.LEFT)

        self.summary = tk.StringVar(value="Prêt.")
        ttk.Label(root, textvariable=self.summary, padding=(10, 0, 10, 8)).pack(fill=tk.X)

        columns = ("selected", "action", "size", "kind", "group", "version", "reason", "path")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=22)
        for column, title, width in (
            ("selected", "Sel", 45),
            ("action", "Action", 70),
            ("size", "Taille", 80),
            ("kind", "Type", 95),
            ("group", "Groupe", 260),
            ("version", "Version", 110),
            ("reason", "Raison", 220),
            ("path", "Chemin", 520),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)

    def scan(self) -> None:
        self.items = scan(self.use_vsix.get(), self.use_webstorage.get(), self.use_installed.get())
        self.refresh()

    def refresh(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.item_by_row.clear()
        sorted_items = sorted(self.items, key=lambda x: (x.action != "delete", x.kind, x.group, x.path.name))
        for index, item in enumerate(sorted_items):
            row_id = str(index)
            self.item_by_row[row_id] = item
            self.tree.insert(
                "",
                tk.END,
                iid=row_id,
                values=(
                    "[x]" if item.selected else "[ ]",
                    item.action,
                    human_size(item.size),
                    item.kind,
                    item.group,
                    item.version,
                    item.reason,
                    str(item.path),
                ),
            )
        delete_count = sum(1 for item in self.items if item.action == "delete")
        delete_size = sum(item.size for item in self.items if item.action == "delete")
        selected_count = sum(1 for item in self.items if item.selected)
        selected_size = sum(item.size for item in self.items if item.selected)
        total_size = sum(item.size for item in self.items)
        self.summary.set(
            f"{len(self.items)} éléments trouvés, {delete_count} anciennes versions à supprimer "
            f"({human_size(delete_size)}), {selected_count} lignes cochées "
            f"({human_size(selected_size)}), total scanné {human_size(total_size)}."
        )

    def update_row(self, row_id: str) -> None:
        item = self.item_by_row[row_id]
        values = list(self.tree.item(row_id, "values"))
        values[0] = "[x]" if item.selected else "[ ]"
        self.tree.item(row_id, values=values)
        self.update_summary_only()

    def update_summary_only(self) -> None:
        delete_count = sum(1 for item in self.items if item.action == "delete")
        delete_size = sum(item.size for item in self.items if item.action == "delete")
        selected_count = sum(1 for item in self.items if item.selected)
        selected_size = sum(item.size for item in self.items if item.selected)
        total_size = sum(item.size for item in self.items)
        self.summary.set(
            f"{len(self.items)} éléments trouvés, {delete_count} anciennes versions à supprimer "
            f"({human_size(delete_size)}), {selected_count} lignes cochées "
            f"({human_size(selected_size)}), total scanné {human_size(total_size)}."
        )

    def toggle_row(self, row_id: str) -> None:
        if row_id not in self.item_by_row:
            return
        item = self.item_by_row[row_id]
        item.selected = not item.selected
        self.update_row(row_id)

    def on_tree_click(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row_id and column == "#1":
            self.toggle_row(row_id)
            return "break"

    def on_tree_space(self, event: tk.Event) -> str:
        rows = self.tree.selection()
        if rows:
            for row_id in rows:
                self.toggle_row(row_id)
        return "break"

    def check_old(self) -> None:
        if not self.ensure_scanned():
            return
        for item in self.items:
            item.selected = item.action == "delete"
        self.refresh()

    def clear_checked(self) -> None:
        for item in self.items:
            item.selected = False
        self.refresh()

    def ensure_scanned(self) -> bool:
        if not self.items:
            self.scan()
        return bool(self.items)

    def delete_old(self) -> None:
        if not self.ensure_scanned():
            messagebox.showinfo("Rien à supprimer", "Aucun élément trouvé.")
            return
        delete_count = sum(1 for item in self.items if item.action == "delete")
        delete_size = sum(item.size for item in self.items if item.action == "delete")
        if delete_count == 0:
            messagebox.showinfo("Rien à supprimer", "Aucune ancienne version identifiable.")
            return
        if not messagebox.askyesno(
            "Confirmation",
            f"Supprimer {delete_count} anciennes versions et libérer environ {human_size(delete_size)} ?",
        ):
            return
        deleted, freed = delete_items(self.items, delete_all=False)
        messagebox.showinfo("Terminé", f"{deleted} éléments supprimés, {human_size(freed)} libérés.")
        self.scan()

    def delete_checked(self) -> None:
        if not self.ensure_scanned():
            messagebox.showinfo("Rien à supprimer", "Aucun élément trouvé.")
            return
        checked = [item for item in self.items if item.selected]
        checked_size = sum(item.size for item in checked)
        if not checked:
            messagebox.showinfo("Rien à supprimer", "Aucune ligne cochée.")
            return
        if not messagebox.askyesno(
            "Confirmation",
            f"Supprimer {len(checked)} lignes cochées et libérer environ {human_size(checked_size)} ?",
        ):
            return
        deleted, freed = delete_items(checked, delete_all=True)
        messagebox.showinfo("Terminé", f"{deleted} éléments supprimés, {human_size(freed)} libérés.")
        self.scan()

    def delete_all(self) -> None:
        if not self.ensure_scanned():
            messagebox.showinfo("Rien à supprimer", "Aucun élément trouvé.")
            return
        total_size = sum(item.size for item in self.items)
        installed_count = sum(1 for item in self.items if item.kind == "Installed")
        warning = ""
        if installed_count:
            warning = (
                f"\n\nAttention: {installed_count} extensions installées sont incluses. "
                "Les supprimer revient à les désinstaller de VS Code."
            )
        if not messagebox.askyesno(
            "Confirmation",
            "Tout supprimer dans les dossiers sélectionnés ?\n\n"
            f"{len(self.items)} éléments, environ {human_size(total_size)}.{warning}",
        ):
            return
        deleted, freed = delete_items(self.items, delete_all=True)
        messagebox.showinfo("Terminé", f"{deleted} éléments supprimés, {human_size(freed)} libérés.")
        self.scan()


def print_scan(items: list[Item]) -> None:
    for item in sorted(items, key=lambda x: (x.action != "delete", x.kind, x.group, x.path.name)):
        print(
            f"{item.action:6} {human_size(item.size):>8} {item.kind:10} "
            f"{item.group:45} {item.version:15} {item.reason} {item.path}"
        )
    delete_size = sum(item.size for item in items if item.action == "delete")
    print(f"\nOld versions to delete: {human_size(delete_size)}")


def main(argv: list[str]) -> int:
    if "--scan" in argv or "--delete-old" in argv or "--delete-all" in argv:
        include_installed = "--include-installed" in argv
        items = scan(True, True, include_installed)
        print_scan(items)
        if "--delete-old" in argv:
            deleted, freed = delete_items(items, delete_all=False)
            print(f"Deleted {deleted} old items, freed {human_size(freed)}.")
        elif "--delete-all" in argv:
            deleted, freed = delete_items(items, delete_all=True)
            print(f"Deleted {deleted} items, freed {human_size(freed)}.")
        return 0

    if tk is None:
        print(
            "Tkinter is not available. Use --scan, --delete-old, --delete-all, "
            "and optionally --include-installed.",
            file=sys.stderr,
        )
        return 1

    root = tk.Tk()
    root.geometry("1300x650")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
