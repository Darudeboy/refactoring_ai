import unittest

from agent_automatic.domain.commands.parser import CommandParser
from agent_automatic.domain.common.enums import CommandIntent


class TestCommandParser(unittest.TestCase):
    def test_parse_run_next_step(self):
        p = CommandParser()
        cmd = p.parse("двигай HRPRELEASE-115202 дальше")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd.intent, CommandIntent.run_next_release_step)
        self.assertEqual(cmd.release_key, "HRPRELEASE-115202")

    def test_parse_guided_cycle(self):
        p = CommandParser()
        cmd = p.parse("запусти полный цикл релиза для HRPRELEASE-1 dry-run")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd.intent, CommandIntent.start_release_guided_cycle)
        self.assertTrue(cmd.slots.get("dry_run"))


if __name__ == "__main__":
    unittest.main()

