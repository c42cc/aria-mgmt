"""Operation B — behavior-as-data composition: a persona references the shared
doctrine (prompts/_principles.md) instead of pasting it.

Proves the include resolver: a persona resolves the doctrine at load, a missing
include is LOUD (never a half-rendered prompt), and an include cycle is LOUD.
"""

from __future__ import annotations

import unittest

from src.constructor import prompts


class PromptIncludes(unittest.TestCase):
    def setUp(self):
        prompts.clear_cache()

    def tearDown(self):
        prompts.clear_cache()

    def test_persona_resolves_the_doctrine(self):
        text = prompts.load_template("planning")
        # The include directive is gone (resolved)...
        self.assertNotIn("{{include:", text)
        # ...replaced by the doctrine...
        self.assertIn("Operate on the dysfunctional primitive", text)
        # ...and the persona's own body is still present.
        self.assertIn("senior software architect", text)

    def test_every_reasoning_build_persona_includes_principles(self):
        for name in (
            "planning", "architecture", "refactor",
            "bug-analysis", "implementation", "do_with_claude_system",
        ):
            with self.subTest(persona=name):
                raw = prompts.read_raw(name)
                self.assertIn("{{include:_principles}}", raw)
                resolved = prompts.load_template(name)
                self.assertIn("Done means verified close to the end user", resolved)

    def test_missing_include_is_loud(self):
        with self.assertRaises(FileNotFoundError):
            prompts._resolve_includes("probe", "{{include:_does_not_exist_zzz}}")

    def test_self_include_cycle_is_loud(self):
        with self.assertRaises(ValueError):
            prompts._resolve_includes("loopy", "{{include:loopy}}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
