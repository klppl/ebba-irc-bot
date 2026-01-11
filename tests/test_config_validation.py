import unittest
from core.utils import validate_config

class TestConfigValidation(unittest.TestCase):
    def test_valid_config(self):
        config = {
            "server": "irc.example.com",
            "port": 6667,
            "nickname": "ebba",
            "username": "ebba",
            "realname": "Ebba Bot",
            "channels": ["#test"],
            "use_tls": True,
            "owner_nicks": ["admin"]
        }
        # Should not raise
        validate_config(config)

    def test_missing_required(self):
        config = {
            "server": "irc.example.com",
            # missing port
            "nickname": "ebba",
            "username": "ebba",
            "realname": "Ebba Bot",
            "channels": ["#test"]
        }
        with self.assertRaisesRegex(KeyError, "Missing required config keys"):
            validate_config(config)

    def test_invalid_type(self):
        config = {
            "server": "irc.example.com",
            "port": "6667", # String instead of int
            "nickname": "ebba",
            "username": "ebba",
            "realname": "Ebba Bot",
            "channels": ["#test"]
        }
        with self.assertRaisesRegex(TypeError, "Config key 'port' must be of type int"):
            validate_config(config)

    def test_optional_invalid_type(self):
        config = {
            "server": "irc.example.com",
            "port": 6667,
            "nickname": "ebba",
            "username": "ebba",
            "realname": "Ebba Bot",
            "channels": ["#test"],
            "use_tls": "yes" # String instead of bool
        }
        with self.assertRaisesRegex(TypeError, "Config key 'use_tls' must be of type bool"):
            validate_config(config)

if __name__ == "__main__":
    unittest.main()
