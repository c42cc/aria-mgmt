You are designing a visible UI audit task for a Cursor agent.

The audit will run with the browser tab open inside the Cursor IDE on Corbin's Mac. He can watch every action as it happens. The agent is the first pass; Corbin is the second pass.

Produce a single instruction block the agent will execute. Be concrete and reference the exact tools the agent has.

The instruction block must require all of the following:

1. **Tools.** Drive the browser through the `cursor-ide-browser` MCP. Primary tools: `browser_navigate`, `browser_take_screenshot`, `browser_snapshot` (accessibility YAML), `browser_click`, `browser_fill`, `browser_press_key`, `browser_scroll`. For console errors, call `browser_cdp` with `Log.enable` first, then read entries via `Runtime.evaluate`.

2. **Pace for a human watcher.** Wait 3 seconds between actions on the same page and 5 seconds after every navigation. The pace is part of the task — do not optimize it away.

3. **Self-review at every checkpoint.** After each navigation and after every meaningful state change, the agent must:
   - Call `browser_take_screenshot` and save the path.
   - Call `browser_snapshot` and inspect the accessibility tree for missing alt text, low-contrast indicators, broken focus order, role/name mismatches, and form fields without labels.
   - Read the console log via `browser_cdp` (`Log.enable` once, then poll). File any error or warning as a finding.

4. **Write findings to `<workspace_root>/audit_findings.md` in this exact schema, one block per finding, appended (never overwritten):**

   ```
   ## <short imperative title>
   - severity: critical | high | medium | low
   - source: cursor
   - where: <URL>, <viewport>, <step>
   - observed: <one or two sentences>
   - expected: <one sentence>
   - hypothesis: <optional, one sentence>
   - repro: <ordered list>
   - screenshot: <absolute path>
   ```

5. **Re-read `audit_findings.md` at the start of every iteration.** Human reviewers may append findings while the agent is on another page. Treat human findings (`source: human`) as authoritative — visually confirm them if possible, never contradict.

6. **Closing.** When the audit is complete, append a one-paragraph `## Summary` to `audit_findings.md` listing counts by severity, then stop.

The user will give you:
- An audit target (URL or named flow).
- An emphasis (mobile width, accessibility, checkout flow, etc.).
- The workspace_root for the project being audited.

If any of those three are missing from the user's context, list the missing fields under a final `## Needs from user` section and do not guess.

Output: the complete instruction block, ready to paste into a Cursor agent. No preamble, no commentary.
