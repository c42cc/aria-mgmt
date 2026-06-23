"""The Universal Constructor — the inspectable program Aria runs on.

Aria (the voice-first shell and her management) is one thing; the Universal
Constructor is another, and this package is the boundary between them. The
Constructor is the *program*, not the shell: a prompt library, the injection
that assembles prompts into model calls, version control over every edit, and
an "improve" loop that scores prompt versions against real usage. Aria *wields*
the Constructor through her prompt-management tools; she does not BE it.

What this package owns:

- ``prompts``      — the library + ``{{include:NAME}}`` injection + version
                     control (load / save / rollback / list).
- ``eval``         — the improve/eval loop (``EvalRunner``): scores prompt
                     versions and advises rollbacks. It ADVISES; user voice
                     edits always win (ARCHITECTURE.md Fundamental 13).
- ``prompt_tools`` — Aria's prompt-management tool handlers (list / show / edit
                     / rollback / versions / reload): her hands on the library.
                     The Aria-side glue they need (Anthropic client, Discord
                     callbacks, session reconnect, cost accounting) is injected
                     via ``init_prompt_tools`` so this package never imports
                     ``bot.py`` or the tool catalog.

What stays on the Aria side (deliberately NOT here): the single agent loop and
the tool catalog/dispatch (``src/tools.py``), and the structured-state store
(``src/db.py``, the one home of the ``prompt_versions`` / ``eval_results``
tables this package reads and writes through).

See ``VISION_CONSTRUCTOR.md`` and ``docs/universal_constructor.html``.
"""

from __future__ import annotations

# The library + injection + version-control surface. `eval` (the improve loop)
# is intentionally NOT re-exported here: it ships a `__main__` CLI, and eagerly
# importing it at package load makes `python -m src.constructor.eval` warn about
# a double import. Reach it directly: `from src.constructor.eval import EvalRunner`.
from .prompts import (
    clear_cache,
    get_path,
    get_versions,
    list_templates,
    load_template,
    read_raw,
    rollback_template,
    save_template,
)

__all__ = [
    "clear_cache",
    "get_path",
    "get_versions",
    "list_templates",
    "load_template",
    "read_raw",
    "rollback_template",
    "save_template",
]
