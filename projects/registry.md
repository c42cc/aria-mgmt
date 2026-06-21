# Project Registry

Name → absolute path mapping. Used by:

- `build_with_cursor` to resolve a project name when Aria spawns a new
  Cursor agent.
- `read_cursor_window`, `focus_cursor_window`, `send_to_cursor_chat`,
  and the rest of the cursor remote-control tools to identify which
  externally-opened Cursor IDE window the user is asking about by short
  name (matched against the window title and against
  `~/.cursor/projects/<safe-cwd>/`).
- `tools._build_context`, which renders this map into EVERY agent loop's
  first message — the loop must never pay Opus prices to search the
  filesystem for a path listed here. (Forensic 2026-06-12: "live visuals
  three" cost ~$3 of blind discovery, twice, because this file didn't list
  it and wasn't surfaced to the agent.) Keep every project Aria might be
  asked about in this list.

Entries are one-per-line, "- name → /absolute/path" (the arrow is a real
unicode →). Names should be short and pronounceable for voice — Aria says
them out loud.

- ucs → /Users/corbin/PycharmProjects/agi_env_v1/ucs2
- live_visuals_4 → /Users/corbin/PycharmProjects/agi_env_v1/live_visuals_4
- live_visuals_4_cc → /Users/corbin/PycharmProjects/agi_env_v1/live_visuals_4_CC
- live_visuals_3 → /Users/corbin/PycharmProjects/agi_env_v1/live_visuals_3
- alive_river → /Users/corbin/PycharmProjects/agi_env_v1/alive_river
- spicylit → /Users/corbin/PycharmProjects/spicylit
- scratch → /Users/corbin/PycharmProjects/agi_env_v1/aria-scratch
