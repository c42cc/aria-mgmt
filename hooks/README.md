# Cursor Hooks (Aria forwarder)

Aria watches every Cursor IDE window on this Mac by registering hooks in
`~/.cursor/hooks.json`. The hooks run a tiny Python forwarder that POSTs
the hook payload to Aria's local HTTP endpoint
(`http://127.0.0.1:8731/cursor-event` by default).

## Files

- `cursor-event.py` — the forwarder. Reads JSON from stdin, augments it
  with the hook type (passed as argv[1]), POSTs it to localhost. Stdlib
  only — no virtualenv required. Exits 0 even on failure so Cursor never
  blocks on us.
- `install.py` — merges Aria's hook entries into `~/.cursor/hooks.json`,
  preserving any other hook entries already configured (e.g.
  `live_visuals_3/hooks/`). Idempotent. Re-running is safe.

## Install

```
.venv/bin/python hooks/install.py
```

That edits `~/.cursor/hooks.json` (following the symlink chain) and adds
entries for:

| Hook event           | Matcher                    | Fires when                         |
|----------------------|----------------------------|-------------------------------------|
| `stop`               | (all)                      | Agent loop ends                     |
| `subagentStop`       | (all)                      | Task subagent finishes              |
| `sessionEnd`         | (all)                      | Composer conversation closes        |
| `postToolUse`        | `CreatePlan`               | Plan generated in plan mode         |
| `postToolUse`        | `Task`                     | Task subagent dispatched            |
| `afterAgentResponse` | (all)                      | After every assistant message       |

Existing hook entries (the ones owned by `live_visuals_3`) are preserved
unchanged. To remove Aria's entries:

```
.venv/bin/python hooks/install.py --uninstall
```

To see what would be written without changing anything:

```
.venv/bin/python hooks/install.py --dry-run
```

A timestamped backup of the previous hooks file is written next to it on
every non-dry-run write.

## Coordination with `live_visuals_3`

The user-level `~/.cursor/hooks.json` is symlinked to
`live_visuals_3/hooks/hooks.json`. The installer follows that symlink and
edits the real file in place. Both repos' hook scripts coexist; the
installer keys Aria's entries with `"_tag": "aria-cursor-event"` so it can
find and replace them on re-install without touching the others.

If you ever change the symlink target, re-run `install.py` so Aria's
entries land in the new target.

## Verification

The bot includes a preflight probe (`cursor_external`) that verifies:

1. The forwarder script exists and is executable.
2. `~/.cursor/hooks.json` contains an aria-forwarder entry.
3. The HTTP endpoint is reachable from localhost.

If any of those fail, preflight will flag it loudly in `#ucs-alerts` —
the bot will still run, but the external-window observation is dark and
Aria won't see anything happening in other Cursor windows.

## Endpoint configuration

Override the URL with `UCS_CURSOR_EVENT_URL` in Cursor's hook environment
(unusual), or change the bot's bind via `.env`:

```
UCS_CURSOR_EVENT_HOST=127.0.0.1
UCS_CURSOR_EVENT_PORT=8731
```

The HTTP server only binds to 127.0.0.1 and rejects non-loopback
connections. Hook payloads never leave the Mac.
