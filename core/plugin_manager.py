import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Tuple

import yaml


class PluginManager:
    """Load, unload, and dispatch events to message plugins."""

    def __init__(
        self,
        plugin_dir: Path,
        logger: Optional[logging.Logger] = None,
        state_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.plugin_dir = plugin_dir
        self.logger = logger or logging.getLogger("PluginManager")
        self._plugins: Dict[str, ModuleType] = {}
        self._state_path = state_path
        self._disabled_plugins, self._known_plugins = self._load_state()
        self._config_path = config_path
        self._apply_config_disabled_preferences()

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

    def load(self, plugin_name: str, bot) -> None:
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
            if not hasattr(self, "_known_plugins"):
                self._known_plugins = set()
            self._known_plugins.add(plugin_name)
            if plugin_name in self._disabled_plugins:
                self._disabled_plugins.discard(plugin_name)
            self._save_state()
            self._set_plugin_enabled_flag(bot, plugin_name, True)
            self.logger.info("Loaded plugin '%s'", plugin_name)

    def unload(self, plugin_name: str, bot) -> None:
        module = self._plugins.pop(plugin_name, None)
        if module is None:
            raise RuntimeError(f"Plugin '{plugin_name}' is not loaded")

        on_unload = getattr(module, "on_unload", None)
        if callable(on_unload):
            try:
                on_unload(bot)
            except Exception:
                self.logger.exception("Error in on_unload for plugin '%s'", plugin_name)

        module_name = getattr(module, "__name__", self.module_name(plugin_name))
        sys.modules.pop(module_name, None)
        self._disabled_plugins.add(plugin_name)
        self._save_state()
        self._set_plugin_enabled_flag(bot, plugin_name, False)
        self.logger.info("Unloaded plugin '%s'", plugin_name)

    def reload(self, plugin_name: str, bot) -> None:
        self.unload(plugin_name, bot)
        self.load(plugin_name, bot)

    def dispatch_message(self, bot, user: str, channel: str, message: str) -> None:
        for name, module in list(self._plugins.items()):
            handler = getattr(module, "on_message", None)
            if not callable(handler):
                self.logger.warning(
                    "Plugin '%s' lacks on_message handler; skipping dispatch", name
                )
                continue
            try:
                handler(bot, user, channel, message)
            except Exception:
                self.logger.exception("Plugin '%s' raised during on_message", name)

    def dispatch_join(self, bot, user: str, channel: str) -> None:
        for name, module in list(self._plugins.items()):
            handler = getattr(module, "on_join", None)
            if not callable(handler):
                continue
            try:
                handler(bot, user, channel)
            except Exception:
                self.logger.exception("Plugin '%s' raised during on_join", name)

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
        self._save_state()

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

    def _load_state(self) -> Tuple[set, set]:
        if not self._state_path:
            return set(), set()
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                disabled = payload.get("disabled", [])
                known = payload.get("plugins", [])
                if isinstance(disabled, list):
                    disabled_set = {str(name) for name in disabled}
                else:
                    disabled_set = set()
                if isinstance(known, list):
                    known_set = {str(name) for name in known}
                else:
                    known_set = set()
                return disabled_set, known_set
        except Exception:
            self.logger.warning("Failed to load plugin state; starting fresh", exc_info=True)
        return set(), set()

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

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            if not getattr(self, "_known_plugins", None):
                self._known_plugins = {
                    path.stem
                    for path in self.plugin_dir.glob("*.py")
                    if not path.name.startswith("_")
                }
            payload = {
                "disabled": sorted(self._disabled_plugins),
                "plugins": sorted(self._known_plugins),
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._state_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
        except Exception:
            self.logger.warning("Failed to persist plugin state", exc_info=True)

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
