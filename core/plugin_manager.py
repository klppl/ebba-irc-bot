import asyncio
import functools
import importlib
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List, Optional, Set, Tuple

import yaml

HANDLER_TIMEOUT_SECS = 10


@dataclass
class CommandSpec:
    plugin: str
    name: str
    aliases: Set[str]
    help_text: str
    handler: Callable


class PluginManager:
    """Load, unload, and dispatch events to message plugins."""

    def __init__(
        self,
        plugin_dir: Path,
        logger: Optional[logging.Logger] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.plugin_dir = plugin_dir
        self.logger = logger or logging.getLogger("PluginManager")
        self._plugins: Dict[str, ModuleType] = {}
        self._disabled_plugins: Set[str] = set()
        self._known_plugins: Set[str] = set()
        self._config_path = config_path
        self._apply_config_disabled_preferences()
        self._commands: Dict[str, CommandSpec] = {}
        self._plugin_commands: Dict[str, Set[str]] = {}
        self._plugin_tasks: Dict[str, Set[asyncio.Task]] = {}
        self._max_concurrent_tasks = 100
        self._task_semaphore = asyncio.Semaphore(self._max_concurrent_tasks)

    def list_plugins(self) -> List[str]:
        return sorted(self._plugins.keys())

    def list_plugin_status(self) -> Tuple[List[str], List[str]]:
        enabled = sorted(self._plugins.keys())
        disabled = sorted(self._disabled_plugins)
        return enabled, disabled

    def module_name(self, plugin_name: str) -> str:
        return f"scripts.{plugin_name}"

    def _load_module(self, plugin_name: str) -> ModuleType:
        plugin_path = self.plugin_dir / f"{plugin_name}.py"
        if not plugin_path.exists():
            raise FileNotFoundError(f"Plugin '{plugin_name}' does not exist at {plugin_path}")

        module_name = self.module_name(plugin_name)
        importlib.invalidate_caches()
        pycache_dir = self.plugin_dir / "__pycache__"
        if pycache_dir.exists():
            for pyc in pycache_dir.glob(f"{plugin_name}.cpython-*.pyc"):
                try:
                    pyc.unlink()
                except OSError:
                    self.logger.debug("Could not remove pycache file %s", pyc)
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(module_name, plugin_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for plugin '{plugin_name}'")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    def load(self, plugin_name: str, bot, refresh_config: bool = False) -> None:
        if refresh_config:
            self._refresh_bot_config(bot)

        if plugin_name in self._plugins:
            raise RuntimeError(f"Plugin '{plugin_name}' is already loaded")

        module = self._load_module(plugin_name)
        try:
            self._apply_config_defaults(bot, plugin_name, module)
            on_load = getattr(module, "on_load", None)
            if callable(on_load):
                on_load(bot)
        except Exception:
            module_name = getattr(module, "__name__", self.module_name(plugin_name))
            sys.modules.pop(module_name, None)
            raise
        else:
            self._plugins[plugin_name] = module
            self._known_plugins.add(plugin_name)
            if plugin_name in self._disabled_plugins:
                self._disabled_plugins.discard(plugin_name)
            self._set_plugin_enabled_flag(bot, plugin_name, True)
            self.logger.info("Loaded plugin '%s'", plugin_name)

    def unload(self, plugin_name: str, bot) -> None:
        module = self._plugins.pop(plugin_name, None)
        if module is None:
            raise RuntimeError(f"Plugin '{plugin_name}' is not loaded")

        self._unregister_commands_for_plugin(plugin_name)
        on_unload = getattr(module, "on_unload", None)
        if callable(on_unload):
            try:
                on_unload(bot)
            except Exception:
                self.logger.exception("Error in on_unload for plugin '%s'", plugin_name)

        module_name = getattr(module, "__name__", self.module_name(plugin_name))
        sys.modules.pop(module_name, None)
        self._disabled_plugins.add(plugin_name)
        self._set_plugin_enabled_flag(bot, plugin_name, False)
        
        # Cancel active tasks for this plugin
        tasks = self._plugin_tasks.pop(plugin_name, set())
        for task in tasks:
            if not task.done():
                task.cancel()
        
        self.logger.info("Unloaded plugin '%s'", plugin_name)

    def reload(self, plugin_name: str, bot) -> None:
        self._refresh_bot_config(bot)
        self.unload(plugin_name, bot)
        self.load(plugin_name, bot)

    def dispatch_message(self, bot, user: str, channel: str, message: str) -> None:
        loop = asyncio.get_running_loop()
        for name, module in list(self._plugins.items()):
            handler = getattr(module, "on_message", None)
            if not callable(handler):
                self.logger.warning(
                    "Plugin '%s' lacks on_message handler; skipping dispatch", name
                )
                continue
            self._spawn_task(
                name,
                self._run_handler(handler, name, "on_message", bot, user, channel, message),
                f"plugin-{name}-on_message",
            )

    def dispatch_join(self, bot, user: str, channel: str) -> None:
        loop = asyncio.get_running_loop()
        for name, module in list(self._plugins.items()):
            handler = getattr(module, "on_join", None)
            if not callable(handler):
                continue
            self._spawn_task(
                name,
                self._run_handler(handler, name, "on_join", bot, user, channel),
                f"plugin-{name}-on_join",
            )

    def dispatch_part(self, bot, user: str, channel: str) -> None:
        loop = asyncio.get_running_loop()
        for name, module in list(self._plugins.items()):
            handler = getattr(module, "on_part", None)
            if not callable(handler):
                continue
            self._spawn_task(
                name,
                self._run_handler(handler, name, "on_part", bot, user, channel),
                f"plugin-{name}-on_part",
            )

    def register_command(
        self,
        plugin_name: str,
        command: str,
        handler: Callable,
        *,
        aliases: Optional[List[str]] = None,
        help_text: str = "",
    ) -> None:
        if not command:
            raise ValueError("Command name must be non-empty")
        names = {command.lower()}
        if aliases:
            names.update(alias.lower() for alias in aliases if alias)

        # Ensure no conflicts
        for name in names:
            if name in self._commands:
                raise ValueError(f"Command '{name}' already registered by {self._commands[name].plugin}")

        spec = CommandSpec(
            plugin=plugin_name,
            name=command.lower(),
            aliases=names,
            help_text=help_text,
            handler=handler,
        )
        for name in names:
            self._commands[name] = spec
        self._plugin_commands.setdefault(plugin_name, set()).update(names)

    def list_commands(self) -> List[CommandSpec]:
        seen = set()
        specs: List[CommandSpec] = []
        for spec in self._commands.values():
            if spec.name in seen:
                continue
            seen.add(spec.name)
            specs.append(spec)
        return sorted(specs, key=lambda s: s.name)

    def dispatch_registered_command(
        self,
        bot,
        user: str,
        channel: str,
        command: str,
        args: List[str],
        is_private: bool,
    ) -> bool:
        spec = self._commands.get(command.lower())
        if spec is None:
            return False
        try:
            maybe_coro = spec.handler(bot, user, channel, args, is_private)
            if inspect.iscoroutine(maybe_coro):
                self._spawn_task(
                    spec.plugin,
                    maybe_coro,
                    f"cmd-{spec.plugin}-{spec.name}"
                )
        except Exception:
            self.logger.exception("Command '%s' in plugin '%s' failed", command, spec.plugin)
        return True

    def get_config_path(self) -> Optional[Path]:
        return self._config_path

    def load_all(self, bot) -> None:
        if not self.plugin_dir.exists():
            self.logger.warning("Plugin directory %s does not exist; creating", self.plugin_dir)
            self.plugin_dir.mkdir(parents=True, exist_ok=True)

        available = sorted(
            path.stem
            for path in self.plugin_dir.glob("*.py")
            if not path.name.startswith("_")
        )
        self._disabled_plugins.intersection_update(available)
        self._known_plugins = set(available)

        for name in available:
            self._ensure_plugin_entry(bot, name, name not in self._disabled_plugins)
            if name in self._disabled_plugins:
                self.logger.info("Skipping disabled plugin '%s'", name)
                self._set_plugin_enabled_flag(bot, name, False)
                continue
            try:
                self.load(name, bot)
            except Exception:
                self.logger.exception("Failed to load plugin '%s'", name)

    def _apply_config_disabled_preferences(self) -> None:
        if not self._config_path or not self._config_path.exists():
            return
        try:
            with self._config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception:
            self.logger.warning("Failed to read config for plugin preferences", exc_info=True)
            return

        plugins_section = data.get("plugins")
        if not isinstance(plugins_section, dict):
            return

        enabled_in_config = set()
        disabled_in_config = set()
        for name, entry in plugins_section.items():
            if not isinstance(entry, dict):
                continue
            enabled_flag = entry.get("enabled")
            if enabled_flag is True:
                enabled_in_config.add(str(name))
            elif enabled_flag is False:
                disabled_in_config.add(str(name))

        if enabled_in_config:
            self._disabled_plugins.difference_update(enabled_in_config)
        if disabled_in_config:
            self._disabled_plugins.update(disabled_in_config)

    def _apply_config_defaults(self, bot, plugin_name: str, module: ModuleType) -> None:
        defaults = getattr(module, "CONFIG_DEFAULTS", None)
        if not isinstance(defaults, dict) or not defaults:
            return

        # Merge into in-memory config for the running bot.
        try:
            bot_config = getattr(bot, "config", None)
            if isinstance(bot_config, dict):
                self._merge_defaults(bot_config, defaults)
        except Exception:
            self.logger.warning("Failed to merge defaults into runtime config for '%s'", plugin_name)

        if not self._config_path:
            return

        try:
            if self._config_path.exists():
                with self._config_path.open("r", encoding="utf-8") as handle:
                    config_data = yaml.safe_load(handle) or {}
            else:
                config_data = {}
        except Exception:
            self.logger.warning("Failed to read config file while applying defaults for '%s'", plugin_name, exc_info=True)
            return

        changed = self._merge_defaults(config_data, defaults)
        if not changed:
            return

        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config_data, handle, sort_keys=False)
            self.logger.info("Installed default settings for plugin '%s' in config.yaml", plugin_name)
        except Exception:
            self.logger.warning("Failed to write config defaults for '%s'", plugin_name, exc_info=True)

    def _merge_defaults(self, target: Dict, defaults: Dict) -> bool:
        changed = False
        for key, value in defaults.items():
            if isinstance(value, dict):
                existing = target.get(key)
                if not isinstance(existing, dict):
                    if key in target:
                        # Existing non-dict; skip to avoid corruption.
                        continue
                    target[key] = {}
                    existing = target[key]
                    changed = True
                if self._merge_defaults(existing, value):
                    changed = True
            elif isinstance(value, list):
                existing = target.setdefault(key, [])
                if not isinstance(existing, list):
                    continue
                for item in value:
                    if item not in existing:
                        existing.append(item)
                        changed = True
            else:
                if key not in target:
                    target[key] = value
                    changed = True
        return changed

    def _ensure_plugin_entry(self, bot, plugin_name: str, enabled: bool) -> None:
        if hasattr(bot, "config") and isinstance(bot.config, dict):
            self._ensure_plugin_entry_in_dict(bot.config, plugin_name, enabled)
        self._ensure_plugin_entry_in_file(plugin_name, enabled)

    def _set_plugin_enabled_flag(self, bot, plugin_name: str, enabled: bool) -> None:
        if hasattr(bot, "config") and isinstance(bot.config, dict):
            self._ensure_plugin_entry_in_dict(bot.config, plugin_name, enabled, force=True)
        self._ensure_plugin_entry_in_file(plugin_name, enabled, force=True)

    def _ensure_plugin_entry_in_dict(
        self, config_dict: Dict, plugin_name: str, enabled: bool, force: bool = False
    ) -> None:
        plugins_section = config_dict.setdefault("plugins", {})
        if not isinstance(plugins_section, dict):
            return
        entry = plugins_section.setdefault(plugin_name, {})
        if not isinstance(entry, dict):
            return
        if force or "enabled" not in entry:
            entry["enabled"] = bool(enabled)

    def _ensure_plugin_entry_in_file(self, plugin_name: str, enabled: bool, force: bool = False) -> None:
        if not self._config_path:
            return
        try:
            if self._config_path.exists():
                with self._config_path.open("r", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
            else:
                data = {}
        except Exception:
            self.logger.warning("Failed to read config file while ensuring plugin entry for '%s'", plugin_name, exc_info=True)
            return

        plugins_section = data.setdefault("plugins", {})
        if not isinstance(plugins_section, dict):
            plugins_section = {}
            data["plugins"] = plugins_section

        entry = plugins_section.setdefault(plugin_name, {})
        if not isinstance(entry, dict):
            entry = {}
            plugins_section[plugin_name] = entry

        desired = bool(enabled)
        current = entry.get("enabled") if isinstance(entry.get("enabled"), bool) else None

        if force:
            if current == desired:
                return
            entry["enabled"] = desired
        else:
            if current is not None:
                return
            entry["enabled"] = desired

        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=False)
        except Exception:
            self.logger.warning("Failed to write plugin enabled flag for '%s'", plugin_name, exc_info=True)

    def _refresh_bot_config(self, bot) -> None:
        if not self._config_path or not self._config_path.exists():
            return

        try:
            with self._config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception:
            self.logger.warning("Failed to reload config.yaml from disk", exc_info=True)
            return

        if not isinstance(data, dict):
            self.logger.warning("Config reload skipped: root is not a mapping")
            return

        bot_config = getattr(bot, "config", None)
        if isinstance(bot_config, dict):
            bot_config.clear()
            bot_config.update(data)
            refresher = getattr(bot, "refresh_runtime_settings", None)
            if callable(refresher):
                try:
                    refresher()
                except Exception:
                    self.logger.exception("Failed to refresh runtime settings after config reload")

    async def _run_handler(
        self,
        handler,
        plugin_name: str,
        handler_name: str,
        *args,
    ) -> None:
        async with self._task_semaphore:
            try:
                if inspect.iscoroutinefunction(handler):
                    await asyncio.wait_for(handler(*args), timeout=HANDLER_TIMEOUT_SECS)
                else:
                    # Sync handlers
                    result = handler(*args)
                    if inspect.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=HANDLER_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Plugin '%s' %s timed out after %ss", plugin_name, handler_name, HANDLER_TIMEOUT_SECS
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Plugin '%s' raised during %s", plugin_name, handler_name)

    def _spawn_task(self, plugin_name: str, coro, task_name: str) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro, name=task_name)
        
        tasks = self._plugin_tasks.setdefault(plugin_name, set())
        tasks.add(task)
        task.add_done_callback(lambda t: tasks.discard(t))

    def _unregister_commands_for_plugin(self, plugin_name: str) -> None:
        names = self._plugin_commands.pop(plugin_name, set())
        for name in names:
            self._commands.pop(name, None)

