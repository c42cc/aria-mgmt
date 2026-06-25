# UCS — the monorepo (whole-house line)

> ## What this repo is
>
> **`c42cc/ucs` — the whole-house monorepo**, this worktree checked out on the
> **`whole-house-aria`** line. It is the shared git home both Arias grew out of —
> NOT itself the running bot, and NOT the standalone membrane.
>
> - **Span of control.** The broader environment and substrate: the house (Home
>   Assistant endpoint + loops), the DGX **Spark** endpoint, and the historical
>   UCS/Aria code. Every branch of the live bot — including its pinned trunk
>   `aria-live` — lives in this repo's git.
> - **Used for.** Whole-house development and shared infrastructure. Two distinct
>   systems run *from* this repo and keep their own docs:
>   - **Live Discord Aria** — the always-on voice/MCP assistant, run from the
>     `aria-live` worktree at `../ucs2-notify-on-stop`.
>   - **The v2 membrane** — the system that manages your repos via Claude Code,
>     now standalone at **`c42cc/aria`** (`../aria`).
> - **For whom.** Corbin / developers working across the environment.

Open the worktree that matches your task: `../ucs2-notify-on-stop` for the live
assistant (its README is the canonical run + architecture guide), `../aria` for
the membrane. This `whole-house-aria` line is where Aria reaches past the repo to
control the house (Home Assistant + Spark endpoints).
