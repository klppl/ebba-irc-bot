import sys
import unittest
from datetime import timedelta, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.remindme import _parse_duration, _parse_absolute

class TestRemindMeParsing(unittest.TestCase):
    def test_parse_simple(self):
        self.assertEqual(_parse_duration("1s"), timedelta(seconds=1))

    def test_absolute_parsing(self):
        # Test future date
        future = datetime.now() + timedelta(days=1, hours=1)
        future_str = future.strftime("%Y-%m-%d %H:%M")
        input_str = f"{future_str} My Reminder"
        
        delta, msg, error = _parse_absolute(input_str)
        self.assertIsNone(error)
        self.assertEqual(msg, "My Reminder")
        # delta should be roughly 1 day 1 hour
        self.assertTrue(timedelta(days=1) < delta < timedelta(days=1, hours=2))

    def test_absolute_parsing_invalid(self):
        # Invalid format
        delta, msg, error = _parse_absolute("2025-99-99 99:99")
        self.assertEqual(error, "correct format is YYYY-MM-DD HH:MM")
        self.assertIsNone(delta)

        # Missing time
        delta, msg, error = _parse_absolute("2025-12-16")
        self.assertEqual(error, "correct format is YYYY-MM-DD HH:MM")

if __name__ == '__main__':
    unittest.main()
