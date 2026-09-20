# TrabBit Toloka Seed Manager v2.0

Модульний менеджер роздач для qBittorrent + Toloka RSS.

## Структура

```text
TrabBit/
├── main.py
├── trabbit/
│   ├── config.py
│   ├── db.py
│   ├── manager.py
│   ├── models.py
│   ├── qbit.py
│   ├── rules.py
│   ├── toloka.py
│   └── torrent.py
├── config/
│   └── rules.json
├── .env
├── .env.example
├── requirements.txt
└── manager.db
```

## Встановлення

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Заповни `QBIT_PASSWORD`, `TOLOKA_USERNAME`, `TOLOKA_PASSWORD`.

## Звичайний запуск

```powershell
python main.py
```

## Watchlist

```powershell
python main.py --watch-add 699050
python main.py --watch-list
python main.py --watch-remove 699050
```

За замовчуванням тема у watchlist перевіряється раз на 24 години. Глобальні налаштування `WATCHLIST_AUTO_ADD` і `WATCHLIST_AUTO_UPDATE` можна змінити в `.env`.

## Приведення старих торентів до нової системи

Після оновлення qBittorrent у тебе можуть залишитися старі торенти з єдиною категорією. Один раз запусти:

```powershell
python main.py --retag-existing
```

У `DRY_RUN=true` він лише покаже, що змінить. Після перевірки постав `DRY_RUN=false` і повтори команду.

## Що робить v2.0

- Читає Toloka RSS.
- Авторизується на Toloka перед перевіркою тем.
- Визначає `.torrent`, fingerprint, info hash та розмір.
- Перевіряє фактичну наявність info hash у qBittorrent, а не лише запис у SQLite.
- Зберігає стан у SQLite.
- Має окремий watchlist з пріоритетом, власним інтервалом перевірки та auto-add/auto-update.
- Має глобальний ліміт сховища та warning-поріг.
- Автоматично визначає category та tags за `config/rules.json`.
- Додає службовий tag `trabbit` до власних роздач.
- Має `--retag-existing` для старих роздач.
- `DRY_RUN` не записує торенти як реально додані у qBittorrent.
- Безпечніше переживає `429 Too Many Requests`.

## Категорії та теги

Категорії використовуються як логічні мітки qBittorrent, а всі нові файли за замовчуванням залишаються у твоєму існуючому корені `D:\Torent\Toloka`. Це зроблено навмисно, щоб зміна категорії не розкладала вже завантажені файли по нових підпапках. За потреби шляхи категорій можна змінити у `config/rules.json`.

А qBittorrent отримає теги на кшталт:

```text
toloka
anime
ukr
1080p
bdrip
hevc
ongoing
watchlist
priority-high
s01
```

Правила редагуються без зміни Python-коду у `config/rules.json`.

## Важливо про оновлення torrent

`AUTO_UPDATE=true` додає нову версію оновленого torrent, але `REMOVE_OLD_ON_UPDATE=false` за замовчуванням навмисно залишає стару роздачу. Автоматично видаляти стару роздачу з файлами небезпечно. Спочатку перевір систему в такому режимі.


## Retag safety

TrabBit does not retag every qBittorrent torrent. `--retag-existing` only processes torrents that are already known to TrabBit, carry `MANAGED_TAG`, or are still in a legacy managed category listed in `LEGACY_MANAGED_CATEGORIES`.

`IGNORE_TAG` always wins. Add `trabbit-ignore` to unrelated torrents (Linux ISOs, personal downloads, etc.) and TrabBit will leave them untouched even if they still use the old `TolokaSeed` category.

Example:

```env
MANAGED_TAG=trabbit
IGNORE_TAG=trabbit-ignore
LEGACY_MANAGED_CATEGORIES=TolokaSeed
```

## Topic-page metadata (v2.1)

TrabBit now parses the Toloka topic page that it already needs to visit for the torrent download link. The page is used once per topic and can contribute structured metadata to classification, including:

- age restriction (18+)
- genres, country, studio and director
- episode progress and duration
- quality, video codec, resolution and bitrate
- audio languages, translation type and codecs
- subtitle languages, types and formats
- source and translator

This information is converted into additional tags such as `18plus`, `genre-comedy`, `country-japan`, `studio-asahi-production`, `audio-ukr`, `dub-multivoice`, `sub-ass`, `source-anitube-in-ua` and `translator-aliceinhunterlnd` when the corresponding values are present.

Use the diagnostic command before enabling broad automation:

```powershell
python main.py --analyze-topic 699103
```

The command prints the extracted metadata and the resulting category/tags without adding the torrent to qBittorrent.


### Deep retagging

Preview deep retagging without changing qBittorrent:

```powershell
python main.py --retag-existing --deep --dry-run
```

Deep mode rereads the saved Toloka topic for each managed torrent that has a `topic_id` in SQLite, rebuilds TrabBit-owned tags, and preserves unrelated user tags. Explicit `trabbit-ignore` always wins.

Apply the changes:

```powershell
python main.py --retag-existing --deep
```

Deep retagging keeps the normal Toloka request delay and additionally pauses after every `DEEP_RETAG_BATCH_SIZE` topics. The defaults are 20 topics and 30 seconds.
