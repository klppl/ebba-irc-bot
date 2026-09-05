import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


# The project declares requests as a runtime dependency. Keep these unit tests
# runnable in minimal source-check environments too, where dependencies may not
# yet have been installed.
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")

    class RequestError(Exception):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=type("Timeout", (RequestError,), {}),
        ConnectionError=type("ConnectionError", (RequestError,), {}),
        HTTPError=type("HTTPError", (RequestError,), {}),
    )
    requests_stub.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from scripts import ai


class AIPluginDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = ai.AISettings(
            api_key="test",
            provider="grok",
            model="grok-4.6",
            history_retention_days=30,
            history_max_entries=2,
            enabled=True,
        )
        ai.state = ai.AIState(
            settings=settings,
            db_path=Path(self.temp_dir.name) / "ai.sqlite3",
        )
        ai._init_db()

    def tearDown(self):
        ai.state = None
        self.temp_dir.cleanup()

    def test_history_is_isolated_by_channel_and_pm(self):
        ai._db_add_turn("alex", "user", "from a", "#a")
        ai._db_add_turn("alex", "assistant", "reply a", "#a")
        ai._db_add_turn("alex", "user", "from b", "#b")
        ai._db_add_turn("alex", "user", "private", "PM")

        self.assertEqual(
            ai._db_get_recent("alex", "#a"),
            [("user", "from a"), ("assistant", "reply a")],
        )
        self.assertEqual(ai._db_get_recent("alex", "#b"), [("user", "from b")])
        self.assertEqual(ai._db_get_recent("alex", "PM"), [("user", "private")])

    def test_history_clear_is_scoped(self):
        ai._db_add_turn("alex", "user", "a", "#a")
        ai._db_add_turn("sam", "user", "a2", "#a")
        ai._db_add_turn("alex", "user", "b", "#b")

        ai._db_clear_history(nick="alex", source="#a")

        self.assertEqual(ai._db_get_recent("alex", "#a"), [])
        self.assertEqual(ai._db_get_recent("sam", "#a"), [("user", "a2")])
        self.assertEqual(ai._db_get_recent("alex", "#b"), [("user", "b")])

    def test_history_is_bounded_per_conversation(self):
        for text in ("one", "two", "three"):
            ai._db_add_turn("alex", "user", text, "#a")

        self.assertEqual(
            ai._db_get_recent("alex", "#a", limit=20),
            [("user", "two"), ("user", "three")],
        )

    def test_grok_payload_disables_server_side_state(self):
        payload = ai._build_responses_payload(
            [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "latest news"},
            ],
            "grok-4.6",
            0.8,
            200,
            True,
        )

        self.assertIs(payload["store"], False)
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["max_turns"], 1)
        self.assertEqual(payload["prompt_cache_key"], "ebba-irc-ai-v1")
        self.assertEqual(payload["instructions"], "Be concise")

    def test_openai_payload_is_cost_bounded(self):
        ai.state.settings = ai.AISettings(
            api_key="test",
            provider="openai",
            model="gpt-5.6-luna",
            reasoning_effort="none",
            search_max_calls=1,
            search_context_size="low",
            enabled=True,
        )

        payload = ai._build_responses_payload(
            [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "latest news"},
            ],
            "gpt-5.6-luna",
            0.75,
            180,
            True,
        )

        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["text"], {"verbosity": "low"})
        self.assertEqual(
            payload["tools"],
            [{"type": "web_search", "search_context_size": "low"}],
        )
        self.assertEqual(payload["max_tool_calls"], 1)
        self.assertEqual(payload["max_output_tokens"], 180)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_turns", payload)

    def test_background_context_excludes_triggering_message(self):
        ai.state.settings.background_context_chars = 1200
        ai.state.channel_log["#a"] = ai.deque(
            [("sam", "earlier context"), ("alex", "ebba: latest news?")]
        )

        lines = ai._build_background_lines(
            "#a", exclude_last=("alex", "ebba: latest news?")
        )

        self.assertEqual(lines, ["sam: earlier context"])

    def test_responses_parser_uses_structured_citations(self):
        data = {
            "citations": [
                "https://example.com/article",
                "https://example.net/source",
            ],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Fact[[1]](https://example.com/article)",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/article",
                                    "title": "Article",
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        reply, citations = ai._parse_responses_reply(data)

        self.assertIn("Fact", reply)
        self.assertEqual(
            citations,
            [
                {"url": "https://example.com/article", "title": "Article"},
                {"url": "https://example.net/source", "title": ""},
            ],
        )

    def test_circuit_breaker_has_a_cooldown(self):
        ai.state.settings.failure_threshold = 2
        ai.state.settings.failure_cooldown_secs = 10

        ai._record_api_failure("#a")
        self.assertNotIn("#a", ai.state.circuit_open_until)
        ai._record_api_failure("#a")

        self.assertGreater(ai.state.circuit_open_until["#a"], 0)

    def test_talkback_requires_explicit_channel_opt_in(self):
        self.assertEqual(ai._db_get_channel_talkback("#a"), 0)
        self.assertTrue(ai._db_set_channel_talkback("#a", True))
        self.assertEqual(ai._db_get_channel_talkback("#a"), 1)
        self.assertTrue(ai._db_set_channel_talkback("#a", False))
        self.assertEqual(ai._db_get_channel_talkback("#a"), 0)

    def test_legacy_implicit_talkback_is_disabled_by_migration(self):
        with ai._db_conn() as conn:
            conn.execute("DROP TABLE ai_channel_settings")
            conn.execute(
                "CREATE TABLE ai_channel_settings ("
                "channel TEXT PRIMARY KEY, talkback INTEGER DEFAULT 1, "
                "enabled INTEGER DEFAULT 1, language TEXT)"
            )
            conn.execute(
                "INSERT INTO ai_channel_settings (channel, talkback, enabled) "
                "VALUES ('#legacy', 1, 1)"
            )
            conn.commit()

        ai.state.channel_settings_cache.clear()
        ai._init_db()

        self.assertEqual(ai._db_get_channel_talkback("#legacy"), 0)

    def test_legacy_grok_default_is_migrated(self):
        fake_utils = types.ModuleType("core.utils")
        fake_utils.get_plugin_config = lambda bot, name: {
            "provider": "grok",
            "model": "grok-4-1-fast",
            "api_key": "test",
        }

        with mock.patch.dict(sys.modules, {"core.utils": fake_utils}):
            settings = ai._settings_from_config(object())

        self.assertEqual(settings.model, "grok-4.6")
        self.assertTrue(settings.enabled)

    def test_blank_grok_model_uses_balanced_quality_defaults(self):
        fake_utils = types.ModuleType("core.utils")
        fake_utils.get_plugin_config = lambda bot, name: {
            "provider": "grok",
            "model": "",
            "api_key": "test",
            "language": "sv",
        }

        with mock.patch.dict(sys.modules, {"core.utils": fake_utils}):
            settings = ai._settings_from_config(object())

        self.assertEqual(settings.model, "grok-4.6")
        self.assertEqual(settings.reasoning_effort, "low")
        self.assertEqual(settings.grok_reasoning_effort, "low")
        self.assertEqual(settings.language, "sv")

    def test_blank_openai_model_uses_cost_optimized_defaults(self):
        fake_utils = types.ModuleType("core.utils")
        fake_utils.get_plugin_config = lambda bot, name: {
            "provider": "openai",
            "model": "",
            "api_key": "test",
        }

        with mock.patch.dict(sys.modules, {"core.utils": fake_utils}):
            settings = ai._settings_from_config(object())

        self.assertEqual(settings.model, "gpt-5.6-luna")
        self.assertEqual(settings.reasoning_effort, "none")
        self.assertEqual(settings.search_max_calls, 1)
        self.assertEqual(settings.search_context_size, "low")
        self.assertEqual(settings.history_context_entries, 8)
        self.assertEqual(settings.background_context_chars, 1200)
        self.assertEqual(settings.max_reply_chars, 420)

    def test_generic_swedish_questions_do_not_trigger_web_search(self):
        for message in (
            "vad är en monad?",
            "vem är gandalf?",
            "berätta om linux",
            "hur gammal är jorden?",
        ):
            self.assertIsNone(ai._SV_BUNDLE.search_intent_re.search(message), message)

    def test_current_swedish_questions_still_trigger_web_search(self):
        for message in (
            "senaste nyheterna om xAI",
            "vad kostar bitcoin just nu?",
            "väder idag",
            "sök efter valresultatet",
        ):
            self.assertIsNotNone(ai._SV_BUNDLE.search_intent_re.search(message), message)


class AIPluginAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        ai.state = ai.AIState(
            settings=ai.AISettings(
                api_key="test", provider="grok", model="grok-4.6", enabled=True
            ),
            db_path=Path(self.temp_dir.name) / "ai.sqlite3",
        )
        ai._init_db()

    async def asyncTearDown(self):
        if ai.state is not None:
            for task in list(ai.state.background_tasks):
                task.cancel()
        ai.state = None
        await asyncio.sleep(0)
        self.temp_dir.cleanup()

    async def test_ai_commands_never_enter_channel_context(self):
        class Bot:
            nickname = "ebba"
            prefix = "."

        ai.on_message(
            Bot(), "owner!~ident@host", "#room", ".ai remember secret fact"
        )

        self.assertNotIn("#room", ai.state.channel_log)

    async def test_forget_removes_note_and_all_channel_context(self):
        class Bot:
            nickname = "ebba"
            prefix = "."

            def __init__(self):
                self.sent = []

            def _has_owner_access(self, user):
                return True

            async def privmsg(self, channel, text):
                self.sent.append((channel, text))

        bot = Bot()
        ai._db_add_memory("#room", "secret fact", "owner")
        ai._db_add_memory("#room", "keep this", "owner")
        ai._db_add_turn("alex", "user", "what is the secret?", "#room")
        ai._db_add_turn("sam", "assistant", "the secret fact", "#room")
        ai._db_add_turn("alex", "user", "unrelated", "#other")
        ai.state.history[("#room", "alex")] = ai.deque(["alex: secret fact"])
        ai.state.history[("#other", "alex")] = ai.deque(["alex: unrelated"])
        ai.state.channel_log["#room"] = ai.deque(
            [("owner", ".ai remember secret fact"), ("ebba", "the secret fact")]
        )
        ai.state.citation_cache["#room"] = [{"url": "https://example.com"}]

        await ai._cmd_ai_toggle(
            bot, "owner!~ident@host", "#room", ["forget", "1"], False
        )

        self.assertEqual(ai._db_get_memories("#room"), [(2, "keep this")])
        self.assertEqual(ai._db_get_recent("alex", "#room"), [])
        self.assertEqual(ai._db_get_recent("sam", "#room"), [])
        self.assertEqual(ai._db_get_recent("alex", "#other"), [("user", "unrelated")])
        self.assertNotIn(("#room", "alex"), ai.state.history)
        self.assertIn(("#other", "alex"), ai.state.history)
        self.assertNotIn("#room", ai.state.channel_log)
        self.assertNotIn("#room", ai.state.citation_cache)
        self.assertEqual(
            bot.sent[-1], ("#room", "Forgot note #1 for #room.")
        )

    async def test_forget_all_reports_database_failure(self):
        class Bot:
            nickname = "ebba"
            prefix = "."

            def __init__(self):
                self.sent = []

            def _has_owner_access(self, user):
                return True

            async def privmsg(self, channel, text):
                self.sent.append((channel, text))

        bot = Bot()
        with mock.patch.object(ai, "_db_clear_memories", return_value=False):
            await ai._cmd_ai_toggle(
                bot, "owner!~ident@host", "#room", ["forget", "all"], False
            )

        self.assertEqual(
            bot.sent[-1], ("#room", ai._EN_STRINGS.ai_failed)
        )

    async def test_reply_from_pre_forget_context_is_discarded(self):
        class Bot:
            nickname = "ebba"

            def __init__(self):
                self.sent = []

            async def privmsg(self, channel, text):
                self.sent.append((channel, text))

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_call_api(messages, model, temp, max_toks, *, search_mode):
            started.set()
            await release.wait()
            return "stale secret", []

        bot = Bot()
        lock = ai._get_channel_lock("#room")
        with mock.patch.object(ai, "_call_api", fake_call_api):
            task = asyncio.create_task(
                ai._run_completion(
                    bot,
                    "alex",
                    "#room",
                    [{"role": "user", "content": "what is the secret?"}],
                    False,
                    False,
                    search_mode=False,
                    wants_sources=False,
                    is_chimein=False,
                    chan_lock=lock,
                    per_conv_key=("#room", "alex"),
                    bundle=ai._EN_BUNDLE,
                    context_revision=0,
                )
            )
            await started.wait()
            ai._clear_channel_runtime_context("#room")
            release.set()
            await task

        self.assertEqual(bot.sent, [])
        self.assertEqual(ai._db_get_recent("alex", "#room"), [])

    async def test_queued_pre_forget_message_cannot_restore_cleared_context(self):
        class Bot:
            nickname = "ebba"

            def __init__(self):
                self.sent = []

            async def privmsg(self, channel, text):
                self.sent.append((channel, text))

        bot = Bot()
        lock = ai._get_channel_lock("#Room")
        await lock.acquire()
        try:
            task = asyncio.create_task(
                ai._process_message(
                    bot,
                    "alex!user@host",
                    "alex",
                    "#room",
                    False,
                    True,
                    "the forgotten secret",
                )
            )
            await asyncio.sleep(0)
            ai._clear_channel_runtime_context("#room")
        finally:
            lock.release()
        await task

        self.assertEqual(bot.sent, [])
        self.assertNotIn(("#room", "alex"), ai.state.history)
        self.assertEqual(ai._db_get_recent("alex", "#room"), [])

    async def test_unload_cancels_plugin_owned_tasks(self):
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        task = ai._spawn_ai_task(worker(), "ai-test-worker")
        await started.wait()

        ai.on_unload(None)
        await asyncio.sleep(0)

        self.assertIsNotNone(task)
        self.assertTrue(task.cancelled())
        self.assertIsNone(ai.state)

    async def test_grok_call_uses_production_payload_and_timeout(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hello"}],
                        }
                    ]
                }

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

        ai.state.headers = {"Authorization": "Bearer test"}
        ai.state.settings.connect_timeout_secs = 3
        ai.state.settings.request_timeout_secs = 45

        fake_utils = types.ModuleType("core.utils")

        async def run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        fake_utils.run_blocking = run_blocking

        with mock.patch.dict(sys.modules, {"core.utils": fake_utils}), mock.patch.object(
            ai.requests, "post", fake_post
        ):
            reply, citations = await ai._call_api(
                [{"role": "user", "content": "hello"}],
                "grok-4.6",
                0.8,
                200,
                search_mode=True,
            )

        self.assertEqual(reply, "hello")
        self.assertEqual(citations, [])
        self.assertEqual(captured["url"], "https://api.x.ai/v1/responses")
        self.assertEqual(captured["timeout"], (3, 45))
        self.assertIs(captured["json"]["store"], False)
        self.assertEqual(captured["json"]["tools"], [{"type": "web_search"}])
        self.assertEqual(captured["json"]["reasoning"], {"effort": "low"})
        self.assertEqual(captured["json"]["max_turns"], 1)

    async def test_split_output_stays_within_irc_byte_limit(self):
        class Bot:
            def __init__(self):
                self.sent = []

            async def privmsg(self, channel, text):
                self.sent.append((channel, text))

        bot = Bot()
        channel = "#production"
        text = ("räksmörgås🙂" * 80) + " end"

        with mock.patch.object(ai, "SEND_DELAY", 0):
            await ai._send_split(bot, channel, text)

        self.assertGreater(len(bot.sent), 1)
        for target, part in bot.sent:
            wire = f"PRIVMSG {target} :{part}\r\n".encode("utf-8")
            self.assertLessEqual(len(wire), 512)


if __name__ == "__main__":
    unittest.main()
