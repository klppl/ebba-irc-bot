import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Tuple


class PluginManager:
    """Load, unload, and dispatch events to message plugins."""

    def __init__(
        self,
        plugin_dir: Path,
        logger: Optional[logging.Logger] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        self.plugin_dir = plugin_dir
        self.logger = logger or logging.getLogger("PluginManager")
        self._plugins: Dict[str, ModuleType] = {}
        self._state_path = state_path
        self._disabled_plugins, self._known_plugins = self._load_state()

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
            if name in self._disabled_plugins:
                self.logger.info("Skipping disabled plugin '%s'", name)
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
