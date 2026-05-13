#!/usr/bin/env node
/**
 * Bridge between UCS Python and @cursor/sdk.
 *
 * Protocol (single stdout stream, demuxed by Python):
 *   Python -> Node  {request_id, action, ...}
 *   Node   -> Python (response) {request_id, type:"response", ...}
 *   Node   -> Python (error)    {request_id, type:"error",    error}
 *   Node   -> Python (event)    {type:"event", session_id, event, data}
 *
 * One request_id -> one response or error. Build stream events have no
 * request_id and carry their session_id so Python routes them to a
 * per-session queue.
 */

const readline = require("readline");

if (process.argv.includes("--healthcheck")) {
  console.log(JSON.stringify({ type: "response", status: "ok" }));
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
    fail(null, "Invalid JSON");
    return;
  }

  const requestId = cmd.request_id ?? null;
  try {
    if (cmd.action === "ping") {
      respond(requestId, { ok: true });
    } else if (cmd.action === "create") {
      await handleCreate(cmd, requestId);
    } else if (cmd.action === "send") {
      await handleSend(cmd, requestId);
    } else if (cmd.action === "cancel") {
      await handleCancel(cmd, requestId);
    } else {
      fail(requestId, `Unknown action: ${cmd.action}`);
    }
  } catch (err) {
    fail(requestId, err && err.message ? err.message : String(err));
  }
});

async function handleCreate({ project_path, instruction, model }, requestId) {
  const opts = {
    model: model || "composer-2",
    local: { cwd: project_path },
  };
  if (process.env.CURSOR_API_KEY) {
    opts.apiKey = process.env.CURSOR_API_KEY;
  }

  const agent = await Agent.create(opts);
  const sessionId = agent.id || `session-${Date.now()}`;
  sessions.set(sessionId, agent);

  respond(requestId, { session_id: sessionId, status: "running" });

  // Stream prompt events asynchronously so we can keep accepting commands.
  (async () => {
    try {
      const run = agent.prompt(instruction);
      for await (const event of run.stream()) {
        emit({ session_id: sessionId, event: event.type, data: event });
      }
      emit({ session_id: sessionId, event: "completion", status: "done" });
    } catch (err) {
      emit({
        session_id: sessionId,
        event: "error",
        data: { message: err && err.message ? err.message : String(err) },
      });
    }
  })();
}

async function handleSend({ session_id, message }, requestId) {
  const agent = sessions.get(session_id);
  if (!agent) {
    fail(requestId, `No session: ${session_id}`);
    return;
  }
  await agent.send(message);
  respond(requestId, { ok: true, session_id });
}

async function handleCancel({ session_id }, requestId) {
  const agent = sessions.get(session_id);
  if (!agent) {
    fail(requestId, `No session: ${session_id}`);
    return;
  }
  if (typeof agent.cancel === "function") {
    await agent.cancel();
  }
  sessions.delete(session_id);
  respond(requestId, { ok: true, cancelled: session_id });
}

function respond(requestId, obj) {
  process.stdout.write(
    JSON.stringify({ type: "response", request_id: requestId, ...obj }) + "\n",
  );
}

function fail(requestId, error) {
  process.stdout.write(
    JSON.stringify({ type: "error", request_id: requestId, error }) + "\n",
  );
}

function emit(obj) {
  process.stdout.write(JSON.stringify({ type: "event", ...obj }) + "\n");
}

rl.on("close", () => process.exit(0));
