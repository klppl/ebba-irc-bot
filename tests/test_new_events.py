
import asyncio
import unittest
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.plugin_manager import PluginManager

class TestNewEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.plugin_dir = self.test_dir / "scripts"
        self.plugin_dir.mkdir()
        self.bot = MagicMock()
        self.bot.config = {}
        self.pm = PluginManager(self.plugin_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def create_dummy_plugin(self, name, content):
        p = self.plugin_dir / f"{name}.py"
        with p.open("w") as f:
            f.write(content)
        return name

    async def test_dispatch_nick(self):
        content = """
events = []
def on_nick(bot, user, new_nick):
    events.append((user, new_nick))
"""
        self.create_dummy_plugin("p_nick", content)
        self.pm.load("p_nick", self.bot)

        self.pm.dispatch_nick(self.bot, "old_nick!user@host", "new_nick")

        # Allow task to run
        await asyncio.sleep(0.1)

        module = self.pm._plugins["p_nick"]
        self.assertEqual(len(module.events), 1)
        self.assertEqual(module.events[0], ("old_nick!user@host", "new_nick"))

    async def test_dispatch_kick(self):
        content = """
events = []
def on_kick(bot, channel, target, kicker, reason):
    events.append((channel, target, kicker, reason))
"""
        self.create_dummy_plugin("p_kick", content)
        self.pm.load("p_kick", self.bot)

        self.pm.dispatch_kick(self.bot, "#chan", "victim", "admin", "reason")

        await asyncio.sleep(0.1)

        module = self.pm._plugins["p_kick"]
        self.assertEqual(len(module.events), 1)
        self.assertEqual(module.events[0], ("#chan", "victim", "admin", "reason"))

    async def test_dispatch_quit(self):
        content = """
events = []
def on_quit(bot, user, reason):
    events.append((user, reason))
"""
        self.create_dummy_plugin("p_quit", content)
        self.pm.load("p_quit", self.bot)

        self.pm.dispatch_quit(self.bot, "user!u@h", "bye")

        await asyncio.sleep(0.1)

        module = self.pm._plugins["p_quit"]
        self.assertEqual(len(module.events), 1)
        self.assertEqual(module.events[0], ("user!u@h", "bye"))

if __name__ == "__main__":
    unittest.main()
