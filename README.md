# Cache Cleaners

Trois outils Python/Tkinter pour inspecter, réparer et nettoyer des caches locaux sans supprimer à l'aveugle.

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

## `MozillaFirefoxCacheBugfix.py`

Répare les corruptions Firefox qui peuvent apparaître quand un même profil utilisateur
est utilisé sur plusieurs postes :

- détecte automatiquement les profils via `profiles.ini` ;
- renomme le cache local du poste en `.bak-<date>` pour permettre un retour arrière ;
- supprime uniquement les petits caches régénérables présents dans le profil ;
- peut reconstruire, en option avancée, le registre local des extensions sans supprimer
  les XPI ni leurs données ;
- sauvegarde les petits fichiers de registre avant de les retirer ;
- refuse toute réparation tant que Firefox est ouvert ;
- signale les chemins d'extensions invalides et les copies `prefs-N.js`.

Lancer l'interface :

```bash
python3 MozillaFirefoxCacheBugfix.py
```

Diagnostic en ligne de commande :

```bash
python3 MozillaFirefoxCacheBugfix.py --scan
python3 MozillaFirefoxCacheBugfix.py --scan --all-profiles
```

Réparation après avoir complètement fermé Firefox :

```bash
python3 MozillaFirefoxCacheBugfix.py --repair --yes
python3 MozillaFirefoxCacheBugfix.py --repair --yes --include-extensions
python3 MozillaFirefoxCacheBugfix.py --repair --yes --extensions-only
```

Par défaut, seule la réparation éprouvée du cache local au poste est appliquée. Utiliser
`--include-extensions` uniquement si les extensions restent invisibles ou inactives après
la régénération du cache.

Si Firefox affiche `Installation aborted because the add-on appears to be corrupt` même
après cette réparation, fermer Firefox puis tester l'installation avec un profil jetable :

```bash
test_profile="$(mktemp -d)"
firefox --no-remote --profile "$test_profile"
```

Si l'installation fonctionne dans ce profil, le profil habituel est en cause. Si elle
échoue encore, vérifier plutôt Firefox, le réseau ou le téléchargement du fichier XPI.

Lorsque le profil temporaire fonctionne mais que la reconstruction du registre ne suffit
pas, ouvrir `about:profiles`, créer ou conserver le profil propre et le définir comme
profil par défaut. Ne pas supprimer immédiatement l'ancien profil : il peut encore
contenir favoris, mots de passe et données locales d'extensions. Faire une sauvegarde de
son répertoire racine et, lors d'un retrait depuis le gestionnaire, préférer
`Don't Delete Files`.

Firefox utilise toujours un profil, même sans compte Firefox Sync et sans profil créé
manuellement. `Original Profile` désigne simplement le profil généré automatiquement au
premier lancement ; la connexion à Sync est indépendante.

Le répertoire racine du nouveau profil doit se trouver dans le `/home` persistant de
l'utilisateur. Le répertoire local, qui contient le cache, peut rester dans `goinfre`.

Après avoir lancé le nouveau profil au moins une fois puis fermé complètement Firefox,
les données essentielles peuvent être migrées sans recopier la corruption :

```bash
python3 MozillaFirefoxCacheBugfix.py \
  --migrate-from "$HOME/.mozilla/firefox/ANCIEN_PROFIL" \
  --migrate-to "$HOME/.mozilla/firefox/NOUVEAU_PROFIL" \
  --yes
```

La migration copie uniquement `logins.json` avec `key4.db`, `cookies.sqlite`,
`places.sqlite`, `favicons.sqlite` et `formhistory.sqlite`. Elle sauvegarde les fichiers
déjà présents dans le nouveau profil. Elle ne copie ni `prefs.js`, ni les extensions, ni
leur registre. Certaines sessions web peuvent malgré tout demander une reconnexion.

Références Mozilla :

- [Réparer les fichiers d'extensions corrompus](https://support.mozilla.org/en-US/kb/unable-install-add-ons-extensions-or-themes)
- [Créer, sélectionner ou retirer un profil](https://support.mozilla.org/en-US/kb/profile-manager-create-remove-switch-firefox-profiles)
- [Sauvegarder et restaurer un profil](https://support.mozilla.org/en-US/kb/back-and-restore-information-firefox-profiles)

## Conseils

- Fermer VS Code ou Firefox avant de supprimer ou réparer des données.
- Préférer le scan puis les lignes cochées plutôt que `--delete-all`.
- Pour Firefox, vérifier les lignes marquées `sensible` avant suppression : elles peuvent contenir des données de session/app web.
- Les scripts ne modifient rien au scan. La suppression demande une action explicite.
