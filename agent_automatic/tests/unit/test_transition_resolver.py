import unittest

from agent_automatic.infrastructure.jira.transition_resolver import TransitionResolver


class TestTransitionResolver(unittest.TestCase):
    def test_resolve_by_alias(self):
        resolver = TransitionResolver()
        transitions = [{"id": "1", "name": "На стабилизацию"}, {"id": "2", "name": "Something else"}]
        aliases = {"Стабилизация": ["На стабилизацию", "Стабилизация"]}
        resolved = resolver.resolve("Стабилизация", transitions, aliases)
        self.assertEqual(resolved.id, "1")
        self.assertEqual(resolved.name, "На стабилизацию")

    def test_resolve_by_preferred_id(self):
        resolver = TransitionResolver()
        transitions = [{"id": "15904", "name": "X"}, {"id": "1", "name": "Стабилизация"}]
        resolved = resolver.resolve("Стабилизация", transitions, {}, preferred_transition_id="15904")
        self.assertEqual(resolved.id, "15904")


if __name__ == "__main__":
    unittest.main()

