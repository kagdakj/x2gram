# Instagram Auto Uploader from X reposts

Watches an X/Twitter account's reposts (and an optional local `incoming/` folder) and uploads them to Instagram
with [instagrapi](https://github.com/subzeroid/instagrapi). Images outside Instagram's aspect ratio are cropped
around anime faces ([hysts/anime-face-detector](https://github.com/hysts/anime-face-detector)).

Everything is driven from one place: a **TUI control center** (`python run.py`) or the equivalent **CLI** commands.

---

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pipx install twitter-cli            # X source

cp config.example.yaml config.yaml  # fill in Instagram + X credentials
python run.py doctor                # validate config and test logins
python run.py                       # open the TUI
```

## How it works

```
 sources ──fetch──▶  posts table  ──upload──▶  Instagram
 (X, incoming/)      queued → uploading → uploaded
                                       ↘ failed  (retry)
                     skipped  (first-run baseline / manually skipped)
```

1. **Fetch** polls the sources and stores new posts as `queued`.
   On the very first poll existing reposts are stored as `skipped` so your whole history isn't uploaded at once
   (`initial_skip`). A **deep fetch** queues the backlog (`twitter.initial_load` posts) instead.
2. **Upload** stages media in a temp dir, runs the plugins (crop → caption → watermark) and publishes.
   Every upload path (daemon, TUI, `queue upload`, `sync`) goes through the same publisher, so random delays,
   daily limits and the Instagram action-block pause always apply (limits can be overridden explicitly with *force*).
3. The **daemon** repeats fetch → upload up to `uploads_per_cycle` posts → sleep.
4. **Sync** compares your Instagram profile with recent X reposts, marks posts it finds as uploaded,
   and lets you queue or upload what's missing.

Failed posts keep their error message and attempt count; retry them any time.

## TUI

`python run.py` (or `python run.py tui`)

| Key | Action |
|---|---|
| `s` | start / stop the daemon |
| `f` / `F` | fetch now / deep fetch |
| `space`, `a` | select row, select all/none |
| `u` | upload selected rows (or the row under the cursor) |
| `r` / `k` / `delete` | retry / skip / remove selected |
| `x` | stop the running upload batch |
| `1`–`4` | Queue, Sync, History, Log tabs |
| `?` | help |

The Queue tab shows details and a caption preview for the highlighted post. Logs go to the Log tab and
`data/uploader.log`.

## CLI

```bash
python run.py run                    # daemon (Ctrl+C to stop)
python run.py run --once             # one poll + upload cycle
python run.py run --local            # also watch incoming/

python run.py queue list             # queued + failed (use -s uploaded / -s skipped ...)
python run.py queue fetch [--deep] [--dry-run]
python run.py queue upload ID... | -i 1-3,5 | --next 2   [--force] [-y]
python run.py queue retry [ID...]    # all failed when no IDs
python run.py queue skip ID...
python run.py queue remove ID...
python run.py queue clear [-s failed]

python run.py sync [--dry-run] [--select 1-3] [--queue] [--include-uploaded]
python run.py history [--all]
python run.py stats
python run.py doctor [--offline]
python run.py reset [--restore-files] [--logout]   # --logout also drops the device fingerprint

python run.py tools crop art.png -o art_ig.jpg --ratio auto
python run.py tools fix-captions --limit 30
python run.py tools plugins
```

### Local folder

With `local.enabled: true` (or `run --local`), drop files into `incoming/`:

- a single image/video → one post (`art.txt` next to it becomes the caption)
- a folder of images → one carousel (`folder/caption.txt` for the caption)

After a successful upload the files move to `data/processed/YYYY-MM-DD/`.

## Staying under Instagram's radar

The uploader tries to look like one phone posting at a human pace:

- **One device, forever.** The saved session (`data/instagram_session.json`) holds the device fingerprint
  (uuids, device model, user agent). When the session expires it is re-authenticated *with the same device*,
  never replaced. `reset` keeps it too; only `reset --logout` drops it (a backup is kept).
- **Current app version.** Instagram rejects logins from an app version it considers outdated
  (`Your version of Instagram is out of date`). The stored session's app profile is refreshed from
  instagrapi on load, keeping the uuids and phone model — the same phone with an updated app.
  Inspect it with `python run.py tools session`. Keep instagrapi itself up to date.
- **Request pacing.** `instagram.request_delay_min/max` makes instagrapi wait between individual API calls.
- **Upload pacing.** Random delay between posts, daily cap, minimum interval, and a 6h pause when Instagram
  answers `feedback_required`.
- **Gentle caption repair.** If Instagram strips a caption it is retried once, then left for
  `tools fix-captions` instead of editing the fresh post repeatedly.

Optional, **off by default** (enable in `config.yaml`, they need `rate_limit_enabled: true`):

| Setting | Example | Effect |
|---|---|---|
| `service.active_hours` | `"10:00-23:00"` | no uploads outside the window (may wrap midnight) |
| `service.interval_jitter` | `0.8` | gaps become 1.0x–1.8x `min_post_interval` instead of fixed |
| `service.daily_limit_jitter` | `1` | daily cap `max_posts_per_day ± 1`, fixed per day |
| `service.rest_day_probability` | `0.15` | ~15% of days upload nothing |
| `plugins.caption.hashtag_count` | `[4, 8]` | random subset of the hashtag pool, shuffled |
| `plugins.caption.source_link_probability` | `0.5` | only some posts carry the `Source:` line |
| `plugins.caption.vary_templates` | `true` | pick one of the configured templates per post |

Caption randomness is seeded with the post id, so a retry rebuilds exactly the same caption.

### Caption format

`plugins.caption.templates` is a list of format strings; `credit_format`, `source_format` and `tags_format`
shape the individual blocks:

```yaml
plugins:
  caption:
    templates:
      - "{credit}{nl}{nl}{text}{nl}{nl}{source}{nl}{nl}{tags}"
      - "{text}{nl}{nl}by #{author_tag}{nl}{nl}{hashtags}"
    credit_format: "Artist: {author_name} (#{author_tag}) on X"
    source_format: "Source: {url_plain}"
    tags_format: ".{nl}.{nl}{hashtags}"
```

Placeholders: `{credit}` `{text}` `{source}` `{tags}` `{hashtags}` `{author}` `{author_tag}` `{author_name}`
`{url}` `{url_plain}` `{post_id}` `{nl}`. Empty parts drop out together with the blank lines around them,
so a template can mention every placeholder safely. With several templates, `vary_templates: true` picks one
per post (seeded by post id); otherwise the first one is used.

Preview before uploading anything:

```bash
python run.py tools caption                 # a few queued posts
python run.py tools caption 2100241060366881177 --all-templates
```

If your device locale/timezone don't match the IP you connect from, set `instagram.locale`, `country`,
`country_code` and `timezone_offset` (or use a matching `proxy`) — but pick one and never change it again.

None of this makes automated reposting undetectable. Posting less is what actually helps.

## Configuration

See `config.example.yaml` for every option. Secrets can also come from `.env` / environment variables
(`.env.example`). Older configs keep working: `post_delay` is read as `post_delay_min`.

The v1 database (`seen_posts` + `upload_queue`) is migrated automatically on first start; the old tables are
kept as `legacy_*`.

## Project layout

```
run.py                     entry point (Typer CLI, no args → TUI)
auto_uploader/
├── cli.py                 CLI commands (thin wrappers around services)
├── tui/                   Textual app, widgets, dialogs
├── context.py             builds config, store and services (lazily)
├── store.py               SQLite: posts table + metrics, legacy migration
├── models.py              Post, MediaItem, PostStatus, UploadResult
├── config.py              pydantic config, .env loading
├── services/
│   ├── ingest.py          fetch sources → store (baseline / deep)
│   ├── publisher.py       the only upload path
│   ├── policy.py          delays, rate limits, action-block backoff
│   ├── daemon.py          fetch/upload loop
│   ├── sync.py            Instagram ↔ X comparison
│   ├── maintenance.py     doctor, caption repair, reset
│   ├── media.py           download / stage media
│   └── events.py          event bus used by the TUI
├── sources/               twitter_cli, local_folder
├── uploader/              instagrapi wrapper
└── plugins/               anime_crop, caption, watermark
tests/
```

## Custom plugins

```python
from auto_uploader.plugins.base import BasePlugin, register_plugin

@register_plugin("my_filter")
class MyFilter(BasePlugin):
    description = "Adds a footer."
    priority = 60  # lower runs earlier

    def process_caption(self, caption, post):
        return f"{caption}\n\nposted by bot"
```

Import the module in `auto_uploader/plugins/__init__.py` so it registers, and configure it under
`plugins.custom.<name>` in `config.yaml`.

## Tests

```bash
pytest tests/ -q
```
