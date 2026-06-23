"""Local chat window — a browser chat over the SAME agent loop + MCP fleet, with
a LOCAL brain served from the DGX Spark. No Discord, no Gemini, no cloud Claude.

This is the transport half of the Local Spark Agent. The reasoning half is the
unchanged `tools._do_with_claude` loop; the brain half is `ANTHROPIC_BASE_URL`
pointing the Anthropic SDK at the Spark's vLLM (`src/spark.py::serve_*`). This
module only adds a transport: an aiohttp server (the `src/cursor_external.py`
pattern) that serves a one-file chat page and streams the loop's progress + final
answer to the browser over SSE, and routes the loop's ask / propose / confirm
callbacks to inline browser controls.

Run it (env sets the brain; the Makefile target wires the Spark endpoint):
    ANTHROPIC_BASE_URL=http://<spark-ip>:8000 CLAUDE_MODEL=local-brain \
        .venv/bin/python -m src.local_chat_web

Halt, don't heal: if the local brain is not configured/reachable, this REFUSES
to start with the one-command fix. There is no fallback to cloud Claude — a
silent fallback would hide the exact failure the user must see.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from aiohttp import web

from .config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("local_chat_web")


# ---------------------------------------------------------------------------
# Event hub: per-session SSE queue + pending ask/propose/confirm replies
# ---------------------------------------------------------------------------

class ChatSession:
    """One browser session: an outbound event queue + reply futures in flight."""

    def __init__(self) -> None:
        # Bounded so a disconnected browser cannot grow memory without limit.
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self.pending: dict[str, asyncio.Future] = {}


class ChatHub:
    """Routes the agent loop's callbacks to the right browser session and back.

    The agent loop's callbacks are process-global (injected once via init_tools);
    this hub is the single place that fans them out to the live SSE streams and
    blocks the loop on a browser reply for ask / propose / confirm.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, ChatSession] = {}
        self.last_active: str = ""
        # Last final answer per session — the deterministic signal the web-UI
        # gate polls (/last) so it screenshots the browser only once the answer
        # has actually rendered, never on a fixed guess.
        self.answers: dict[str, dict] = {}

    def get(self, key: str) -> ChatSession:
        if key not in self.sessions:
            self.sessions[key] = ChatSession()
        return self.sessions[key]

    async def emit(self, key: str, event: dict) -> None:
        sess = self.get(key)
        try:
            sess.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:  # drop the oldest, keep the newest (status spam is disposable)
                sess.queue.get_nowait()
                sess.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def broadcast(self, event: dict) -> None:
        for key in list(self.sessions):
            await self.emit(key, event)

    async def _await_reply(self, key: str, event: dict, timeout: float) -> str:
        """Emit an interactive event and block for the browser's /reply."""
        sess = self.get(key)
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        sess.pending[rid] = fut
        event = {**event, "id": rid}
        await self.emit(key, event)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ""
        finally:
            sess.pending.pop(rid, None)

    async def ask(self, key: str, question: str, *, timeout: float) -> str:
        return await self._await_reply(key, {"type": "ask", "text": question}, timeout)

    async def propose(self, key: str, title: str, why: str, task: str, *, timeout: float) -> str:
        return await self._await_reply(
            key, {"type": "propose", "title": title, "why": why, "task": task}, timeout
        )

    async def confirm(self, key: str, action_id: str, tool: str, summary: str, *, timeout: float) -> str:
        return await self._await_reply(
            key, {"type": "confirm", "tool": tool, "summary": summary}, timeout
        )

    def resolve(self, key: str, reply_id: str, value: str) -> bool:
        sess = self.sessions.get(key)
        if not sess:
            return False
        fut = sess.pending.get(reply_id)
        if fut is None or fut.done():
            return False
        fut.set_result(value)
        return True


hub = ChatHub()


# ---------------------------------------------------------------------------
# Agent-loop callbacks (closures over the hub) — wired into init_tools.
# ---------------------------------------------------------------------------

async def _post(content: str, thread: Any = None) -> None:
    await hub.broadcast({"type": "note", "text": content})


async def _alert(content: str) -> None:
    await hub.broadcast({"type": "alert", "text": content})


async def _progress(step: str, session_key: str = "") -> None:
    await hub.emit(session_key or hub.last_active, {"type": "status", "text": step})


async def _ask(question: str, session_key: str = "") -> str:
    return await hub.ask(session_key or hub.last_active, question, timeout=300.0)


async def _propose(title: str, why: str = "", task: str = "", session_key: str = "") -> dict:
    """Mirror the bot: return an ack immediately; await approval + run in the
    background. On approve, run the task on the SAME local brain and stream the
    result; on decline, say so. No autonomous execution without the tap."""
    key = session_key or hub.last_active

    async def _await_and_run() -> None:
        from . import tools
        decision = await hub.propose(key, title, why, task, timeout=config.proposal_timeout_sec)
        if decision == "approve":
            await hub.emit(key, {"type": "status", "text": f"approved: {title} — running"})
            try:
                result = await tools._do_with_claude(task, session_key=key)
            except Exception as exc:  # surface loudly; never swallow
                await hub.emit(key, {"type": "error", "text": f"{type(exc).__name__}: {exc}"})
                return
            await hub.emit(key, {"type": "answer", "text": result})
        else:
            await hub.emit(key, {"type": "note", "text": f"declined: {title}"})

    asyncio.create_task(_await_and_run())
    return {"ok": True, "proposed": title, "note": "awaiting your approval in the chat window"}


async def _confirm(action_id: str, tool_name: str, summary: str) -> dict:
    """Tier-I/X confirmation (OFF by default; surfaced loudly if ever enabled).
    Timeout denies — never a silent approve."""
    decision = await hub.confirm(hub.last_active, action_id, tool_name, summary, timeout=120.0)
    return {"approved": decision == "approve"}


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

def _authorized(request: web.Request) -> bool:
    """Shared-secret gate. Loopback with no configured secret is allowed; a
    non-loopback bind without a secret is refused at startup, so by here a
    non-loopback request always requires the secret."""
    secret = config.local_chat_secret
    if not secret and request.remote in ("127.0.0.1", "::1"):
        return True
    given = request.headers.get("X-Chat-Secret") or request.query.get("secret", "")
    return bool(secret) and given == secret


async def _handle_index(_request: web.Request) -> web.Response:
    return web.Response(text=_CHAT_HTML, content_type="text/html")


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "brain": config.brain_base_url or "cloud",
        "model": config.claude_model,
        "sessions": len(hub.sessions),
    })


async def _handle_events(request: web.Request) -> web.StreamResponse:
    if not _authorized(request):
        return web.Response(status=403, text="unauthorized")
    session_key = request.query.get("session", "").strip() or uuid.uuid4().hex
    sess = hub.get(session_key)
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    await resp.write(
        f"data: {json.dumps({'type': 'ready', 'session': session_key, 'brain': config.brain_base_url, 'model': config.claude_model})}\n\n".encode()
    )
    try:
        while True:
            try:
                event = await asyncio.wait_for(sess.queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")  # keepalive
                continue
            await resp.write(f"data: {json.dumps(event)}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        log.info("SSE stream closed for session %s", session_key[:8])
    return resp


async def _handle_chat(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    session_key = str(data.get("session", "")).strip()
    message = str(data.get("message", "")).strip()
    if not session_key or not message:
        return web.json_response({"error": "session and message are required"}, status=400)

    hub.last_active = session_key
    from . import tools

    async def _run() -> None:
        try:
            result = await tools._do_with_claude(message, session_key=session_key)
            state = tools._state_for(session_key)
            tool_fired = bool(getattr(state, "last_tool_trace", None))
            hub.answers[session_key] = {"text": result, "tool_fired": tool_fired}
            await hub.emit(session_key, {"type": "answer", "text": result, "tool_fired": tool_fired})
        except Exception as exc:  # never swallow: the browser must see real failures
            log.exception("do_with_claude failed for session %s", session_key[:8])
            hub.answers[session_key] = {"text": f"{type(exc).__name__}: {exc}", "tool_fired": False, "error": True}
            await hub.emit(session_key, {"type": "error", "text": f"{type(exc).__name__}: {exc}"})

    asyncio.create_task(_run())
    return web.json_response({"ok": True, "accepted": True})


async def _handle_reply(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    session_key = str(data.get("session", "")).strip()
    reply_id = str(data.get("id", "")).strip()
    value = str(data.get("value", ""))
    if not (session_key and reply_id):
        return web.json_response({"error": "session and id are required"}, status=400)
    ok = hub.resolve(session_key, reply_id, value)
    return web.json_response({"ok": ok})


async def _handle_last(request: web.Request) -> web.Response:
    """Return the last final answer for a session (deterministic gate signal)."""
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    session_key = request.query.get("session", "").strip()
    ans = hub.answers.get(session_key)
    if not ans:
        return web.json_response({"answered": False})
    return web.json_response({"answered": True, **ans})


async def _handle_stop(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    session_key = str(data.get("session", "")).strip()
    from . import tools
    await tools._cancel_current_task(session_key)
    await hub.emit(session_key, {"type": "note", "text": "stopping…"})
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# App assembly (one home for the routes; used by main() and the tests)
# ---------------------------------------------------------------------------

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_health)
    app.router.add_get("/events", _handle_events)
    app.router.add_post("/chat", _handle_chat)
    app.router.add_post("/reply", _handle_reply)
    app.router.add_post("/stop", _handle_stop)
    app.router.add_get("/last", _handle_last)
    return app


# ---------------------------------------------------------------------------
# Startup (halt, don't heal)
# ---------------------------------------------------------------------------

async def main() -> int:
    host = config.local_chat_host
    port = config.local_chat_port

    # 1. The brain must be a LOCAL endpoint. No base_url => this would silently
    #    run on cloud Opus, which is exactly the primitive we relocated. Refuse.
    if not config.brain_base_url:
        log.error(
            "ANTHROPIC_BASE_URL is not set — the local chat has no local brain to talk to. "
            "Serve a model on the Spark and point at it, e.g.:\n"
            "  .venv/bin/python scripts/spark_serve.py --node spark1 --start\n"
            "  ANTHROPIC_BASE_URL=http://<spark-ip>:8000 CLAUDE_MODEL=local-brain make local-chat"
        )
        return 2

    # 2. A non-loopback bind without a shared secret is an open agent on the LAN.
    if host not in ("127.0.0.1", "localhost", "::1") and not config.local_chat_secret:
        log.error(
            "Refusing to bind the chat to %s without LOCAL_CHAT_SECRET set — that would "
            "expose an autonomous agent to everyone on the network. Set LOCAL_CHAT_SECRET "
            "(any strong string) and pass it from the browser, or bind to 127.0.0.1.", host,
        )
        return 2

    from .db import init_db
    from .memory import init_memory
    from . import tools
    from .mcp import init_mcp
    from . import preflight

    init_db()
    try:
        init_memory()
    except Exception:
        log.exception("memory init failed — continuing without long-term memory")

    tools.init_tools(
        None,  # no cursor bridge needed for the search/notes/files/MCP surface
        post_callback=_post,
        alert_callback=_alert,
        progress_callback=_progress,
        ask_callback=_ask,
        propose_callback=_propose,
    )

    mcp = None
    try:
        mcp = await init_mcp()
        if mcp:
            mcp.set_confirm_callback(_confirm)
        log.info("MCP fleet started.")
    except Exception:
        log.exception("MCP failed to start — the agent will have no tools; continuing so the "
                      "brain check can still report")

    # 3. The local brain must actually answer the Messages API with a tool_use.
    #    A dead/parser-broken brain is fatal here — never fall back to cloud.
    ok, err, fix, detail = await preflight.probe_local_brain()
    if not ok:
        log.error("LOCAL BRAIN CHECK FAILED — refusing to start (no cloud fallback).\n"
                  "  error: %s\n  fix:   %s\n  detail: %s", err, fix, detail)
        if mcp:
            try:
                await mcp.stop_all()
            except Exception:
                log.exception("error stopping MCP after failed brain check")
        return 2
    log.info("local brain OK: %s", detail)

    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    shown_host = "localhost" if host in ("127.0.0.1", "::1") else host
    print("\n" + "=" * 64)
    print(f"  Aria local chat — brain: {config.claude_model} @ {config.brain_base_url}")
    print(f"  Open: http://{shown_host}:{port}/")
    if config.local_chat_secret:
        print("  (LOCAL_CHAT_SECRET required — enter it in the page header)")
    print("  Ctrl+C to exit.")
    print("=" * 64 + "\n", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    try:
        await stop_event.wait()
    finally:
        log.info("shutting down…")
        await runner.cleanup()
        if mcp:
            try:
                await mcp.stop_all()
            except Exception:
                log.exception("error stopping MCP servers")
    return 0


# ---------------------------------------------------------------------------
# The one-file chat UI
# ---------------------------------------------------------------------------

_CHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Aria — local</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font: 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#0b0d10; color:#e6e9ee; height:100vh; display:flex; flex-direction:column; }
  header { padding:10px 16px; border-bottom:1px solid #1c2128; display:flex; gap:12px;
           align-items:center; background:#0e1116; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#f0883e; }
  header .dot.live { background:#3fb950; }
  header .meta { font-size:12px; color:#8b949e; }
  header input { background:#0b0d10; border:1px solid #30363d; color:#e6e9ee; border-radius:6px;
                 padding:4px 8px; font-size:12px; width:140px; }
  #log { flex:1; overflow-y:auto; padding:18px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:760px; padding:10px 14px; border-radius:12px; white-space:pre-wrap;
         word-wrap:break-word; }
  .user { align-self:flex-end; background:#1f6feb; color:#fff; border-bottom-right-radius:3px; }
  .assistant { align-self:flex-start; background:#161b22; border:1px solid #21262d;
               border-bottom-left-radius:3px; }
  .assistant pre { background:#0b0d10; border:1px solid #21262d; padding:10px; border-radius:8px;
                   overflow-x:auto; }
  .status { align-self:flex-start; color:#8b949e; font-style:italic; font-size:13px; padding:0 6px; }
  .note { align-self:flex-start; color:#6e7681; font-size:13px; padding:0 6px; }
  .alert { align-self:flex-start; color:#d29922; font-size:13px; padding:0 6px; }
  .error { align-self:flex-start; color:#f85149; font-size:13px; padding:0 6px; }
  .prompt { align-self:flex-start; max-width:760px; background:#161b22; border:1px solid #30363d;
            border-radius:12px; padding:12px 14px; }
  .prompt .q { margin-bottom:8px; }
  .prompt .row { display:flex; gap:8px; }
  .prompt input { flex:1; background:#0b0d10; border:1px solid #30363d; color:#e6e9ee;
                  border-radius:6px; padding:8px; }
  .prompt button { background:#238636; border:0; color:#fff; border-radius:6px; padding:8px 14px;
                   cursor:pointer; }
  .prompt button.no { background:#30363d; }
  footer { padding:12px 16px; border-top:1px solid #1c2128; background:#0e1116; }
  #form { display:flex; gap:10px; max-width:980px; margin:0 auto; }
  #input { flex:1; background:#0b0d10; border:1px solid #30363d; color:#e6e9ee; border-radius:10px;
           padding:11px 14px; font:inherit; resize:none; max-height:160px; }
  #send { background:#1f6feb; border:0; color:#fff; border-radius:10px; padding:0 20px; cursor:pointer;
          font-weight:600; }
  #stop { background:#30363d; border:0; color:#e6e9ee; border-radius:10px; padding:0 14px; cursor:pointer; }
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <strong>Aria</strong>
    <span class="meta" id="meta">connecting…</span>
    <span style="flex:1"></span>
    <input id="secret" placeholder="secret (if set)" autocomplete="off"/>
  </header>
  <div id="log"></div>
  <footer>
    <div id="form">
      <textarea id="input" rows="1" placeholder="Ask Aria to search, pull a note, traverse files…"></textarea>
      <button id="send">Send</button>
      <button id="stop" title="Stop the current task">Stop</button>
    </div>
  </footer>
<script>
(function(){
  const log = document.getElementById('log');
  const meta = document.getElementById('meta');
  const dot = document.getElementById('dot');
  const input = document.getElementById('input');
  const secretBox = document.getElementById('secret');
  const urlParams = new URLSearchParams(location.search);
  let session = urlParams.get('session') || localStorage.getItem('aria_session') || (Math.random().toString(36).slice(2) + Date.now().toString(36));
  localStorage.setItem('aria_session', session);
  secretBox.value = urlParams.get('secret') || localStorage.getItem('aria_secret') || '';
  secretBox.addEventListener('change', () => { localStorage.setItem('aria_secret', secretBox.value); connect(); });

  function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function render(text){
    // minimal: fence ```code``` then preserve newlines
    const parts = (text||'').split(/```/);
    let html = '';
    for (let i=0;i<parts.length;i++){
      if (i % 2 === 1) html += '<pre>'+esc(parts[i])+'</pre>';
      else html += esc(parts[i]).replace(/\n/g,'<br>');
    }
    return html;
  }
  function add(cls, html){
    const d = document.createElement('div');
    d.className = 'msg ' + cls; d.innerHTML = html;
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }
  function line(cls, text){
    const d = document.createElement('div'); d.className = cls; d.textContent = text;
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }

  async function post(path, body){
    const headers = {'Content-Type':'application/json'};
    if (secretBox.value) headers['X-Chat-Secret'] = secretBox.value;
    return fetch(path, {method:'POST', headers, body: JSON.stringify(Object.assign({session}, body))});
  }

  function promptCard(ev){
    const card = document.createElement('div'); card.className = 'prompt';
    if (ev.type === 'ask'){
      card.innerHTML = '<div class="q">'+esc(ev.text)+'</div>';
      const row = document.createElement('div'); row.className='row';
      const inp = document.createElement('input'); const btn = document.createElement('button'); btn.textContent='Answer';
      row.appendChild(inp); row.appendChild(btn); card.appendChild(row);
      btn.onclick = () => { post('/reply', {id: ev.id, value: inp.value}); card.remove(); line('note','you: '+inp.value); };
    } else { // propose / confirm
      const title = ev.title || ev.tool || 'Approve?';
      card.innerHTML = '<div class="q"><strong>'+esc(title)+'</strong><br>'+esc(ev.why||ev.summary||'')+'</div>';
      const row = document.createElement('div'); row.className='row';
      const yes = document.createElement('button'); yes.textContent='Approve';
      const no = document.createElement('button'); no.textContent='Decline'; no.className='no';
      row.appendChild(yes); row.appendChild(no); card.appendChild(row);
      yes.onclick = () => { post('/reply', {id: ev.id, value:'approve'}); card.remove(); };
      no.onclick = () => { post('/reply', {id: ev.id, value:'reject'}); card.remove(); };
    }
    log.appendChild(card); log.scrollTop = log.scrollHeight;
  }

  let es;
  function connect(){
    if (es) es.close();
    const q = '/events?session='+encodeURIComponent(session)+(secretBox.value?('&secret='+encodeURIComponent(secretBox.value)):'');
    es = new EventSource(q);
    es.onopen = () => { dot.classList.add('live'); };
    es.onerror = () => { dot.classList.remove('live'); meta.textContent = 'reconnecting…'; };
    es.onmessage = (e) => {
      let ev; try { ev = JSON.parse(e.data); } catch(_) { return; }
      if (ev.type === 'ready'){ meta.textContent = (ev.model||'?') + ' @ ' + (ev.brain||'local'); return; }
      if (ev.type === 'status') return line('status', '→ ' + ev.text);
      if (ev.type === 'note')   return line('note', ev.text);
      if (ev.type === 'alert')  return line('alert', ev.text);
      if (ev.type === 'error')  return line('error', ev.text);
      if (ev.type === 'answer') return add('assistant', render(ev.text));
      if (ev.type === 'ask' || ev.type === 'propose' || ev.type === 'confirm') return promptCard(ev);
    };
  }

  function send(){
    const text = input.value.trim(); if (!text) return;
    add('user', render(text)); input.value=''; input.style.height='auto';
    post('/chat', {message: text});
  }
  document.getElementById('send').onclick = send;
  document.getElementById('stop').onclick = () => post('/stop', {});
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  });
  input.addEventListener('input', () => { input.style.height='auto'; input.style.height=Math.min(160,input.scrollHeight)+'px'; });
  connect();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
