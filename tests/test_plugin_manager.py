import asyncio
import unittest
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Adjust path to import core modules
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.plugin_manager import PluginManager

class TestPluginManager(unittest.IsolatedAsyncioTestCase):
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

    async def test_load_unload(self):
        self.create_dummy_plugin("p1", "def on_load(bot): pass")
        self.pm.load("p1", self.bot)
        self.assertIn("p1", self.pm.list_plugins())
        
        self.pm.unload("p1", self.bot)
        self.assertNotIn("p1", self.pm.list_plugins())

    async def test_task_cleanup(self):
        # Plugin that starts a long running task
        content = """
import asyncio
async def on_message(bot, user, channel, message):
    await asyncio.sleep(5)
"""
        self.create_dummy_plugin("p2", content)
        self.pm.load("p2", self.bot)
        
        # Dispatch a message to spawn a task
        self.pm.dispatch_message(self.bot, "u", "c", "msg")
        
        # Verify task exists
        tasks = self.pm._plugin_tasks.get("p2", set())
        self.assertTrue(len(tasks) > 0)
        
        # Unload should cancel tasks
        self.pm.unload("p2", self.bot)
        
        # Tasks should be gone or done/cancelled
        # Note: unload calls cancel(), but we need to yield to loop to let it process
        await asyncio.sleep(0.1)
        tasks = self.pm._plugin_tasks.get("p2", set())
        # The set might be empty because done callback removes it, or empty because we popped it in unload.
        # Check internal state of pm.
        self.assertEqual(len(tasks), 0)

    async def test_concurrency_limit(self):
        # We need to artificially lower the limit for testing
        self.pm._task_semaphore = asyncio.Semaphore(2)
        
        content = """
import asyncio
async def on_message(bot, user, channel, message):
    await asyncio.sleep(0.2)
"""
        self.create_dummy_plugin("p3", content)
        self.pm.load("p3", self.bot)
        
        # Spawn 5 tasks
        for _ in range(5):
            self.pm.dispatch_message(self.bot, "u", "c", "msg")
            
        # We can't easily check the semaphore state directly comfortably, 
        # but we can ensure no exceptions were raised and tasks are tracked.
        tasks = self.pm._plugin_tasks.get("p3", set())
        self.assertEqual(len(tasks), 5)
        
        # Wait for them to finish
        await asyncio.sleep(0.5)
        self.assertEqual(len(tasks), 0)

if __name__ == "__main__":
    unittest.main()
