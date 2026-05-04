# Cache Cleaners

Deux petits outils Python/Tkinter pour inspecter et nettoyer des caches locaux sans supprimer à l'aveugle.

## Scripts

### `CodeCacheCleaner.py`

Nettoie certains caches de Visual Studio Code :

- `~/.config/Code/CachedExtensionVSIXs`
- `~/.config/Code/WebStorage`
- optionnellement `~/.vscode/extensions`

Fonctions principales :

- détecte les anciennes versions de caches/extensions ;
- affiche taille, type, version, chemin ;
- permet de cocher des lignes manuellement ;
- garde les versions les plus récentes ;
- avertit si les extensions installées sont incluses.

Lancer l'interface :

```bash
python3 CodeCacheCleaner.py
```

Ligne de commande :

```bash
python3 CodeCacheCleaner.py --scan
python3 CodeCacheCleaner.py --delete-old
python3 CodeCacheCleaner.py --delete-all
python3 CodeCacheCleaner.py --scan --include-installed
```

## `MozillaFirefoxCleaner.py`

Inspecte le stockage local Firefox :

```text
~/.mozilla/firefox/wl9bf19c.default-release/storage
```

Fonctions principales :

- décode les origines Firefox (`https+++...`, `partitionKey`, `moz-extension+++...`) ;
- affiche le site concerné, la partition, le type de données, la taille et la date ;
- classe les éléments : site web, cache, tiers partitionné, pub/traceur, app sensible, extension, interne Firefox ;
- protège les données internes Firefox et les extensions, dont Tampermonkey ;
- permet de cocher par catégorie, date, taille ou manuellement.

Lancer l'interface :

```bash
python3 MozillaFirefoxCleaner.py
```

Ligne de commande :

```bash
python3 MozillaFirefoxCleaner.py --scan
```

## Conseils

- Fermer VS Code ou Firefox avant de supprimer des données.
- Préférer le scan puis les lignes cochées plutôt que `--delete-all`.
- Pour Firefox, vérifier les lignes marquées `sensible` avant suppression : elles peuvent contenir des données de session/app web.
- Les scripts ne modifient rien au scan. La suppression demande une action explicite.
