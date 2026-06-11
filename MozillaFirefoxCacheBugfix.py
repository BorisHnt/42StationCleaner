#!/usr/bin/env python3
"""Repair Firefox caches and extension registries without resetting user data."""

import argparse
import configparser
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
except ImportError:
    tk = None
    messagebox = None
    simpledialog = None
    ttk = None


FIREFOX_DIR = Path.home() / ".mozilla/firefox"
CACHE_BASE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
CACHE_ROOT = CACHE_BASE / "mozilla/firefox"
BACKUP_DIR_NAME = "MozillaFirefoxCacheBugfix-backups"

# These directories contain generated data. Firefox recreates them as needed.
CACHE_DIR_NAMES = (
    "cache2",
    "startupCache",
    "shader-cache",
    "jumpListCache",
    "OfflineCache",
)

# These files describe the local Firefox/add-on installation. They can contain
# machine-specific paths and are rebuilt from the installed XPI files.
EXTENSION_REGISTRY_FILES = (
    "addonStartup.json.lz4",
    "compatibility.ini",
    "extensions.json",
    "extensions.ini",
    "extensions.sqlite",
    "extensions.sqlite-journal",
)

MIGRATION_FILES = (
    "logins.json",
    "key4.db",
    "cookies.sqlite",
    "places.sqlite",
    "favicons.sqlite",
    "formhistory.sqlite",
)

PROFILE_MARKER_FILES = (
    "places.sqlite",
    "cookies.sqlite",
    "key4.db",
    "extensions.json",
    "logins.json",
)

SEARCH_PRUNE_DIRS = {
    ".cache",
    ".git",
    ".local/share/Trash",
    "Cache",
    "cache2",
    "Code Cache",
    "node_modules",
    "startupCache",
}


@dataclass(frozen=True)
class FirefoxProfile:
    name: str
    path: Path
    is_default: bool = False


@dataclass(frozen=True)
class RepairItem:
    path: Path
    category: str
    action: str
    size: int
    reason: str


@dataclass
class RepairResult:
    removed: int = 0
    backed_up: int = 0
    quarantined: int = 0
    freed: int = 0
    backup_dir: Path | None = None
    quarantine_paths: list[Path] | None = None
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.quarantine_paths is None:
            self.quarantine_paths = []
        if self.errors is None:
            self.errors = []


@dataclass
class MigrationResult:
    copied: list[str]
    skipped: list[str]
    backup_dir: Path | None
    errors: list[str]


@dataclass(frozen=True)
class CreatedProfile:
    profile: FirefoxProfile
    made_default: bool


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0

    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return parser


def write_ini(path: Path, parser: configparser.ConfigParser) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)
    os.replace(temporary, path)


def resolve_profile_path(root: Path, raw_path: str, is_relative: bool) -> Path:
    path = Path(raw_path).expanduser()
    return root / path if is_relative else path


def discover_profiles(root: Path = FIREFOX_DIR) -> list[FirefoxProfile]:
    profiles_ini = read_ini(root / "profiles.ini")
    installs_ini = read_ini(root / "installs.ini")
    install_defaults = {
        section.get("Default", "")
        for section in installs_ini.values()
        if section.get("Default", "")
    }

    profiles = []
    seen: set[Path] = set()
    for section_name in profiles_ini.sections():
        if not section_name.startswith("Profile"):
            continue
        section = profiles_ini[section_name]
        raw_path = section.get("Path", "").strip()
        if not raw_path:
            continue
        is_relative = section.getboolean("IsRelative", fallback=True)
        path = resolve_profile_path(root, raw_path, is_relative)
        path_key = path.resolve(strict=False)
        if path_key in seen:
            continue
        seen.add(path_key)
        profiles.append(
            FirefoxProfile(
                name=section.get("Name", path.name),
                path=path,
                is_default=raw_path in install_defaults
                or (
                    not install_defaults
                    and section.getboolean("Default", fallback=False)
                ),
            )
        )

    if not profiles and root.exists():
        for path in sorted(root.glob("*.default*")):
            if path.is_dir():
                profiles.append(FirefoxProfile(path.name, path, False))

    return sorted(profiles, key=lambda profile: (not profile.is_default, profile.name, str(profile.path)))


def default_profile_search_roots() -> list[Path]:
    roots = [Path.home()]
    goinfre = Path("/goinfre") / Path.home().name
    if goinfre.is_dir() and goinfre.resolve(strict=False) != Path.home().resolve(strict=False):
        roots.append(goinfre)
    return roots


def is_firefox_profile_directory(path: Path) -> bool:
    if not (path / "prefs.js").is_file():
        return False
    return any((path / marker).exists() for marker in PROFILE_MARKER_FILES)


def find_firefox_profiles(
    search_roots: list[Path] | None = None,
    known_profiles: list[FirefoxProfile] | None = None,
) -> list[FirefoxProfile]:
    if known_profiles is None:
        known_profiles = discover_profiles()
    profiles_by_path = {
        profile.path.resolve(strict=False): profile for profile in known_profiles
    }

    for search_root in search_roots or default_profile_search_roots():
        search_root = search_root.expanduser().resolve(strict=False)
        if not search_root.is_dir():
            continue
        for root, dirs, _files in os.walk(search_root, followlinks=False):
            directory = Path(root)
            relative_parts = directory.relative_to(search_root).parts
            relative_path = "/".join(relative_parts)
            dirs[:] = [
                name
                for name in dirs
                if not (directory / name).is_symlink()
                and name not in SEARCH_PRUNE_DIRS
                and "/".join((*relative_parts, name)) not in SEARCH_PRUNE_DIRS
                and not name.startswith(f"{BACKUP_DIR_NAME}")
                and not name.startswith("backup-before-transfer-")
            ]
            if relative_path in SEARCH_PRUNE_DIRS:
                dirs.clear()
                continue
            if not is_firefox_profile_directory(directory):
                continue
            resolved = directory.resolve(strict=False)
            if resolved not in profiles_by_path:
                profiles_by_path[resolved] = FirefoxProfile(
                    name=directory.name,
                    path=directory,
                    is_default=False,
                )
            dirs.clear()

    return sorted(
        profiles_by_path.values(),
        key=lambda profile: (not profile.is_default, profile.name, str(profile.path)),
    )


def valid_profile_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("le nom du profil est vide")
    if len(name) > 64:
        raise ValueError("le nom du profil dépasse 64 caractères")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError(
            "le nom du profil doit contenir uniquement lettres ASCII, chiffres, '.', '_' ou '-'"
        )
    return name


def set_default_profile(profile: FirefoxProfile, root: Path = FIREFOX_DIR) -> None:
    profiles_ini_path = root / "profiles.ini"
    profiles_ini = read_ini(profiles_ini_path)
    relative_path = os.path.relpath(profile.path, root)
    matched = False

    for section_name in profiles_ini.sections():
        if not section_name.startswith("Profile"):
            continue
        section = profiles_ini[section_name]
        raw_path = section.get("Path", "")
        is_relative = section.getboolean("IsRelative", fallback=True)
        section_path = resolve_profile_path(root, raw_path, is_relative).resolve(strict=False)
        is_selected = section_path == profile.path.resolve(strict=False)
        if is_selected:
            section["Default"] = "1"
            matched = True
        else:
            section.pop("Default", None)

    if not matched:
        raise ValueError("le profil créé n'est pas enregistré dans profiles.ini")

    for section_name in profiles_ini.sections():
        if section_name.startswith("Install"):
            profiles_ini[section_name]["Default"] = relative_path
            profiles_ini[section_name]["Locked"] = "1"
    write_ini(profiles_ini_path, profiles_ini)

    installs_ini_path = root / "installs.ini"
    installs_ini = read_ini(installs_ini_path)
    for section_name in installs_ini.sections():
        installs_ini[section_name]["Default"] = relative_path
        installs_ini[section_name]["Locked"] = "1"
    if installs_ini.sections():
        write_ini(installs_ini_path, installs_ini)


def create_firefox_profile(
    name: str,
    make_default: bool = True,
    root: Path = FIREFOX_DIR,
    firefox_binary: str | None = None,
) -> CreatedProfile:
    name = valid_profile_name(name)
    if any(profile.name.casefold() == name.casefold() for profile in discover_profiles(root)):
        raise ValueError(f"un profil nommé {name!r} existe déjà")

    root.mkdir(parents=True, exist_ok=True)
    while True:
        profile_path = root / f"{secrets.token_hex(4)}.{name}"
        if not profile_path.exists():
            break

    firefox_binary = firefox_binary or shutil.which("firefox")
    if not firefox_binary:
        raise ValueError("exécutable Firefox introuvable")

    process = subprocess.run(
        [firefox_binary, "-CreateProfile", f"{name} {profile_path}"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        details = (process.stderr or process.stdout).strip()
        raise RuntimeError(details or f"Firefox a retourné le code {process.returncode}")

    created = next(
        (
            profile
            for profile in discover_profiles(root)
            if profile.path.resolve(strict=False) == profile_path.resolve(strict=False)
        ),
        None,
    )
    if created is None or not profile_path.is_dir():
        raise RuntimeError("Firefox n'a pas enregistré le nouveau profil")

    if make_default:
        set_default_profile(created, root)
        created = FirefoxProfile(created.name, created.path, True)
    return CreatedProfile(created, make_default)


def firefox_processes() -> list[tuple[int, str]]:
    proc = Path("/proc")
    if not proc.exists():
        return []

    current_uid = os.getuid()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != current_uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        args = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        executable = Path(args[0]).name.lower()
        command = " ".join(args)
        if executable in {"firefox", "firefox-bin"} or "/firefox" in args[0].lower():
            found.append((int(entry.name), command))
    return sorted(found)


def scan_profile(
    profile: FirefoxProfile,
    include_cache: bool = True,
    include_extensions: bool = True,
    cache_root: Path = CACHE_ROOT,
) -> list[RepairItem]:
    items = []
    if include_cache:
        for name in CACHE_DIR_NAMES:
            path = profile.path / name
            if path.exists() or path.is_symlink():
                items.append(
                    RepairItem(
                        path=path,
                        category="cache",
                        action="delete",
                        size=path_size(path),
                        reason="cache régénérable par Firefox",
                    )
                )
        external_profile_cache = cache_root / profile.path.name
        if external_profile_cache.exists() or external_profile_cache.is_symlink():
            items.append(
                RepairItem(
                    path=external_profile_cache,
                    category="cache local",
                    action="quarantine",
                    size=path_size(external_profile_cache),
                    reason="cache propre à ce poste, renommé en .bak pour permettre un retour arrière",
                )
            )

    if include_extensions:
        for name in EXTENSION_REGISTRY_FILES:
            path = profile.path / name
            if path.exists() or path.is_symlink():
                items.append(
                    RepairItem(
                        path=path,
                        category="extensions",
                        action="backup",
                        size=path_size(path),
                        reason="registre local reconstruit depuis les extensions installées",
                    )
                )
    return items


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
        return True
    except ValueError:
        return False


def profile_warnings(profile: FirefoxProfile, home: Path | None = None) -> list[str]:
    warnings = []
    home = home or Path.home()
    if is_within(profile.path, Path("/tmp")):
        warnings.append("le répertoire racine du profil est temporaire")
    elif not is_within(profile.path, home):
        warnings.append(
            "le répertoire racine du profil est hors du home persistant"
        )

    extensions_json = profile.path / "extensions.json"
    if extensions_json.exists():
        try:
            data = json.loads(extensions_json.read_text(encoding="utf-8"))
            invalid_paths = 0
            for addon in data.get("addons", []):
                raw_path = addon.get("path")
                if raw_path and Path(raw_path).is_absolute() and not Path(raw_path).exists():
                    invalid_paths += 1
            if invalid_paths:
                warnings.append(
                    f"{invalid_paths} chemin(s) d'extension absolu(s) ne correspondent pas à ce poste"
                )
        except (OSError, json.JSONDecodeError):
            warnings.append("extensions.json est illisible ou invalide")

    duplicate_prefs = sorted(profile.path.glob("prefs-[0-9]*.js"))
    if duplicate_prefs:
        warnings.append(
            f"{len(duplicate_prefs)} copie(s) prefs-N.js détectée(s), signe possible de conflits de synchronisation"
        )
    return warnings


def migration_plan(old_profile: Path, new_profile: Path) -> list[tuple[str, bool, bool]]:
    return [
        (name, (old_profile / name).exists(), (new_profile / name).exists())
        for name in MIGRATION_FILES
    ]


def migrate_profile_data(
    old_profile: Path,
    new_profile: Path,
    timestamp: str | None = None,
) -> MigrationResult:
    old_profile = old_profile.expanduser().resolve(strict=False)
    new_profile = new_profile.expanduser().resolve(strict=False)
    if old_profile == new_profile:
        raise ValueError("les profils source et destination sont identiques")
    if not old_profile.is_dir():
        raise ValueError(f"profil source introuvable: {old_profile}")
    if not new_profile.is_dir():
        raise ValueError(f"profil destination introuvable: {new_profile}")

    timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = new_profile / f"backup-before-transfer-{timestamp}"
    result = MigrationResult(copied=[], skipped=[], backup_dir=None, errors=[])

    password_files = ("logins.json", "key4.db")
    password_pair_complete = all((old_profile / name).is_file() for name in password_files)

    for name in MIGRATION_FILES:
        source = old_profile / name
        destination = new_profile / name
        if name in password_files and not password_pair_complete:
            result.skipped.append(f"{name}: paire logins.json/key4.db incomplète")
            continue
        if source.is_symlink():
            result.skipped.append(f"{name}: lien symbolique source refusé")
            continue
        if not source.is_file():
            result.skipped.append(f"{name}: absent de la source")
            continue
        sidecars = (
            [Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
            if name.endswith(".sqlite")
            else []
        )
        unsafe_sidecar = next((path for path in sidecars if path.is_symlink()), None)
        if unsafe_sidecar:
            result.errors.append(
                f"{unsafe_sidecar.name}: lien symbolique destination refusé"
            )
            continue
        try:
            if destination.is_symlink():
                result.errors.append(f"{name}: lien symbolique destination refusé")
                continue
            if destination.exists():
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_dir / name)
                result.backup_dir = backup_dir
            for sidecar in sidecars:
                if sidecar.exists():
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sidecar), str(backup_dir / sidecar.name))
                    result.backup_dir = backup_dir
            shutil.copy2(source, destination)
            result.copied.append(name)
        except OSError as error:
            result.errors.append(f"{name}: {error}")
    return result


def backup_destination(backup_dir: Path, profile: FirefoxProfile, item: RepairItem) -> Path:
    return backup_dir / profile.path.name / item.path.name


def quarantine_destination(path: Path, timestamp: str) -> Path:
    destination = path.with_name(f"{path.name}.bak-{timestamp}")
    suffix = 1
    while destination.exists() or destination.is_symlink():
        destination = path.with_name(f"{path.name}.bak-{timestamp}-{suffix}")
        suffix += 1
    return destination


def repair_profile(
    profile: FirefoxProfile,
    items: list[RepairItem],
    backup_root: Path | None = None,
) -> RepairResult:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = backup_root or profile.path.parent / BACKUP_DIR_NAME
    backup_dir = backup_root / timestamp
    result = RepairResult(backup_dir=backup_dir)

    for item in items:
        if not item.path.exists() and not item.path.is_symlink():
            continue
        try:
            size = path_size(item.path)
            if item.action == "backup":
                destination = backup_destination(backup_dir, profile, item)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination = destination.with_name(f"{destination.name}-{timestamp}")
                shutil.move(str(item.path), str(destination))
                result.backed_up += 1
            elif item.action == "quarantine":
                destination = quarantine_destination(item.path, timestamp)
                shutil.move(str(item.path), str(destination))
                result.quarantined += 1
                result.quarantine_paths.append(destination)
            else:
                remove_path(item.path)
                result.removed += 1
                result.freed += size
        except OSError as error:
            result.errors.append(f"{item.path}: {error}")

    if result.backed_up == 0:
        result.backup_dir = None
        try:
            backup_dir.rmdir()
        except OSError:
            pass
    return result


def select_profiles(
    profiles: list[FirefoxProfile],
    explicit_path: str | None,
    all_profiles: bool,
) -> list[FirefoxProfile]:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        return [FirefoxProfile(path.name, path, True)]
    if all_profiles:
        return profiles
    defaults = [profile for profile in profiles if profile.is_default]
    if defaults:
        return defaults
    return profiles[:1]


def print_scan(profiles: list[FirefoxProfile], include_cache: bool, include_extensions: bool) -> None:
    for profile in profiles:
        default = " [défaut]" if profile.is_default else ""
        print(f"\nProfil: {profile.name}{default}\nChemin: {profile.path}")
        for warning in profile_warnings(profile):
            print(f"ATTENTION: {warning}")
        items = scan_profile(profile, include_cache, include_extensions)
        if not items:
            print("Aucun cache ou registre ciblé trouvé.")
            continue
        for item in items:
            action_label = {
                "delete": "delete",
                "backup": "backup",
                "quarantine": "rename",
            }[item.action]
            print(
                f"{action_label:6} {human_size(item.size):>8} "
                f"{item.category:10} {item.path} ({item.reason})"
            )
        print(f"Total ciblé: {human_size(sum(item.size for item in items))}")


class App:
    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title("Mozilla Firefox Cache Bugfix")
        self.profiles = discover_profiles()
        self.profile_by_label = {
            self.profile_label(profile): profile for profile in self.profiles
        }
        self.profile_var = tk.StringVar()
        self.migrate_from_var = tk.StringVar()
        self.migrate_to_var = tk.StringVar()
        self.use_cache = tk.BooleanVar(value=True)
        self.use_extensions = tk.BooleanVar(value=False)
        self.summary = tk.StringVar(value="Prêt.")
        self.items: list[RepairItem] = []

        controls = ttk.Frame(root, padding=10)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Profil Firefox:").pack(side=tk.LEFT)
        self.profile_box = ttk.Combobox(
            controls,
            textvariable=self.profile_var,
            values=list(self.profile_by_label),
            state="readonly",
            width=70,
        )
        self.profile_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.profile_box.bind("<<ComboboxSelected>>", lambda event: self.scan())
        ttk.Button(
            controls,
            text="Créer un profil",
            command=self.create_profile,
        ).pack(side=tk.LEFT)
        ttk.Button(
            controls,
            text="Retrouver les profils",
            command=self.find_profiles,
        ).pack(side=tk.LEFT, padx=(8, 0))

        if self.profile_by_label:
            self.profile_var.set(next(iter(self.profile_by_label)))

        options = ttk.Frame(root, padding=(10, 0, 10, 8))
        options.pack(fill=tk.X)
        ttk.Checkbutton(
            options,
            text="Caches régénérables",
            variable=self.use_cache,
            command=self.scan,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            options,
            text="Reconstruire le registre des extensions",
            variable=self.use_extensions,
            command=self.scan,
        ).pack(side=tk.LEFT, padx=12)
        ttk.Button(options, text="Scanner", command=self.scan).pack(side=tk.LEFT)
        ttk.Button(options, text="Réparer", command=self.repair).pack(side=tk.LEFT, padx=8)

        migration = ttk.LabelFrame(root, text="Transfert vers un profil propre", padding=10)
        migration.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Label(migration, text="Depuis :").grid(row=0, column=0, sticky=tk.W)
        self.migrate_from_box = ttk.Combobox(
            migration,
            textvariable=self.migrate_from_var,
            values=list(self.profile_by_label),
            state="readonly",
            width=48,
        )
        self.migrate_from_box.grid(row=0, column=1, sticky=tk.EW, padx=(6, 12))
        ttk.Label(migration, text="Vers :").grid(row=0, column=2, sticky=tk.W)
        self.migrate_to_box = ttk.Combobox(
            migration,
            textvariable=self.migrate_to_var,
            values=list(self.profile_by_label),
            state="readonly",
            width=48,
        )
        self.migrate_to_box.grid(row=0, column=3, sticky=tk.EW, padx=(6, 12))
        ttk.Button(
            migration,
            text="Transférer les données",
            command=self.transfer_profile_data,
        ).grid(row=0, column=4)
        migration.columnconfigure(1, weight=1)
        migration.columnconfigure(3, weight=1)
        ttk.Label(
            migration,
            text=(
                "Copie mots de passe, cookies, favoris/historique et formulaires. "
                "Les extensions et préférences ne sont pas copiées."
            ),
        ).grid(row=1, column=0, columnspan=5, sticky=tk.W, pady=(8, 0))

        ttk.Label(
            root,
            text=(
                "Les extensions, marque-pages, mots de passe, cookies et données de sites "
                "ne sont pas supprimés."
            ),
            padding=(10, 0, 10, 8),
        ).pack(fill=tk.X)
        ttk.Label(root, textvariable=self.summary, padding=(10, 0, 10, 8)).pack(fill=tk.X)

        columns = ("action", "size", "category", "reason", "path")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=18)
        for column, title, width in (
            ("action", "Action", 85),
            ("size", "Taille", 90),
            ("category", "Catégorie", 100),
            ("reason", "Raison", 360),
            ("path", "Chemin", 600),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        labels = list(self.profile_by_label)
        if labels:
            self.migrate_from_var.set(labels[0])
        if len(labels) > 1:
            self.migrate_to_var.set(labels[1])
        self.scan()

    @staticmethod
    def profile_label(profile: FirefoxProfile) -> str:
        default = " [défaut]" if profile.is_default else ""
        return f"{profile.name}{default} - {profile.path}"

    def selected_profile(self) -> FirefoxProfile | None:
        return self.profile_by_label.get(self.profile_var.get())

    def label_for_path(self, path: Path | None) -> str:
        if path is None:
            return ""
        return next(
            (
                label
                for label, profile in self.profile_by_label.items()
                if profile.path.resolve(strict=False) == path.resolve(strict=False)
            ),
            "",
        )

    def reload_profiles(
        self,
        selected_path: Path | None = None,
        migrate_from: Path | None = None,
        migrate_to: Path | None = None,
        profiles: list[FirefoxProfile] | None = None,
    ) -> None:
        self.profiles = profiles if profiles is not None else discover_profiles()
        self.profile_by_label = {
            self.profile_label(profile): profile for profile in self.profiles
        }
        labels = list(self.profile_by_label)
        self.profile_box.configure(values=labels)
        self.migrate_from_box.configure(values=labels)
        self.migrate_to_box.configure(values=labels)
        selected_label = self.label_for_path(selected_path) or next(
            iter(self.profile_by_label), ""
        )
        self.profile_var.set(selected_label)
        previous_from = self.migrate_from_var.get()
        previous_to = self.migrate_to_var.get()
        from_label = self.label_for_path(migrate_from)
        if not from_label and previous_from in self.profile_by_label:
            from_label = previous_from
        to_label = self.label_for_path(migrate_to)
        if not to_label and previous_to in self.profile_by_label:
            to_label = previous_to
        self.migrate_from_var.set(
            from_label or next(iter(self.profile_by_label), "")
        )
        self.migrate_to_var.set(to_label)
        self.scan()

    def find_profiles(self) -> None:
        selected = self.selected_profile()
        self.summary.set("Recherche des profils Firefox en cours...")
        self.root.update_idletasks()
        profiles = find_firefox_profiles(known_profiles=self.profiles)
        found_count = len(profiles) - len(self.profiles)
        self.reload_profiles(
            selected_path=selected.path if selected else None,
            profiles=profiles,
        )
        if found_count:
            messagebox.showinfo(
                "Recherche terminée",
                f"{found_count} profil(s) Firefox supplémentaire(s) trouvé(s).",
            )
        else:
            messagebox.showinfo(
                "Recherche terminée",
                "Aucun profil Firefox supplémentaire trouvé.",
            )

    def create_profile(self) -> None:
        if firefox_processes():
            messagebox.showerror(
                "Firefox est ouvert",
                "Ferme complètement Firefox avant de créer un profil.",
            )
            return
        name = simpledialog.askstring(
            "Nouveau profil",
            "Nom du profil persistant :",
            initialvalue="42-persistent",
            parent=self.root,
        )
        if name is None:
            return
        make_default = messagebox.askyesno(
            "Profil par défaut",
            "Définir ce nouveau profil comme profil Firefox par défaut ?",
        )
        source_profile = self.selected_profile()
        try:
            result = create_firefox_profile(name, make_default=make_default)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            messagebox.showerror("Création impossible", str(error))
            return
        self.reload_profiles(
            result.profile.path,
            migrate_from=source_profile.path if source_profile else None,
            migrate_to=result.profile.path,
        )
        default_text = " et défini par défaut" if result.made_default else ""
        details = (
            f"Profil {result.profile.name!r} créé{default_text}.\n\n"
            f"Répertoire racine : {result.profile.path}"
        )
        if source_profile and messagebox.askyesno(
            "Profil créé",
            details
            + "\n\nTransférer maintenant les mots de passe, cookies, favoris "
            "et formulaires depuis l'ancien profil ?",
        ):
            self.transfer_profile_data(skip_confirmation=True)
        else:
            messagebox.showinfo("Profil créé", details)

    def transfer_profile_data(self, skip_confirmation: bool = False) -> None:
        old_profile = self.profile_by_label.get(self.migrate_from_var.get())
        new_profile = self.profile_by_label.get(self.migrate_to_var.get())
        if old_profile is None or new_profile is None:
            messagebox.showerror(
                "Profils manquants",
                "Sélectionne un profil source et un profil destination.",
            )
            return
        if old_profile.path.resolve(strict=False) == new_profile.path.resolve(strict=False):
            messagebox.showerror(
                "Migration impossible",
                "Le profil source et le profil destination doivent être différents.",
            )
            return
        if firefox_processes():
            messagebox.showerror(
                "Firefox est ouvert",
                "Ferme complètement Firefox avant le transfert.",
            )
            return

        plan = migration_plan(old_profile.path, new_profile.path)
        present = [name for name, source_exists, _ in plan if source_exists]
        replaced = [name for name, _, destination_exists in plan if destination_exists]
        details = (
            f"Depuis : {old_profile.name}\n"
            f"Vers : {new_profile.name}\n\n"
            f"Fichiers disponibles : {', '.join(present) or 'aucun'}.\n"
            "Les extensions et prefs.js ne seront pas copiés."
        )
        if replaced:
            details += (
                "\n\nLes fichiers déjà présents dans la destination seront sauvegardés : "
                + ", ".join(replaced)
                + "."
            )
        details += (
            "\n\nLes cookies récupèrent souvent les sessions, mais certains sites "
            "peuvent demander une nouvelle authentification."
        )
        if not skip_confirmation and not messagebox.askyesno(
            "Confirmer le transfert",
            details,
        ):
            return

        try:
            result = migrate_profile_data(old_profile.path, new_profile.path)
        except ValueError as error:
            messagebox.showerror("Migration impossible", str(error))
            return

        report = f"Fichiers copiés : {', '.join(result.copied) or 'aucun'}."
        if result.skipped:
            report += "\n\nIgnorés :\n" + "\n".join(result.skipped)
        if result.backup_dir:
            report += f"\n\nSauvegarde de la destination : {result.backup_dir}"
        if result.errors:
            report += "\n\nErreurs :\n" + "\n".join(result.errors)
            messagebox.showwarning("Transfert partiel", report)
        else:
            messagebox.showinfo(
                "Transfert terminé",
                report + "\n\nTu peux maintenant lancer le nouveau profil.",
            )

    def scan(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        profile = self.selected_profile()
        if profile is None:
            self.items = []
            self.summary.set("Aucun profil Firefox trouvé.")
            return

        self.items = scan_profile(profile, self.use_cache.get(), self.use_extensions.get())
        for index, item in enumerate(self.items):
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    {
                        "delete": "supprimer",
                        "backup": "sauvegarder",
                        "quarantine": "renommer",
                    }[item.action],
                    human_size(item.size),
                    item.category,
                    item.reason,
                    str(item.path),
                ),
            )
        warnings = profile_warnings(profile)
        warning_text = f" Attention: {'; '.join(warnings)}." if warnings else ""
        self.summary.set(
            f"{len(self.items)} élément(s), {human_size(sum(item.size for item in self.items))} ciblés."
            f"{warning_text}"
        )

    def repair(self) -> None:
        profile = self.selected_profile()
        if profile is None or not self.items:
            messagebox.showinfo("Rien à faire", "Aucun élément réparable trouvé.")
            return
        processes = firefox_processes()
        if processes:
            messagebox.showerror(
                "Firefox est ouvert",
                "Ferme complètement Firefox avant la réparation.\n\n"
                f"{len(processes)} processus Firefox détecté(s).",
            )
            return
        if not messagebox.askyesno(
            "Confirmation",
            f"Réparer le profil {profile.name} ?\n\n"
            "Le cache local sera renommé en .bak avant que Firefox en recrée un propre."
            + (
                "\nLes registres d'extensions seront aussi sauvegardés et reconstruits."
                if self.use_extensions.get()
                else ""
            ),
        ):
            return

        result = repair_profile(profile, self.items)
        details = (
            f"{result.quarantined} cache(s) local(aux) renommé(s), "
            f"{result.removed} petit(s) cache(s) supprimé(s), "
            f"{result.backed_up} fichier(s) sauvegardé(s), "
            f"{human_size(result.freed)} libérés."
        )
        if result.quarantine_paths:
            details += "\n\nCache(s) de secours:\n" + "\n".join(
                str(path) for path in result.quarantine_paths
            )
        if result.backup_dir:
            details += f"\n\nRegistres sauvegardés: {result.backup_dir}"
        if result.errors:
            details += "\n\nErreurs:\n" + "\n".join(result.errors)
            messagebox.showwarning("Réparation partielle", details)
        else:
            details += "\n\nRelance Firefox. Le premier démarrage peut être plus lent."
            messagebox.showinfo("Réparation terminée", details)
        self.scan()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Répare les caches et le registre des extensions Firefox."
    )
    parser.add_argument("--scan", action="store_true", help="affiche les éléments ciblés")
    parser.add_argument("--repair", action="store_true", help="effectue la réparation")
    parser.add_argument("--yes", action="store_true", help="confirme la réparation en ligne de commande")
    parser.add_argument("--all-profiles", action="store_true", help="traite tous les profils détectés")
    parser.add_argument("--profile", metavar="PATH", help="traite explicitement ce profil")
    parser.add_argument(
        "--find-profiles",
        action="store_true",
        help="recherche les profils déplacés dans le home et le goinfre",
    )
    parser.add_argument(
        "--search-root",
        action="append",
        metavar="PATH",
        help="racine à parcourir avec --find-profiles (option répétable)",
    )
    parser.add_argument("--cache-only", action="store_true", help="ne traite que les caches")
    parser.add_argument(
        "--include-extensions",
        action="store_true",
        help="reconstruit aussi le registre des extensions (option avancée)",
    )
    parser.add_argument(
        "--extensions-only",
        action="store_true",
        help="ne reconstruit que le registre des extensions",
    )
    parser.add_argument(
        "--migrate-from",
        metavar="PATH",
        help="ancien profil dont copier les données personnelles utiles",
    )
    parser.add_argument(
        "--migrate-to",
        metavar="PATH",
        help="nouveau profil propre recevant les données",
    )
    parser.add_argument(
        "--create-profile",
        metavar="NAME",
        help="crée un profil persistant sous ~/.mozilla/firefox",
    )
    parser.add_argument(
        "--no-default",
        action="store_true",
        help="ne définit pas le profil créé comme profil par défaut",
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_default and not args.create_profile:
        parser.error("--no-default nécessite --create-profile")
    if args.create_profile and (
        args.scan
        or args.repair
        or args.migrate_from
        or args.migrate_to
        or args.profile
        or args.all_profiles
        or args.find_profiles
        or args.search_root
        or args.cache_only
        or args.include_extensions
        or args.extensions_only
    ):
        parser.error("--create-profile doit être utilisé seul, avec --yes et éventuellement --no-default")
    if bool(args.migrate_from) != bool(args.migrate_to):
        parser.error("--migrate-from et --migrate-to doivent être utilisés ensemble")
    if args.cache_only and args.extensions_only:
        parser.error("--cache-only et --extensions-only sont incompatibles")
    if args.cache_only and args.include_extensions:
        parser.error("--cache-only et --include-extensions sont incompatibles")
    if args.search_root and not args.find_profiles:
        parser.error("--search-root nécessite --find-profiles")

    include_cache = not args.extensions_only
    include_extensions = args.extensions_only or args.include_extensions
    discovered_profiles = discover_profiles()
    if args.find_profiles:
        search_roots = (
            [Path(path) for path in args.search_root]
            if args.search_root
            else default_profile_search_roots()
        )
        discovered_profiles = find_firefox_profiles(search_roots, discovered_profiles)
        print("Profils Firefox détectés:")
        for profile in discovered_profiles:
            default = " [défaut]" if profile.is_default else ""
            print(f"  {profile.name}{default}: {profile.path}")
        if not discovered_profiles:
            print("  aucun")
        if not args.scan and not args.repair:
            return 0
    profiles = select_profiles(discovered_profiles, args.profile, args.all_profiles)

    if args.create_profile:
        try:
            name = valid_profile_name(args.create_profile)
        except ValueError as error:
            parser.error(str(error))
        print(
            f"Création du profil {name!r} sous {FIREFOX_DIR}."
            + ("" if args.no_default else "\nIl deviendra le profil par défaut.")
        )
        if not args.yes:
            print("\nAjoute --yes pour confirmer la création.", file=sys.stderr)
            return 2
        if firefox_processes():
            print("\nCréation refusée: Firefox est encore ouvert.", file=sys.stderr)
            return 3
        try:
            result = create_firefox_profile(name, make_default=not args.no_default)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"\nCréation impossible: {error}", file=sys.stderr)
            return 1
        print(f"\nProfil créé: {result.profile.path}")
        print(f"Profil par défaut: {'oui' if result.made_default else 'non'}")
        return 0

    if args.migrate_from and args.migrate_to:
        old_profile = Path(args.migrate_from)
        new_profile = Path(args.migrate_to)
        print(f"Migration sélective:\n  source: {old_profile}\n  destination: {new_profile}")
        for name, source_exists, destination_exists in migration_plan(old_profile, new_profile):
            source_state = "présent" if source_exists else "absent"
            backup_state = ", destination sauvegardée" if destination_exists else ""
            print(f"  {name:20} {source_state}{backup_state}")
        print("\nLes extensions et prefs.js ne seront pas copiés.")
        if not args.yes:
            print("\nAjoute --yes pour confirmer la migration.", file=sys.stderr)
            return 2
        processes = firefox_processes()
        if processes:
            print("\nMigration refusée: Firefox est encore ouvert.", file=sys.stderr)
            return 3
        try:
            result = migrate_profile_data(old_profile, new_profile)
        except ValueError as error:
            print(f"\nMigration refusée: {error}", file=sys.stderr)
            return 1
        print(f"\nFichiers copiés: {', '.join(result.copied) or 'aucun'}")
        for skipped in result.skipped:
            print(f"IGNORÉ: {skipped}")
        if result.backup_dir:
            print(f"Sauvegarde de la destination: {result.backup_dir}")
        for error in result.errors:
            print(f"ERREUR: {error}", file=sys.stderr)
        return 1 if result.errors else 0

    if args.scan or args.repair:
        if not profiles:
            print("Aucun profil Firefox trouvé.", file=sys.stderr)
            return 1
        print_scan(profiles, include_cache, include_extensions)
        if not args.repair:
            return 0
        if not args.yes:
            print("\nAjoute --yes pour confirmer la réparation.", file=sys.stderr)
            return 2
        processes = firefox_processes()
        if processes:
            print("\nRéparation refusée: Firefox est encore ouvert.", file=sys.stderr)
            for pid, command in processes[:5]:
                print(f"  PID {pid}: {command}", file=sys.stderr)
            if len(processes) > 5:
                print(
                    f"  ... et {len(processes) - 5} autre(s) processus Firefox.",
                    file=sys.stderr,
                )
            return 3

        failed = False
        for profile in profiles:
            items = scan_profile(profile, include_cache, include_extensions)
            result = repair_profile(profile, items)
            print(
                f"\n{profile.name}: {result.quarantined} cache(s) local(aux) renommé(s), "
                f"{result.removed} petit(s) cache(s) supprimé(s), "
                f"{result.backed_up} registre(s) sauvegardé(s), "
                f"{human_size(result.freed)} libérés."
            )
            for path in result.quarantine_paths:
                print(f"Cache de secours: {path}")
            if result.backup_dir:
                print(f"Registres sauvegardés: {result.backup_dir}")
            for error in result.errors:
                failed = True
                print(f"ERREUR: {error}", file=sys.stderr)
        return 1 if failed else 0

    if tk is None:
        print("Tkinter n'est pas disponible. Utilise --scan ou --repair --yes.", file=sys.stderr)
        return 1
    root = tk.Tk()
    root.geometry("1300x620")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
