import unittest
from pathlib import Path

from agent_automatic.app.settings import load_settings


class TestContainerBuild(unittest.TestCase):
    def test_build(self):
        try:
            from agent_automatic.app.container import build_container
        except Exception as e:
            self.skipTest(f"Container build skipped (missing deps?): {e}")
        root = Path(__file__).resolve().parents[4]
        settings = load_settings(root)
        container = build_container(settings)
        self.assertIsNotNone(container.chat_controller)


if __name__ == "__main__":
    unittest.main()

