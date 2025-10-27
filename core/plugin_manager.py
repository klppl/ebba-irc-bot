import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional


class PluginManager:
    """Load, unload, and dispatch events to message plugins."""

    def __init__(self, plugin_dir: Path, logger: Optional[logging.Logger] = None) -> None:
        self.plugin_dir = plugin_dir
        self.logger = logger or logging.getLogger("PluginManager")
        self._plugins: Dict[str, ModuleType] = {}

    def list_plugins(self) -> List[str]:
        return sorted(self._plugins.keys())

    def module_name(self, plugin_name: str) -> str:
        return f"scripts.{plugin_name}"

    def _load_module(self, plugin_name: str) -> ModuleType:
        plugin_path = self.plugin_dir / f"{plugin_name}.py"
        if not plugin_path.exists():
            raise FileNotFoundError(f"Plugin '{plugin_name}' does not exist at {plugin_path}")

        module_name = self.module_name(plugin_name)
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

        for path in sorted(self.plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            try:
                self.load(name, bot)
            except Exception:
                self.logger.exception("Failed to load plugin '%s'", name)
