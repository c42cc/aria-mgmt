# Project Registry

Name → absolute path mapping. Used by:

- `build_with_cursor` to resolve a project name when Aria spawns a new
  Cursor agent.
- `read_cursor_window`, `focus_cursor_window`, `send_to_cursor_chat`,
  and the rest of the cursor remote-control tools to identify which
  externally-opened Cursor IDE window the user is asking about by short
  name (matched against the window title and against
  `~/.cursor/projects/<safe-cwd>/`).

Entries are one-per-line, "- name → /absolute/path" (the arrow is a real
unicode →). Names should be short and pronounceable for voice — Aria says
them out loud.

- ucs → /Users/corbin/PycharmProjects/agi_env_v1/ucs2

<!--
ADD YOUR TWO OTHER PRODUCT PROJECTS HERE. Examples:

- product-one → /Users/corbin/PycharmProjects/some-product-one
- product-two → /Users/corbin/PycharmProjects/some-other-product
-->
