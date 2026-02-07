## Critical Fixes (security / data loss)

- [ ] **Rate-limit `.auth` attempts** (`core/irc_client.py`): no throttle on password guesses — add a per-nick cooldown or exponential backoff to prevent brute-force attacks on owner auth.
- [ ] **Fix owner-record case-variant duplicate bypass** (`core/irc_client.py`): `_load_owner_records` normalizes to lowercase *after* the duplicate check, so `"Alice"` and `"alice"` can both be added. Normalize before checking.
- [ ] **Enforce TLS hostname verification** (`core/irc_client.py`): pass `server_hostname=self.server` to `asyncio.open_connection` when TLS is enabled so certificates are actually hostname-checked.
- [ ] **Enable WAL mode for SQLite** (`scripts/log.py`): database is opened with `check_same_thread=False` but no WAL — concurrent async writes can corrupt. Enable `PRAGMA journal_mode=WAL` at connection time.
- [ ] **Fix reminder timezone bug** (`scripts/remindme.py`): `datetime.now()` uses local time; if the host changes timezone the schedule breaks. Use `datetime.now(timezone.utc)` everywhere and store/compare in UTC.
- [ ] **Prevent reminder loss on send failure** (`scripts/remindme.py`): reminder is popped from memory *before* the IRC send — if the send fails the reminder is gone. Remove only after confirmed delivery.
- [ ] **DNS rebinding / SSRF hardening** (`scripts/extract_url.py`): hostname is resolved once for the public-IP check, but the HTTP client may re-resolve to a different (private) IP. Pin the resolved address for the actual request, or re-validate after connection.

## Bugs

- [ ] **Nickname collision not persisted** (`core/irc_client.py`): on 433 the bot appends `_` to its nick, but the running config is not updated — `.health` and reconnect logic still reference the old nick.
- [ ] **Plugin disable state inconsistent on load failure** (`core/plugin_manager.py`): if `_load_plugin()` raises, the plugin is removed from `_disabled_plugins` but never actually loaded, leaving it in limbo.
- [ ] **`tell.py` delivery lock never cleared on crash** (`scripts/tell.py`): `state.delivering` set blocks re-delivery, but if the bot crashes mid-delivery the flag is never removed. Use a timeout or task-scoped guard instead.
- [ ] **`seen.py` persistence not locked** (`scripts/seen.py`): `_persist_entries()` writes JSON without `file_lock`; concurrent saves can truncate the file.
- [ ] **Duplicate exception handler** (`scripts/stock.py`): the same `except` block appears twice in sequence — dead code that should be collapsed.
- [ ] **Queue loss on reconnect** (`core/irc_client.py`): `_send_queue` is recreated on disconnect while the send task may still be draining the old queue, silently dropping pending messages.

## Needs to Have

- [ ] **Graceful plugin task teardown** (`core/plugin_manager.py`): track spawned handler tasks and `cancel`/`await` them on unload/reload/shutdown so stray tasks don't run with stale state.
- [ ] **Bound plugin task fan-out** (`core/plugin_manager.py`): per-message handler tasks are unbounded — add a semaphore or drop policy to cap concurrency during floods.
- [ ] **Move secrets to env vars**: API keys for ChatGPT, YouTube, and the hardcoded Algolia credentials in `untappd.py` are in source/config. Load from environment variables and remove from committed files.
- [ ] **Config schema validation** (`bot.py`, `core/plugin_manager.py`): validate config at startup and after hot-reloads (pydantic, voluptuous, or manual) to fail fast with clear error messages.
- [ ] **Lock config during read-modify-write** (`core/plugin_manager.py`): `_apply_config_defaults()` reads and writes config outside the file lock — concurrent plugin loads can clobber changes.
- [ ] **Persistent plugin state helper** (`core/utils.py` or new module): provide a per-plugin state-storage API (JSON/YAML, cached + atomic writes) so plugins stop rolling their own file I/O.
- [ ] **Per-channel plugin toggles** (`core/plugin_manager.py`, config): allow enabling/disabling plugins per channel without unloading them globally.
- [ ] **Admin audit trail** (`core/irc_client.py`): log all admin actions (`.auth`, `.say`, `.join/.part`, plugin load/unload) to a configured channel or log file for incident review.

## Nice to Have

- [ ] **Cross-platform config locking** (`core/utils.py`): `file_lock` uses `fcntl` only — add a Windows fallback (portalocker / `msvcrt`) for portability.
- [ ] **Expose tuning knobs in config** (`config_sample.yaml`): surface `max_backoff`, join delay, rate-limiter settings, and plugin semaphore size so operators can tune without code changes.
- [ ] **Metrics / visibility** (`core/irc_client.py`, `core/plugin_manager.py`): lightweight counters (messages processed, plugin failures/timeouts, reconnects, queue depth) via logs or optional Prometheus text endpoint.
- [ ] **Plugin test harness and CI** (repo root): fake-bot fixture + sample-message tests; wire ruff/mypy in CI to catch regressions early.
- [ ] **Standardize HTTP plugin defaults** (`scripts/*`): shared timeout, user-agent, and optional dry-run mode across all plugins that make HTTP requests.
- [ ] **Replace regex HTML parsing** (`scripts/instagram.py`, `scripts/twitter.py`): use BeautifulSoup/lxml instead of fragile regexes for scraping metadata — less likely to break on upstream HTML changes.
- [ ] **Limit SQLite database growth** (`scripts/log.py`): add `PRAGMA auto_vacuum` or a retention policy so the log database doesn't grow unbounded.
- [ ] **ChatGPT per-channel history isolation** (`scripts/chatgpt.py`): history is keyed by channel name only — if the bot runs on multiple networks, histories for identically-named channels will mix.
- [ ] **Configurable system prompt for ChatGPT** (`scripts/chatgpt.py`): the system prompt is hardcoded in Swedish; make it a config option.
- [ ] **Validate `.auth` password strength** (`core/irc_client.py`): enforce a minimum length and reject empty/trivial passwords when owner records are created.

## Done

- [x] Non-blocking plugin dispatch (`core/plugin_manager.py`)
- [x] Harden URL previews — SSRF, byte budget, scheme vetting (`scripts/extract_url.py`)
- [x] Bound outbound send queue (`core/irc_client.py`)
- [x] Atomic, synchronized config writes (`core/irc_client.py`, `scripts/ignore.py`)
- [x] Command router and help system (`core/plugin_manager.py`)
- [x] Per-target rate limits (`core/irc_client.py`)
- [x] Health/status command (`core/irc_client.py`)
