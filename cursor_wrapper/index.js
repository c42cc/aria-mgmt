#!/usr/bin/env node
/**
 * ~80-line bridge between UCS Python process and @cursor/sdk.
 * Reads JSON commands on stdin, writes JSON events on stdout.
 */

const readline = require("readline");

if (process.argv.includes("--healthcheck")) {
  console.log(JSON.stringify({ status: "ok" }));
  process.exit(0);
}

let Agent;
try {
  ({ Agent } = require("@cursor/sdk"));
} catch {
  console.error("@cursor/sdk not installed. Run: npm install");
  process.exit(1);
}

const sessions = new Map();

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", async (line) => {
  let cmd;
  try {
    cmd = JSON.parse(line);
  } catch {
    respond({ error: "Invalid JSON" });
    return;
  }

  try {
    if (cmd.action === "create") {
      await handleCreate(cmd);
    } else if (cmd.action === "send") {
      await handleSend(cmd);
    } else {
      respond({ error: `Unknown action: ${cmd.action}` });
    }
  } catch (err) {
    respond({ error: err.message });
  }
});

async function handleCreate({ project_path, instruction, model }) {
  const agent = await Agent.create({
    model: model || "composer-2",
    local: { cwd: project_path },
  });

  const sessionId = agent.id || `session-${Date.now()}`;
  sessions.set(sessionId, agent);

  respond({ session_id: sessionId, status: "running" });

  const run = agent.prompt(instruction);
  for await (const event of run.stream()) {
    respond({ session_id: sessionId, event: event.type, data: event });
  }
  respond({ session_id: sessionId, event: "completion", status: "done" });
}

async function handleSend({ session_id, message }) {
  const agent = sessions.get(session_id);
  if (!agent) {
    respond({ error: `No session: ${session_id}` });
    return;
  }
  await agent.send(message);
  respond({ ok: true, session_id });
}

function respond(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

rl.on("close", () => process.exit(0));
