import unittest

from agent_automatic.domain.release.workflow import next_status, is_terminal


class TestWorkflow(unittest.TestCase):
    def test_next_status(self):
        order = ["A", "B", "C"]
        self.assertEqual(next_status("A", order), "B")
        self.assertEqual(next_status("b", order), "C")
        self.assertIsNone(next_status("C", order))
        self.assertIsNone(next_status("X", order))

    def test_is_terminal(self):
        self.assertTrue(is_terminal("Done", ["done"]))
        self.assertFalse(is_terminal("In Progress", ["done"]))


if __name__ == "__main__":
    unittest.main()

