import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from core.irc_client import IRCClient
from core.plugin_manager import PluginManager
from core.utils import setup_logging, validate_required_keys


CONFIG_ENV_MAP = {
    "SERVER": ("server", str),
    "PORT": ("port", int),
    "USE_TLS": ("use_tls", lambda v: v.lower() in {"1", "true", "yes", "on"}),
    "NICKNAME": ("nickname", str),
    "USERNAME": ("username", str),
    "REALNAME": ("realname", str),
    "CHANNELS": ("channels", lambda v: [c.strip() for c in v.split(",") if c.strip()]),
    "PREFIX": ("prefix", str),
    "OWNER_NICKS": ("owner_nicks", lambda v: [n.strip() for n in v.split(",") if n.strip()]),
    "RECONNECT_DELAY_SECS": ("reconnect_delay_secs", int),
    "REQUEST_TIMEOUT_SECS": ("request_timeout_secs", int),
}


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping")

    apply_env_overrides(data)
    validate_required_keys(
        data,
        {
            "server": str,
            "port": int,
            "use_tls": bool,
            "nickname": str,
            "username": str,
            "realname": str,
            "channels": list,
            "prefix": str,
            "owner_nicks": list,
            "reconnect_delay_secs": int,
            "request_timeout_secs": int,
        },
    )

    return data


def apply_env_overrides(config: Dict[str, Any]) -> None:
    for env_key, (config_key, caster) in CONFIG_ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is None:
            continue
        try:
            config[config_key] = caster(value)
        except Exception as exc:
            raise ValueError(f"Invalid value for {env_key}: {value}") from exc


async def run_bot(config_path: Path) -> None:
    config = load_config(config_path)
    plugin_dir = Path(__file__).parent / "scripts"
    plugin_manager = PluginManager(
        plugin_dir,
        config_path=config_path,
    )
    client = IRCClient(config, plugin_manager)
    plugin_manager.load_all(client)
    try:
        await client.start()
    finally:
        await client.stop()


def main() -> None:
    setup_logging()
    config_path_env = os.environ.get("CONFIG_PATH")
    config_path = Path(config_path_env) if config_path_env else Path("config.yaml")
    try:
        asyncio.run(run_bot(config_path))
    except KeyboardInterrupt:
        logging.getLogger("bot").info("Interrupted, shutting down")
    except Exception as exc:
        logging.getLogger("bot").exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
