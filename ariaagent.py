#!/usr/bin/env python3
"""
Aria — Autonomous Reasoning Intelligent Agent. Standalone local AI agent with a web UI.
Run:  python aria.py        (stdlib only, no installs)
Opens http://127.0.0.1:8400 — tools operate in the launch directory.

Network: the server listens on all interfaces (0.0.0.0) so other devices on your
LAN can connect to http://<your-lan-ip>:8400. The FIRST device to ever connect
becomes the trusted "host". Any other IP that connects must be approved by the
host (a prompt appears in the host's UI). Until approved, that device just sees
a "waiting for approval" screen and cannot use any tools.
"""
import json, os, re, signal, ssl, subprocess, threading, time, uuid, html as htmlmod
import urllib.request, urllib.parse, urllib.error, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8400
ROOT = os.path.realpath(os.getcwd())
PENDING = {}          # tool confirmation -> approval
MAX_TURNS = 10
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"}

# ── LAN access control ──────────────────────────────────────────────
ACCESS = {"host": None, "trusted": set(), "pending": {}}  # pending: ip -> {"ts": float, "ua": str}
ACCESS_LOCK = threading.Lock()

def access_check(ip):
    """Returns 'ok', 'pending', or 'new' for a given client IP."""
    with ACCESS_LOCK:
        if ACCESS["host"] is None:
            ACCESS["host"] = ip
            ACCESS["trusted"].add(ip)
            return "ok"
        if ip == ACCESS["host"] or ip in ACCESS["trusted"]:
            return "ok"
        if ip in ACCESS["pending"]:
            return "pending"
        return "new"

def access_register_pending(ip, ua):
    with ACCESS_LOCK:
        ACCESS["pending"][ip] = {"ts": time.time(), "ua": ua}

# ── active shell processes (so "stop" actually kills them) ──────────
ACTIVE_SHELLS = {}  # threading.get_ident() -> Popen

# ── tools ──────────────────────────────────────────────────────────
TOOL_SPEC = f"""
You have REAL tools.

To CREATE or OVERWRITE a file, use this exact block (RAW content — no JSON, no escaping, no backticks):
<<write:relative/path.ext>>
...full file content here...
<<end>>

For every OTHER tool, reply with ONLY a JSON object (no prose, no backticks), then STOP:
{{"tool":"shell","args":{{"command":"uname -a"}}}}
The result arrives as the next user message; continue from there (you may chain calls).
Tools:
- read_file   args: {{"path":"relative/path"}}
- edit_file   args: {{"path":"relative/path","old_str":"...","new_str":"..."}} (old_str must be unique)
- shell       args: {{"command":"..."}} (workspace cwd, 60s timeout)
- search      args: {{"query":"..."}}
- browse      args: {{"url":"https://..."}}
Workspace: {ROOT}
RULES:
- NEVER put file content inside JSON — always use the <<write:...>> block for files.
- NEVER describe a command in prose instead of calling it.
- One tool call per reply. When done with tools, write a normal final answer (no JSON, no blocks).
""".strip()

def safe_path(p):
    full = os.path.realpath(os.path.join(ROOT, p))
    if full != ROOT and not full.startswith(ROOT + os.sep):
        raise ValueError("path escapes workspace")
    return full

def t_read_file(a):
    with open(safe_path(a["path"]), encoding="utf-8", errors="replace") as f:
        d = f.read()
    return d[:12000] + ("\n…[truncated]" if len(d) > 12000 else "")

def t_write_file(a):
    fp = safe_path(a["path"])
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(a["content"])
    return f"Wrote {len(a['content'])} bytes to {a['path']}"

def t_edit_file(a):
    fp = safe_path(a["path"])
    with open(fp, encoding="utf-8") as f:
        d = f.read()
    n = d.count(a["old_str"])
    if n != 1:
        return f"ERROR: old_str found {n} times (need exactly 1)"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(d.replace(a["old_str"], a["new_str"], 1))
    return f"Edited {a['path']}"

def t_shell(a):
    ident = threading.get_ident()
    try:
        proc = subprocess.Popen(
            a["command"], shell=True, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=os.setsid,
        )
        ACTIVE_SHELLS[ident] = proc
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_proc(proc)
            return "ERROR: timed out (60s)"
        return f"exit {proc.returncode}\n{(out or '(no output)').strip()[:6000]}"
    except KeyboardInterrupt:
        return "ERROR: stopped by user"
    finally:
        ACTIVE_SHELLS.pop(ident, None)

def _kill_proc(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass

def kill_active_shell(ident):
    proc = ACTIVE_SHELLS.get(ident)
    if proc and proc.poll() is None:
        _kill_proc(proc)
        return True
    return False

def _strip_tags(s):
    return htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()

def _fetch(url, data=None, timeout=10):
    """GET/POST with realistic headers; retries once with relaxed SSL on cert errors."""
    req = urllib.request.Request(url, data=data, headers={
        **UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode("utf-8", "replace")
        raise

def _ddg_url(href):
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
    return urllib.parse.unquote(q[0]) if q else href

def _dedupe(pairs, n=6):
    out, seen = [], set()
    for url, title in pairs:
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append(f"- {_strip_tags(title)}\n  {url}")
        if len(out) >= n:
            break
    return out

def t_search(a):
    query, errs = a["query"], []
    # 1) DuckDuckGo Lite
    try:
        page = _fetch("https://lite.duckduckgo.com/lite/",
                      data=urllib.parse.urlencode({"q": query}).encode())
        hits = re.findall(r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', page, re.S) \
            or re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        out = _dedupe((_ddg_url(h), t) for h, t in hits)
        if out:
            return "\n".join(out)
        errs.append("ddg-lite: parsed 0 results (likely bot challenge)")
    except Exception as e:
        errs.append(f"ddg-lite: {type(e).__name__}: {e}")
    # 2) DuckDuckGo HTML
    try:
        page = _fetch("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
        hits = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        out = _dedupe((_ddg_url(h), t) for h, t in hits)
        if out:
            return "\n".join(out)
        errs.append("ddg-html: parsed 0 results")
    except Exception as e:
        errs.append(f"ddg-html: {type(e).__name__}: {e}")
    # 3) Bing
    try:
        page = _fetch("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query))
        hits = re.findall(r'<h2><a href="(http[^"]+)"[^>]*>(.*?)</a></h2>', page, re.S)
        out = _dedupe(hits)
        if out:
            return "\n".join(out)
        errs.append("bing: parsed 0 results")
    except Exception as e:
        errs.append(f"bing: {type(e).__name__}: {e}")
    return ("ERROR: all search engines failed:\n" + "\n".join("  " + e for e in errs)
            + "\nTell the user search failed and show these reasons; do not invent results.")

def t_browse(a):
    page = _fetch(a["url"], timeout=12)
    page = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", page)
    text = _strip_tags(page)
    return text[:6000] + ("…[truncated]" if len(text) > 6000 else "")

TOOLS = {"read_file": t_read_file, "write_file": t_write_file, "edit_file": t_edit_file,
         "shell": t_shell, "search": t_search, "browse": t_browse}
CONFIRM = {"shell": "confirmShell", "write_file": "confirmWrite", "edit_file": "confirmWrite"}

def tool_detail(n, a):
    return {"read_file": a.get("path", ""), "write_file": a.get("path", ""),
            "edit_file": a.get("path", ""), "shell": "$ " + a.get("command", ""),
            "search": '"%s"' % a.get("query", ""), "browse": a.get("url", "")}.get(n, "")

def partial_detail(name, text):
    pats = {"read_file": r'"path"\s*:\s*"([^"]*)', "edit_file": r'"path"\s*:\s*"([^"]*)',
            "shell": r'"command"\s*:\s*"([^"]*)', "search": r'"query"\s*:\s*"([^"]*)',
            "browse": r'"url"\s*:\s*"([^"]*)'}
    m = re.search(pats.get(name, r"$^"), text)
    d = m.group(1) if m else ""
    return ("$ " + d) if name == "shell" else d

# ── tool-call extraction ───────────────────────────────────────────
WRITE_RE = re.compile(r"<<write:([^\n>]+)>>\r?\n?")

def extract_tool(text):
    if '"tool"' not in text:
        return None, -1, -1
    for m in re.finditer(r"\{", text):
        s = m.start()
        depth, instr, escp = 0, False, False
        for i in range(s, len(text)):
            ch = text[i]
            if instr:
                if escp: escp = False
                elif ch == "\\": escp = True
                elif ch == '"': instr = False
            elif ch == '"': instr = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[s:i + 1]
                    if '"tool"' in chunk:
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict) and "tool" in obj:
                                return obj, s, i + 1
                        except ValueError:
                            pass
                    break
    return None, -1, -1

# ── LLM streaming ──────────────────────────────────────────────────
def llm_stream(cfg, messages):
    base = cfg["baseUrl"].rstrip("/")
    if cfg["platform"] == "ollama":
        url = base + "/api/chat"
        body = {"model": cfg["model"], "messages": messages, "stream": True,
                "options": {"temperature": cfg.get("temp", 0.7)}}
    else:
        if not base.endswith("/v1"): base += "/v1"
        url = base + "/chat/completions"
        body = {"model": cfg["model"], "messages": messages, "stream": True,
                "temperature": cfg.get("temp", 0.7)}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"): line = line[5:].strip()
            if not line or line == "[DONE]": continue
            try: j = json.loads(line)
            except ValueError: continue
            tok = (j.get("message", {}) or {}).get("content", "") if cfg["platform"] == "ollama" \
                else ((j.get("choices") or [{}])[0].get("delta", {}) or {}).get("content", "")
            if tok: yield tok

# ── agent loop ─────────────────────────────────────────────────────
TRAIL = re.compile(r"(```json|```html|```|<<tool>>|\s)+$")

def run_agent(cfg, history, emit):
    sysp = cfg.get("sysPrompt", "Your Aria (Autonomous Reasoning Intelligent Agent), a local AI agent.")
    if cfg.get("deepResearch"):
        sysp += " Deep-research mode: plan multi-step search/browse investigations, verify claims, give a structured report."
    messages = [{"role": "system", "content": sysp + "\n\n" + TOOL_SPEC}] + history
    bad = 0

    for _ in range(MAX_TURNS):
        full, emitted, call = "", 0, None
        gen_id, gen_name, gen_from, gen_mark = None, None, 0, 0

        for tok in llm_stream(cfg, messages):
            full += tok
            cands = []
            jt = full.find('"tool"')
            if jt != -1:
                b = full.rfind("{", 0, jt)
                cands.append(b if b != -1 else jt)
            mt = full.find("<<tool")
            if mt != -1: cands.append(mt)
            wt = full.find("<<write:")
            if wt != -1: cands.append(wt)
            limit = min(cands) if cands else len(full) - 12
            if limit > emitted:
                emit({"type": "token", "text": full[emitted:limit]}); emitted = limit

            if cands and gen_id is None:
                gen_id, gen_from, gen_mark = uuid.uuid4().hex[:8], min(cands), len(full)
            if gen_id:
                if gen_name is None:
                    wm = WRITE_RE.search(full)
                    if wt != -1 and wm and wm.start() == wt:
                        gen_name = "write_file"
                        emit({"type": "tool", "id": gen_id, "tool": "write_file",
                              "detail": wm.group(1).strip(), "status": "gen"})
                    else:
                        nm = re.search(r'"tool"\s*:\s*"(\w+)"', full)
                        if nm:
                            gen_name = nm.group(1)
                            emit({"type": "tool", "id": gen_id, "tool": gen_name,
                                  "detail": partial_detail(gen_name, full), "status": "gen"})
                elif len(full) - gen_mark > 350:
                    gen_mark = len(full)
                    emit({"type": "tool_progress", "id": gen_id, "size": len(full) - gen_from})

            wm = WRITE_RE.search(full)
            if wm:
                e = full.find("<<end>>", wm.end())
                if e != -1:
                    call = ("write", wm, full[wm.end():e].rstrip("\n")); break
            if '"tool"' in full:
                obj, s, _ = extract_tool(full)
                if obj:
                    call = ("json", obj, s); break

        if call is None:
            wm = WRITE_RE.search(full)
            if wm and len(full) > wm.end():
                call = ("write", wm, full[wm.end():].rstrip("\n"))

        if call is None:
            if '"tool"' in full or "<<tool" in full or "<<write:" in full:
                if gen_id:
                    emit({"type": "tool_end", "id": gen_id, "ok": False, "output": "malformed tool call"})
                bad += 1
                if bad > 2:
                    emit({"type": "token", "text": "\n\n⚠️ The model kept producing malformed tool calls. Try a larger / more instruction-tuned model."})
                    emit({"type": "done"}); return
                messages += [{"role": "assistant", "content": full},
                             {"role": "user", "content": 'Your tool call was malformed. Use the <<write:path>> block for files, or ONLY valid JSON like {"tool":"shell","args":{"command":"ls"}}.'}]
                continue
            if len(full) > emitted:
                emit({"type": "token", "text": full[emitted:]})
            emit({"type": "done"}); return

        if call[0] == "write":
            name, args, s = "write_file", {"path": call[1].group(1).strip(), "content": call[2]}, call[1].start()
        else:
            _, obj, s = call
            name, args = obj.get("tool"), obj.get("args", {}) or {}
        pre = TRAIL.sub("", full[:s])
        if len(pre) > emitted:
            emit({"type": "token", "text": pre[emitted:]})
        cid = gen_id or uuid.uuid4().hex[:8]
        detail = tool_detail(name, args)

        if name not in TOOLS:
            result = f"ERROR: unknown tool '{name}'. Valid: {', '.join(TOOLS)}"
            emit({"type": "tool", "id": cid, "tool": name or "?", "detail": detail, "status": "run"})
        else:
            flag = CONFIRM.get(name)
            if flag and cfg.get(flag, True):
                emit({"type": "tool", "id": cid, "tool": name, "detail": detail, "status": "confirm"})
                ev = threading.Event(); PENDING[cid] = {"ev": ev, "approved": None, "always": False}
                ev.wait(300)
                entry = PENDING.pop(cid, {})
                if not entry.get("approved"):
                    emit({"type": "tool_end", "id": cid, "ok": False, "output": "Denied by user"})
                    messages += [{"role": "assistant", "content": full},
                                 {"role": "user", "content": f"Tool result for {name}:\nUser DENIED this action. Do not retry it. Adjust or explain."}]
                    continue
                if entry.get("always"):
                    emit({"type": "always_allow", "flag": flag})
                emit({"type": "tool", "id": cid, "tool": name, "detail": detail, "status": "run"})
            else:
                emit({"type": "tool", "id": cid, "tool": name, "detail": detail, "status": "run"})
            try:
                result = TOOLS[name](args)
            except KeyError as e:
                result = f"ERROR: missing arg {e}"
            except Exception as e:
                result = f"ERROR: {e}"

        emit({"type": "tool_end", "id": cid, "ok": not str(result).startswith("ERROR"),
              "output": str(result)[:2500]})
        messages += [{"role": "assistant", "content": full},
                     {"role": "user", "content": f"Tool result for {name}:\n{result}\n\nContinue: call another tool if needed, otherwise give the final answer in plain language (no JSON, no blocks)."}]

    emit({"type": "token", "text": "\n\n⚠️ Reached the tool-call limit for one message."})
    emit({"type": "done"})

# ── HTTP server ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _fetch_json(self, url, timeout=6):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Aria/1.0", "Accept": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                return json.loads(urllib.request.urlopen(req, timeout=timeout, context=ctx).read())
            raise

    def _waiting_page(self, ip):
        b = WAIT_HTML.replace("__IP__", ip).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    # -- access gate, applied to every request --
    def _gate(self, ip):
        """Returns True if allowed to proceed. Handles the response itself if not."""
        state = access_check(ip)
        if state == "ok":
            return True
        if self.path == "/":
            if state == "new":
                access_register_pending(ip, self.headers.get("User-Agent", "?"))
            self._waiting_page(ip)
            return False
        # API calls from unapproved devices
        if state == "new":
            access_register_pending(ip, self.headers.get("User-Agent", "?"))
        self._json({"ok": False, "pending": True,
                     "error": "Waiting for the host device to approve this device."}, 403)
        return False

    def do_GET(self):
        ip = self.client_address[0]
        p = urllib.parse.urlparse(self.path)

        # Access-status endpoints must work even for unapproved devices
        if p.path == "/api/access/status":
            state = access_check(ip)
            self._json({"state": state, "isHost": ip == ACCESS["host"]})
            return
        if p.path == "/api/access/pending":
            if ip != ACCESS["host"]:
                self._json({"pending": []}); return
            with ACCESS_LOCK:
                lst = [{"ip": k, **v} for k, v in ACCESS["pending"].items()]
            self._json({"pending": lst, "trusted": sorted(ACCESS["trusted"])})
            return

        if not self._gate(ip):
            return

        if p.path == "/":
            b = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p.path == "/api/models":
            q = urllib.parse.parse_qs(p.query)
            plat, url = q.get("platform", [""])[0], q.get("url", [""])[0].rstrip("/")
            try:
                if plat == "ollama":
                    r = self._fetch_json(url + "/api/tags")
                    models = [m["name"] for m in r.get("models", [])]
                else:
                    if not url.endswith("/v1"): url += "/v1"
                    r = self._fetch_json(url + "/models")
                    models = [m["id"] for m in r.get("data", [])]
                self._json({"ok": True, "models": models, "cwd": ROOT})
            except (subprocess.TimeoutExpired, urllib.error.URLError) as e:
                err = str(e)
                if "timed out" in err.lower():
                    self._json({"ok": False, "error": "Connection timed out — is your runtime running?", "cwd": ROOT})
                elif "Connection refused" in err:
                    self._json({"ok": False, "error": "Connection refused — start your runtime and check the endpoint URL", "cwd": ROOT})
                else:
                    self._json({"ok": False, "error": err, "cwd": ROOT})
            except Exception as e:
                self._json({"ok": False, "error": str(e), "cwd": ROOT})
        else:
            self.send_error(404)

    def do_POST(self):
        ip = self.client_address[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"

        if self.path == "/api/access/respond":
            if ip != ACCESS["host"]:
                self._json({"ok": False, "error": "Only the host device can manage access."}, 403)
                return
            data = json.loads(raw or b"{}")
            target, approve = data.get("ip"), bool(data.get("approve"))
            with ACCESS_LOCK:
                ACCESS["pending"].pop(target, None)
                if approve:
                    ACCESS["trusted"].add(target)
                else:
                    ACCESS["trusted"].discard(target)
            self._json({"ok": True})
            return

        if not self._gate(ip):
            return

        data = json.loads(raw or b"{}")
        if self.path == "/api/approve":
            e = PENDING.get(data.get("id"))
            if e:
                e["approved"] = bool(data.get("approved"))
                e["always"] = bool(data.get("always"))
                e["ev"].set()
            self._json({"ok": True})
        elif self.path == "/api/stop":
            killed = False
            for ident, proc in list(ACTIVE_SHELLS.items()):
                if proc.poll() is None:
                    _kill_proc(proc); killed = True
            self._json({"ok": True, "killed": killed})
        elif self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache"); self.end_headers()
            def emit(o):
                self.wfile.write(f"data: {json.dumps(o)}\n\n".encode()); self.wfile.flush()
            try:
                run_agent(data["cfg"], data["messages"], emit)
            except (BrokenPipeError, ConnectionError):
                kill_active_shell(threading.get_ident())
            except Exception as e:
                try: emit({"type": "error", "text": str(e)}); emit({"type": "done"})
                except Exception: pass
        else:
            self.send_error(404)

# ── minimal "waiting for approval" page for unapproved LAN devices ──
WAIT_HTML = r"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Aria — Waiting</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0e0f11;color:#e8eaed;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
.box{max-width:360px;padding:32px}
.mark{width:48px;height:48px;border-radius:14px;background:#e8eaed;color:#16181d;display:flex;align-items:center;
justify-content:center;font-size:20px;margin:0 auto 16px;font-weight:700}
h1{font-size:18px;margin-bottom:8px}p{color:#8a94a0;font-size:13.5px;line-height:1.6}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#b7940e;margin-right:6px;
animation:pu 1.2s ease-in-out infinite}
@keyframes pu{0%,100%{opacity:.35}50%{opacity:1}}
.ip{font-family:monospace;color:#e8eaed}
</style></head><body><div class="box">
<div class="mark">✦</div><h1><span class="dot"></span>Waiting for approval</h1>
<p>This device (<span class="ip">__IP__</span>) is asking to connect to Aria.
Approve the request on the host device to continue. This page will refresh automatically.</p>
</div>
<script>setTimeout(()=>location.reload(),3000)</script>
</body></html>"""

# ── UI ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aria</title><style>
:root{--bg:#fff;--soft:#f4f5f6;--softer:#f9fafb;--bd:#e2e4e8;--bdh:#cdd0d6;--tx:#16181d;--mut:#63707a;--dim:#94a0ab;
--ac:#16181d;--acs:rgba(22,24,29,.07);--ok:#0e9f6e;--err:#d92d3f;--warn:#b7940e;--rs:10px;--rsm:8px;--msg:14.5px;--maxw:740px;
--ink:linear-gradient(135deg,#15171c 0%,#4b4f59 50%,#8a8f9b 100%);--pre:#0c0d0f;--pretx:#e2e6ed;
--mono:ui-monospace,'SF Mono','Cascadia Code',Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Display','Segoe UI',Roboto,Oxygen,Ubuntu,sans-serif;
--sh:0 1px 3px rgba(20,22,28,.04),0 8px 28px rgba(20,22,28,.06);--ease:cubic-bezier(.25,.1,.25,1)}
[data-theme=dark]{--bg:#0e0f11;--soft:#141519;--softer:#191a1f;--bd:#23252b;--bdh:#2f323a;--tx:#e8eaed;--mut:#8a94a0;
--dim:#5a6370;--acs:rgba(232,234,237,.07);--ink:linear-gradient(135deg,#f2f3f6 0%,#b9bec9 50%,#7e8693 100%);--pre:#060708;
--sh:0 1px 3px rgba(0,0,0,.3),0 8px 28px rgba(0,0,0,.4)}
*{margin:0;padding:0;box-sizing:border-box}html,body{height:100%}
body{font-family:var(--sans);background:var(--bg);color:var(--tx);font-size:14px;line-height:1.6;overflow:hidden;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;outline:none}
input,select,textarea{font:inherit;color:inherit;outline:none}
a{color:var(--ac);text-decoration:underline;text-decoration-color:var(--bdh)}
::-webkit-scrollbar{width:7px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdh);border-radius:99px;border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:var(--dim);background-clip:content-box}
#splash{position:fixed;inset:0;z-index:300;display:flex;align-items:center;justify-content:center;
background:linear-gradient(160deg,#fcfcfd,#f2f3f5 50%,#e8eaee);transition:opacity .5s ease}
[data-theme=dark] #splash{background:linear-gradient(160deg,#141519,#0e0f11 50%,#08090a)}
#splash.fade{opacity:0;pointer-events:none}
#splash .logo{display:flex;flex-direction:column;align-items:center;gap:8px;opacity:0;animation:wIn .7s var(--ease) .05s forwards}
#splash .mark2{width:52px;height:52px;border-radius:16px;background:var(--ac);color:var(--bg);display:flex;align-items:center;justify-content:center;font-size:22px}
[data-theme=dark] #splash .mark2{background:#e8eaed;color:#16181d}
#splash h1{font-size:clamp(28px,5vw,48px);font-weight:700;letter-spacing:-.03em;background:var(--ink);
-webkit-background-clip:text;background-clip:text;color:transparent}
#splash .tag{color:var(--dim);font-size:clamp(13px,2vw,15px);letter-spacing:.02em;margin-top:-2px}
@keyframes wIn{0%{opacity:0;transform:translateY(16px);filter:blur(6px)}100%{opacity:1;transform:none;filter:blur(0)}}
@keyframes mIn{0%{opacity:0;transform:translateY(6px)}100%{opacity:1;transform:none}}
@keyframes bl{50%{opacity:0}}
@keyframes pu{0%,100%{transform:scale(.55);opacity:.4}50%{transform:scale(1);opacity:1}}
@keyframes rot{100%{transform:rotate(360deg)}}
@keyframes tIn{0%{opacity:0;transform:translateY(18px) scale(.92)}100%{opacity:1;transform:translateY(0) scale(1)}}
@keyframes shim{0%{background-position:200% center}100%{background-position:-200% center}}
@keyframes btnPop{0%{transform:scale(1)}50%{transform:scale(1.04)}100%{transform:scale(1)}}
@keyframes fadeUp{0%{opacity:0;transform:translateY(8px)}100%{opacity:1;transform:none}}
#setupOv{position:fixed;inset:0;z-index:250;display:none;align-items:center;justify-content:center;background:var(--soft);padding:20px}
#setupOv.show{display:flex}
.panel{width:min(440px,100%);background:var(--bg);border:1px solid var(--bd);border-radius:20px;padding:30px 32px 24px;box-shadow:var(--sh);animation:wIn .4s var(--ease)}
.steps{display:flex;gap:6px;margin-bottom:20px}
.steps .dot{flex:1;height:3px;border-radius:99px;background:var(--bd);transition:background .3s}.steps .dot.on{background:var(--ac)}
.step{display:none}.step.active{display:block}
.panel h2{font-size:19px;font-weight:700;margin-bottom:4px}
.panel .sub{color:var(--mut);font-size:13px;margin-bottom:20px;line-height:1.5}
.f{margin-bottom:14px}.f label{display:block;font-size:12px;font-weight:600;color:var(--mut);margin-bottom:5px;letter-spacing:.01em}
.f select,.f input{width:100%;padding:10px 13px;border:1px solid var(--bd);border-radius:var(--rs);background:var(--softer);transition:border-color .15s}
.f select:focus,.f input:focus{border-color:var(--ac)}
.f input{font-family:var(--mono);font-size:13px}
.tline{display:flex;align-items:center;gap:9px;font-size:12.5px;min-height:20px;margin:-2px 0 10px;padding:2px 0}
.tline .d{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.acts{display:flex;justify-content:space-between;align-items:center;margin-top:18px;gap:10px}
.btn{padding:10px 22px;border-radius:10px;background:var(--ac);color:var(--bg);font-weight:600;font-size:13.5px;
transition:opacity .15s,transform .12s}
[data-theme=dark] .btn{background:#e8eaed;color:#16181d}
.btn:active{transform:scale(.96)}
.btn:disabled{opacity:.3;cursor:not-allowed;transform:none}
.bg2{color:var(--mut);padding:9px 14px;border-radius:10px;font-size:13px;transition:background .12s}
.bg2:hover{background:var(--soft);color:var(--tx)}
#app{display:none;height:100%}#app.active{display:flex}
aside{width:256px;flex-shrink:0;background:var(--soft);border-right:1px solid var(--bd);display:flex;flex-direction:column;
transition:transform .25s var(--ease)}
.shead{display:flex;align-items:center;gap:10px;padding:16px 16px 8px}
.mark{width:30px;height:30px;border-radius:10px;background:var(--ac);color:var(--bg);display:flex;align-items:center;
justify-content:center;font-size:14px;font-weight:700}
[data-theme=dark] .mark{background:#e8eaed;color:#16181d}
.shead b{font-size:15px;letter-spacing:-.01em}
.nav{padding:0 10px 6px;display:flex;flex-direction:column;gap:2px}
.nbtn{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:var(--rsm);font-size:13.5px;font-weight:500;width:100%;text-align:left;transition:background .12s}
.nbtn:hover{background:var(--acs)}.nbtn .ni{width:18px;text-align:center;font-size:14px}
.nbtn kbd{margin-left:auto;font-size:10px;color:var(--dim);border:1px solid var(--bd);border-radius:5px;padding:1px 5px;font-family:var(--mono)}
.nbtn.tog{background:var(--acs);font-weight:600}
.slabel{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);font-weight:600;padding:10px 18px 5px}
#chatList{flex:1;overflow-y:auto;padding:0 10px 8px}
.ci{display:flex;align-items:center;width:100%;padding:6px 8px 6px 11px;border-radius:var(--rsm);color:var(--mut);font-size:13px;gap:6px;cursor:pointer;transition:background .12s}
.ci span{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci:hover{background:var(--acs);color:var(--tx)}.ci.on{background:var(--acs);color:var(--tx);font-weight:600}
.ci .del{opacity:0;color:var(--dim);font-size:12px;padding:2px 5px;border-radius:6px;transition:opacity .12s}
.ci:hover .del{opacity:1}.ci .del:hover{color:var(--err)}
.sfoot{padding:10px 14px;border-top:1px solid var(--bd);display:flex;align-items:center;gap:9px}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--warn);flex-shrink:0}.sdot.ok{background:var(--ok)}.sdot.err{background:var(--err)}
.sfoot small{color:var(--mut);font-size:11.5px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gear{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--mut);transition:background .12s;position:relative}
.gear:hover{background:var(--acs);color:var(--tx)}
.gear .bdg{position:absolute;top:-2px;right:-2px;width:9px;height:9px;border-radius:50%;background:var(--err);
display:none;border:2px solid var(--soft)}
.gear .bdg.show{display:block}
main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
.top{height:50px;flex-shrink:0;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:12px;padding:0 14px;background:var(--bg)}
.mdd{position:relative}
.mdd>button{display:flex;align-items:center;gap:8px;padding:5px 10px;border-radius:8px;font-weight:600;font-size:13.5px;transition:background .12s}
.mdd>button:hover{background:var(--soft)}.mdd .car{color:var(--dim);font-size:9px;margin-top:1px}
.menu{position:absolute;top:calc(100% + 5px);left:0;min-width:240px;max-height:320px;overflow-y:auto;background:var(--bg);
border:1px solid var(--bd);border-radius:14px;box-shadow:var(--sh);padding:5px;z-index:50;display:none;animation:fadeUp .15s var(--ease)}
.menu.open{display:block}
.mi{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:7px 11px;border-radius:8px;font-size:13px;transition:background .1s}
.mi:hover{background:var(--soft)}.mi .chk{margin-left:auto;font-weight:700;opacity:0}.mi.sel .chk{opacity:1}
.top .title{font-weight:500;color:var(--mut);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mode{margin-left:auto;display:none;font-size:11.5px;font-weight:600;background:var(--acs);border-radius:99px;padding:4px 11px;letter-spacing:.01em}
.mode.show{display:flex}
#stage{flex:1;display:flex;flex-direction:column;min-height:0}
#scroll{flex:1;overflow-y:auto;scroll-behavior:smooth}
#thread{max-width:var(--maxw);margin:0 auto;padding:22px 20px 10px;display:flex;flex-direction:column;gap:16px}
#heroWrap{flex:1;display:none;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:20px;animation:wIn .5s var(--ease)}
main.empty #scroll{display:none}main.empty #heroWrap{display:flex}
#heroGreet{font-size:clamp(20px,3vw,30px);font-weight:700;letter-spacing:-.025em;background:var(--ink);
-webkit-background-clip:text;background-clip:text;color:transparent;text-align:center}
.srow{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:600px}
.sg{padding:8px 16px;border:1px solid var(--bd);border-radius:99px;background:var(--bg);font-size:12.5px;color:var(--mut);
transition:all .15s ease;cursor:pointer}
.sg:hover{color:var(--tx);border-color:var(--bdh);box-shadow:0 1px 4px rgba(0,0,0,.04);transform:translateY(-1px)}
.sg:active{transform:translateY(0)}
#dock{padding:6px 20px 12px;flex-shrink:0}
main.empty #dock{position:static;padding:0 22px 12px}
.comp{max-width:var(--maxw);margin:0 auto;background:var(--bg);border:1px solid var(--bd);border-radius:16px;padding:10px 12px 8px;
box-shadow:var(--sh);transition:border-color .15s,box-shadow .15s}
.comp:focus-within{border-color:var(--ac);box-shadow:0 0 0 3px var(--acs)}
.comp textarea{width:100%;border:none;outline:none;resize:none;font-size:14.5px;max-height:180px;line-height:1.55;background:none;padding:2px 0}
.cbar{display:flex;align-items:center;gap:6px;margin-top:6px}
.chip{font-size:11px;color:var(--mut);border:1px solid var(--bd);border-radius:99px;padding:3px 10px;background:var(--softer);transition:border-color .12s}
.chip.dr{display:none;border-color:transparent;background:var(--acs);font-weight:600;color:var(--tx)}
.chip.dr.show{display:inline-flex;gap:5px}
#send{margin-left:auto;width:34px;height:34px;border-radius:10px;background:var(--ac);color:var(--bg);display:flex;
align-items:center;justify-content:center;transition:transform .12s,opacity .15s,background .15s;flex-shrink:0}
[data-theme=dark] #send{background:#e8eaed;color:#16181d}
#send:active{transform:scale(.9)}
#send:disabled{opacity:.2;cursor:not-allowed;transform:none}
#send.stop{background:var(--err);color:#fff}
#send:not(:disabled):hover{transform:scale(1.05)}
.fnote{max-width:var(--maxw);margin:6px auto 0;text-align:center;color:var(--dim);font-size:11px;line-height:1.4}
main.empty .fnote{display:none}
.msg{display:flex;gap:12px;animation:mIn .2s var(--ease);position:relative;animation-fill-mode:both}
.av{width:30px;height:30px;border-radius:10px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:13px;margin-top:1px;font-weight:600}
.msg.user .av{background:var(--acs);font-size:15px}.msg.agent .av{background:var(--ac);color:var(--bg)}
[data-theme=dark] .msg.agent .av{background:#e8eaed;color:#16181d}
.bub{flex:1;min-width:0}
.who{font-size:12px;font-weight:600;margin-bottom:3px;display:flex;align-items:center;gap:8px;color:var(--tx)}
.who .t{font-weight:400;color:var(--dim);font-size:11px}body.nots .who .t{display:none}
.cp{opacity:0;margin-left:auto;color:var(--dim);font-size:12px;padding:2px 7px;border-radius:6px;transition:opacity .12s,background .12s}
.msg:hover .cp{opacity:1}.cp:hover{background:var(--soft);color:var(--tx)}
.who .rg{opacity:0;color:var(--dim);font-size:12px;padding:2px 7px;border-radius:6px;transition:opacity .12s,background .12s}
.msg:hover .rg{opacity:1}.rg:hover{background:var(--soft);color:var(--tx)}
.ct{font-size:var(--msg);white-space:pre-wrap;word-wrap:break-word;line-height:1.6}
.ct p{margin:4px 0}
.ct ul,.ct ol{margin:4px 0 4px 1.3em}
.ct li{margin:2px 0}
.ct code{font-family:var(--mono);background:var(--soft);border:1px solid var(--bd);padding:1px 5px;border-radius:5px;font-size:.85em;color:var(--tx)}
.ct pre{background:var(--pre);border-radius:var(--rs);padding:14px 16px;overflow-x:auto;margin:8px 0;white-space:pre;border:1px solid rgba(255,255,255,.04)}
.ct pre code{background:none;border:none;padding:0;color:var(--pretx);font-size:12.5px;line-height:1.5}
.ct :first-child{margin-top:0}.ct :last-child{margin-bottom:0}
.cur{display:inline-block;width:7px;height:14px;background:var(--tx);vertical-align:-2px;border-radius:2px;animation:bl 1s steps(1) infinite;margin-left:1px}
.pw{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:12.5px;padding:6px 0}
.pu{width:12px;height:12px;border-radius:50%;background:var(--tx);animation:pu 1.1s ease-in-out infinite}
.tc{margin:6px 0 2px;border:1px solid var(--bd);border-radius:var(--rs);background:var(--bg);overflow:hidden;font-size:13px;
border-left:3px solid var(--bdh);transition:border-color .15s,box-shadow .1s}
.tc:hover{border-color:var(--bdh)}
.th{display:flex;align-items:center;gap:8px;padding:8px 12px;color:var(--mut);cursor:pointer;flex-wrap:wrap;user-select:none}
.th b{color:var(--tx);font-size:12.5px;font-weight:600}
.th .det{font-family:var(--mono);font-size:11.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;max-width:280px;white-space:nowrap}
.th .sp{width:11px;height:11px;border:2px solid var(--bdh);border-top-color:var(--tx);border-radius:50%;animation:rot .6s linear infinite;margin-left:auto;flex-shrink:0}
.th .ok2{margin-left:auto;font-size:12px;font-weight:700;color:var(--ok);flex-shrink:0}.th .ok2.bad{color:var(--err)}
.tconf{margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.tconf label{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--mut);font-weight:500;cursor:pointer}
.tconf input[type=checkbox]{accent-color:var(--tx);width:13px;height:13px}
.tconf button{font-size:11px;font-weight:600;padding:4px 11px;border-radius:99px;transition:all .1s}
.tconf .y{background:var(--ac);color:var(--bg)}[data-theme=dark] .tconf .y{background:#e8eaed;color:#16181d}
.tconf .y:active{transform:scale(.95)}
.tconf .n{border:1px solid var(--bd);color:var(--mut)}.tconf .n:hover{background:var(--soft)}
.tb{position:relative;display:none;border-top:1px solid var(--bd);padding:9px 12px;font-family:var(--mono);font-size:12px;color:var(--mut);
white-space:pre-wrap;max-height:180px;overflow-y:auto;background:var(--softer);line-height:1.45;animation:fadeUp .15s var(--ease)}
.tc.open .tb{display:block}
.tb .cpo{position:sticky;float:right;top:0;right:0;font-size:10.5px;font-weight:600;color:var(--mut);
background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:2px 7px;opacity:.85}
.tb .cpo:hover{opacity:1;background:var(--soft)}
#setOv{position:fixed;inset:0;z-index:130;background:rgba(0,0,0,.25);display:none;backdrop-filter:blur(2px)}
[data-theme=dark] #setOv{background:rgba(0,0,0,.45)}
#setOv.show{display:block}
.set{position:absolute;top:0;right:0;bottom:0;width:min(380px,100%);background:var(--bg);
box-shadow:-4px 0 30px rgba(0,0,0,.07);padding:20px 24px;overflow-y:auto;transform:translateX(100%);transition:transform .25s var(--ease)}
[data-theme=dark] .set{box-shadow:-4px 0 30px rgba(0,0,0,.3)}
#setOv.show .set{transform:none}
.set h3{font-size:17px;font-weight:700;letter-spacing:-.01em}.set .x{position:absolute;top:14px;right:14px;width:28px;height:28px;border-radius:8px;color:var(--mut);font-size:16px}
.set .x:hover{background:var(--soft)}
.sec{margin-top:20px}.sec>label{display:block;font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
.sr{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 0;font-size:13.5px}
.sr .s2{color:var(--dim);font-size:11px;display:block;margin-top:1px}
.sr input[type=range]{width:110px;accent-color:var(--tx);height:4px}
.sw{display:flex;gap:6px}
.swb{width:24px;height:24px;border-radius:50%;border:2px solid transparent;cursor:pointer;transition:border-color .12s,transform .12s}
.swb:hover{transform:scale(1.15)}
.swb.sel{border-color:var(--tx);box-shadow:inset 0 0 0 2px var(--bg);transform:scale(1.1)}
.seg{display:flex;border:1px solid var(--bd);border-radius:99px;overflow:hidden}
.seg button{padding:4px 12px;font-size:12px;font-weight:600;color:var(--mut);transition:all .12s;background:transparent}
.seg button.on{background:var(--ac);color:var(--bg)}[data-theme=dark] .seg button.on{background:#e8eaed;color:#16181d}
.tgl{width:34px;height:18px;border-radius:99px;background:var(--bdh);position:relative;transition:background .15s;flex-shrink:0;cursor:pointer}
.tgl::after{content:'';position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--bg);transition:left .15s;box-shadow:0 1px 2px rgba(0,0,0,.15)}
.tgl.on{background:var(--ok)}.tgl.on::after{left:18px}
.set textarea,.set input[type=text]{width:100%;padding:8px 11px;border:1px solid var(--bd);border-radius:var(--rsm);background:var(--softer);font-size:13px;transition:border-color .15s}
.set textarea:focus,.set input[type=text]:focus{border-color:var(--ac)}
.set textarea{resize:vertical;min-height:56px;font-family:var(--mono);font-size:12.5px}
.lk{font-size:13px;font-weight:600;padding:7px 0;color:var(--tx);cursor:pointer;display:inline-block;transition:opacity .12s;text-decoration:none}
.lk:hover{opacity:.7}
#connInfo{font-size:13.5px;font-weight:500}
#connInfo .s2{display:block;font-size:11px;font-weight:400;color:var(--dim);margin-top:1px}
.dev{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border:1px solid var(--bd);
border-radius:10px;margin-bottom:6px;font-size:12.5px}
.dev .di{font-family:var(--mono);font-size:12px}
.dev .da{display:flex;gap:6px}
.dev button{font-size:11px;font-weight:600;padding:4px 10px;border-radius:99px}
.dev .y{background:var(--ac);color:var(--bg)}[data-theme=dark] .dev .y{background:#e8eaed;color:#16181d}
.dev .n{border:1px solid var(--bd);color:var(--mut)}.dev .n:hover{background:var(--soft)}
.dev .you{color:var(--dim);font-size:11px}
@media(max-width:760px){
aside{display:none;position:fixed;top:0;left:0;bottom:0;z-index:110;box-shadow:4px 0 30px rgba(0,0,0,.12);
border-right:none;transform:translateX(-100%)}
[data-theme=dark] aside{box-shadow:4px 0 30px rgba(0,0,0,.4)}
aside.show{display:flex;transform:none}.ham{display:flex}.ham.show{display:none}
#scrlBtn{bottom:70px}#toast{bottom:14px}.top{padding:0 10px 0 44px}
}
.ham{display:none;position:fixed;top:10px;left:10px;z-index:120;width:32px;height:32px;border-radius:8px;
background:var(--bg);border:1px solid var(--bd);align-items:center;justify-content:center;font-size:16px;color:var(--tx);
box-shadow:0 1px 3px rgba(0,0,0,.05);transition:background .12s}
.ham:active{transform:scale(.92)}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:500;display:flex;flex-direction:column;
align-items:center;gap:6px;pointer-events:none}
.tst{background:var(--tx);color:var(--bg);padding:8px 18px;border-radius:10px;font-size:13px;font-weight:500;
box-shadow:0 4px 24px rgba(0,0,0,.2);animation:tIn .25s ease;pointer-events:auto}
[data-theme=dark] .tst{background:#e8eaed;color:#16181d}
.tst.access{background:var(--bg);color:var(--tx);border:1px solid var(--bd);box-shadow:var(--sh);padding:12px 16px;
display:flex;flex-direction:column;gap:8px;text-align:left;min-width:260px}
.tst.access b{font-size:13px}.tst.access .ip{font-family:var(--mono);color:var(--mut);font-size:12px}
.tst.access .row{display:flex;gap:6px;justify-content:flex-end}
.tst.access button{font-size:12px;font-weight:600;padding:5px 14px;border-radius:99px}
.tst.access .y{background:var(--ac);color:var(--bg)}[data-theme=dark] .tst.access .y{background:#e8eaed;color:#16181d}
.tst.access .n{border:1px solid var(--bd);color:var(--mut)}.tst.access .n:hover{background:var(--soft)}
#scrlBtn{position:absolute;bottom:90px;left:50%;transform:translateX(-50%);z-index:30;width:36px;height:36px;
border-radius:50%;background:var(--bg);border:1px solid var(--bd);box-shadow:0 2px 12px rgba(0,0,0,.08);font-size:15px;
display:none;align-items:center;justify-content:center;color:var(--tx);transition:transform .12s,opacity .15s,box-shadow .12s}
#scrlBtn:hover{box-shadow:0 4px 16px rgba(0,0,0,.12)}
[data-theme=dark] #scrlBtn{box-shadow:0 2px 12px rgba(0,0,0,.2)}
#scrlBtn.show{display:flex}#scrlBtn:active{transform:translateX(-50%) scale(.88)}
*{transition:background .18s ease,border-color .18s ease,color .18s ease,box-shadow .18s ease;scrollbar-width:thin}
.tc{transition:border-color .15s,box-shadow .1s}
.sg,.chip,.btn,.bg2,.mi,.nbtn,.ci,.gear,.swb,.tgl,.lk{transition:all .12s ease}
</style></head><body>
<div id="splash"><div class="logo"><div class="mark2">✦</div><h1 id="wt">Welcome, user.</h1><div class="tag">Local AI agent with real tools</div></div></div>
<div id="setupOv"><div class="panel">
 <div class="steps" id="stepDots"><div class="dot on"></div><div class="dot"></div></div>
 <div class="step active" data-step="1"><h2>Choose your platform</h2><p class="sub">Which local model runtime are you using?</p>
  <div class="f"><label>Platform</label><select id="platform"><option value="" disabled selected>Select…</option>
   <option value="ollama">🦙 Ollama</option><option value="lmstudio">🧪 LM Studio</option>
   <option value="opencode">⌘ OpenCode</option><option value="other">⚙️ Something else (OpenAI-compatible)</option></select></div>
  <div class="acts"><span></span><button class="btn" id="s2" disabled>Continue →</button></div></div>
 <div class="step" data-step="2"><h2>Set it up</h2><p class="sub">Reconfigure anytime in Settings.</p>
  <div class="f"><label>Endpoint URL</label><input type="text" id="endpoint" spellcheck="false" placeholder="http://localhost:11434"></div>
  <div class="tline" id="tline"></div>
  <div class="f"><label>Default model</label><select id="setupModel"><option value="">— click Test to list models —</option></select></div>
  <div class="acts"><button class="bg2" id="b1">← Back</button>
   <div style="display:flex;gap:8px"><button class="bg2" id="testBtn">Test Connection</button><button class="btn" id="finishBtn">Start →</button></div></div></div>
</div></div>
<div id="app"><button class="ham" id="hamBtn">☰</button><aside>
 <div class="shead"><div class="mark">✦</div><b>Aria</b></div>
 <div class="nav">
  <button class="nbtn" id="newChat"><span class="ni">✚</span>New Chat<kbd>⌘K</kbd></button>
  <button class="nbtn" id="deepBtn"><span class="ni">◎</span>Deep research</button></div>
 <div class="slabel">Chats</div><div id="chatList"></div>
 <div class="sfoot"><div class="sdot" id="sdot"></div><small id="stext">Connecting…</small>
  <button class="gear" id="gearBtn">⚙<span class="bdg" id="gearBdg"></span></button></div></aside>
<main id="main" class="empty">
 <div class="top">
  <div class="mdd"><button id="modelBtn"><span id="modelName">—</span><span class="car">▼</span></button>
   <div class="menu" id="modelMenu"></div></div>
  <span class="title" id="chatTitle"></span><span class="mode" id="modeChip">◎ Deep research</span></div>
 <div id="stage">
  <div id="heroWrap"><div id="heroGreet">How can I help?</div>
   <div class="srow" id="srow">
    <button class="sg" data-p="What OS and hardware am I running on?">🖥️ System info</button>
    <button class="sg" data-p="List the files here and summarize this project.">📖 Explore folder</button>
    <button class="sg" data-p="Search the web for the latest stable Python version.">🔎 Web search</button>
    <button class="sg" data-p="Create a simple landing page in landing/index.html for a chatbot product.">✏️ Build a page</button></div></div>
  <div id="scroll"><div id="thread"></div></div>
  <button id="scrlBtn">↓</button>
  <div id="dock"><div class="comp">
    <textarea id="input" rows="1" placeholder="Message Aria…"></textarea>
    <div class="cbar"><span class="chip" id="provChip">—</span>
     <span class="chip dr" id="drChip">◎ Deep research <button id="drOff">✕</button></span>
     <button id="send" disabled>➤</button></div></div>
   <div class="fnote">Real tools on your machine · <kbd style="background:var(--soft);border:1px solid var(--bd);border-radius:3px;padding:0 5px;font-family:var(--mono);font-size:10px">Esc</kbd> stop · <kbd style="background:var(--soft);border:1px solid var(--bd);border-radius:3px;padding:0 5px;font-family:var(--mono);font-size:10px">⌘K</kbd> new chat · <kbd style="background:var(--soft);border:1px solid var(--bd);border-radius:3px;padding:0 5px;font-family:var(--mono);font-size:10px">⌘B</kbd> sidebar · <kbd style="background:var(--soft);border:1px solid var(--bd);border-radius:3px;padding:0 5px;font-family:var(--mono);font-size:10px">?</kbd> help</div></div>
 </div></main></div>
<div id="setOv"><div class="set">
 <button class="x" id="xSet">✕</button><h3>Settings</h3>
 <div class="sec"><label>Appearance</label>
  <div class="sr"><span>Theme</span><div class="seg" id="themeSeg">
   <button data-v="light">Light</button><button data-v="dark">Dark</button><button data-v="auto">Auto</button></div></div>
  <div class="sr"><span>Accent</span><div class="sw" id="sw">
   <button class="swb" data-c="#16181d" style="background:#16181d"></button>
   <button class="swb" data-c="#3b6ef5" style="background:#3b6ef5"></button>
   <button class="swb" data-c="#7c3aed" style="background:#7c3aed"></button>
   <button class="swb" data-c="#16a36b" style="background:#16a36b"></button>
   <button class="swb" data-c="#e25822" style="background:#e25822"></button></div></div>
  <div class="sr"><span>Text size</span><input type="range" id="fsz" min="13" max="17" step="0.5"></div>
  <div class="sr"><span>Chat width</span><div class="seg" id="widthSeg">
   <button data-v="740px">Cozy</button><button data-v="940px">Wide</button></div></div>
  <div class="sr"><span>Timestamps</span><button class="tgl" id="tsTgl"></button></div>
  <div class="sr"><span>Auto-expand tool output</span><button class="tgl" id="expTgl"></button></div></div>
 <div class="sec"><label>Personalization</label>
  <div class="sr" style="display:block"><span style="display:block;margin-bottom:5px">Your name</span><input type="text" id="nm"></div>
  <div class="sr" style="display:block"><span style="display:block;margin-bottom:5px">System prompt</span><textarea id="sp"></textarea></div>
  <div class="sr"><span>Temperature<span class="s2" id="tv">0.7</span></span><input type="range" id="tp" min="0" max="2" step="0.1"></div></div>
 <div class="sec"><label>Safety</label>
  <div class="sr"><span>Confirm shell commands</span><button class="tgl" id="shT"></button></div>
  <div class="sr"><span>Confirm file writes</span><button class="tgl" id="wrT"></button></div></div>
 <div class="sec" id="accessSec"><label>Network access</label>
  <div class="sr" style="display:block"><span class="s2" style="margin-bottom:6px;display:block">Devices on your network can request access to this Aria instance. Approve or remove them below.</span>
   <div id="devList"></div></div></div>
 <div class="sec"><label>Data</label>
  <div class="sr"><span>Export this chat (JSON)</span><button class="lk" id="exportBtn">Download ↓</button></div></div>
 <div class="sec"><label>Connection</label>
  <div class="sr"><span id="connInfo">—</span></div>
  <button class="lk" id="reconfig">Change platform / endpoint →</button></div>
</div></div>
<script>
(()=>{
const $=s=>document.querySelector(s),$$=s=>document.querySelectorAll(s);
const K='aria.cfg',KC='aria.chats';
const URLS={ollama:'http://localhost:11434',lmstudio:'http://localhost:1234/v1',opencode:'http://localhost:4096/v1',other:'http://localhost:8080/v1'};
const PLAT={ollama:'🦙 Ollama',lmstudio:'🧪 LM Studio',opencode:'⌘ OpenCode',other:'⚙️ Custom'};
const IC={read_file:'📖',write_file:'✏️',edit_file:'🪄',shell:'🖥️',search:'🔎',browse:'🌐'};
const DEF={userName:'user',sysPrompt:'You are Aria (Autonomous Reasoning Intelligent Agent), a powerful local AI agent. Reason step by step, act autonomously with your tools, and be direct and concise.',temp:0.7,
accent:'#16181d',fontSize:14.5,theme:'auto',width:'740px',timestamps:true,expandTools:false,
confirmShell:true,confirmWrite:true,deepResearch:false};
let cfg=null;try{cfg=JSON.parse(localStorage.getItem(K))}catch(e){}
if(cfg)cfg={...DEF,...cfg};
const st={chats:[],cur:null,busy:false,abort:null,online:false,models:[],isHost:false};
try{st.chats=JSON.parse(localStorage.getItem(KC))||[]}catch(e){}
const save=()=>localStorage.setItem(K,JSON.stringify(cfg)),saveC=()=>localStorage.setItem(KC,JSON.stringify(st.chats));
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// Markdown: code blocks/inline code, bold, links, and simple lists.
const md=s=>{
 let o=esc(s);
 o=o.replace(/```(\w*)\n([\s\S]*?)(```|$)/g,(_,l,c)=>`<pre><code>${c}</code></pre>`);
 const blocks=o.split(/(<pre>[\s\S]*?<\/pre>)/g);
 o=blocks.map(b=>{
  if(b.startsWith('<pre>'))return b;
  b=b.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  b=b.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
  b=b.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  b=b.replace(/(^|\s)(https?:\/\/[^\s<]+)/g,(m,p,u)=>`${p}<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  // group consecutive list lines into <ul>/<ol>
  const lines=b.split('\n');let out=[],mode=null;
  for(const ln of lines){
   const um=ln.match(/^\s*[-*]\s+(.*)/),om=ln.match(/^\s*\d+[.)]\s+(.*)/);
   if(um){if(mode!=='ul'){if(mode)out.push(`</${mode}>`);out.push('<ul>');mode='ul'}out.push(`<li>${um[1]}</li>`)}
   else if(om){if(mode!=='ol'){if(mode)out.push(`</${mode}>`);out.push('<ol>');mode='ol'}out.push(`<li>${om[1]}</li>`)}
   else{if(mode){out.push(`</${mode}>`);mode=null}out.push(ln)}
  }
  if(mode)out.push(`</${mode}>`);
  return out.join('\n')
 }).join('');
 return o};
function theme(){
 const dark=cfg.theme==='dark'||(cfg.theme==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);
 document.documentElement.dataset.theme=dark?'dark':'light';
 const r=document.documentElement.style,n=parseInt(cfg.accent.slice(1),16);
 r.setProperty('--ac',cfg.accent);r.setProperty('--acs',`rgba(${n>>16&255},${n>>8&255},${n&255},.1)`);
 r.setProperty('--msg',cfg.fontSize+'px');r.setProperty('--maxw',cfg.width);
 document.body.classList.toggle('nots',!cfg.timestamps)}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>cfg&&theme());
$('#wt').textContent=`Welcome, ${cfg?.userName||'user'}.`;if(cfg)theme();
setTimeout(()=>{$('#splash').classList.add('fade');setTimeout(()=>cfg?enter():$('#setupOv').classList.add('show'),200)},1000);
const goStep=n=>{$$('.step').forEach(s=>s.classList.toggle('active',s.dataset.step==n));
 $$('#stepDots .dot').forEach((d,i)=>d.classList.toggle('on',i+1==n))};
$('#platform').onchange=e=>$('#s2').disabled=!e.target.value;
$('#s2').onclick=()=>{$('#endpoint').value=cfg?.baseUrl||URLS[$('#platform').value];$('#tline').innerHTML='';goStep(2)};
$('#b1').onclick=()=>goStep(1);
async function getModels(p,u){try{const r=await fetch(`/api/models?platform=${encodeURIComponent(p)}&url=${encodeURIComponent(u)}`);
 const j=await r.json();return j.ok?j.models:null}catch(e){return null}}
$('#testBtn').onclick=async()=>{const l=$('#tline');
 l.innerHTML='<span class="d" style="background:var(--warn)"></span><span>Testing connection…</span>';
 const m=await getModels($('#platform').value,$('#endpoint').value.trim());
 if(m){l.innerHTML=`<span class="d" style="background:var(--ok)"></span><span style="color:var(--ok)">Connected — ${m.length} model${m.length!==1?'s':''}</span>`;
  $('#setupModel').innerHTML=m.map(x=>`<option>${x}</option>`).join('');$('#setupModel').value=m[0]}
 else{const r=await fetch(`/api/models?platform=${encodeURIComponent($('#platform').value)}&url=${encodeURIComponent($('#endpoint').value.trim())}`);
  const j=await r.json();
  l.innerHTML=`<span class="d" style="background:var(--err)"></span><span style="color:var(--err)">${j.error||'Unreachable — check your runtime'}</span>`}};
$('#finishBtn').onclick=()=>{cfg={...DEF,...(cfg||{}),platform:$('#platform').value,
 baseUrl:$('#endpoint').value.trim().replace(/\/$/,''),model:$('#setupModel').value||''};
 save();$('#setupOv').classList.remove('show');enter()};
async function connect(){const m=await getModels(cfg.platform,cfg.baseUrl);
 if(m&&m.length){st.online=true;st.models=m;if(!m.includes(cfg.model))cfg.model=m[0];
  if(!cfg.model&&st.models[0])cfg.model=st.models[0];
  $('#sdot').className='sdot ok';$('#stext').textContent='Connected · '+cfg.baseUrl.replace(/^https?:\/\//,'')}
 else{st.online=false;st.models=[];$('#sdot').className='sdot err';$('#stext').textContent='Offline';
  const r=await fetch(`/api/models?platform=${encodeURIComponent(cfg.platform)}&url=${encodeURIComponent(cfg.baseUrl)}`);
  const j=await r.json();$('#stext').textContent=j.error||'Offline — start '+(PLAT[cfg.platform]||'runtime')}
 mm();save()}
function mm(){const rm=$('#modelName');if(rm)rm.textContent=cfg.model||'—';
 const mu=$('#modelMenu');if(!mu)return;
 let html=st.models.map(m=>`<button class="mi ${m===cfg.model?'sel':''}" data-m="${m}">${m}<span class="chk">✓</span></button>`).join('');
 if(st.online)html+=`<div style="border-top:1px solid var(--bd);margin:4px 0;padding:4px 0">
  <button class="mi" id="refreshModels" style="color:var(--mut);font-size:12.5px">↻ Refresh models</button></div>`;
 mu.innerHTML=html||'<div style="padding:10px;color:var(--dim);font-size:12.5px">No models found</div>';
 const rf=document.getElementById('refreshModels');
 if(rf)rf.onclick=async e=>{e.stopPropagation();rf.textContent='↻ Refreshing…';rf.disabled=true;
  const m=await getModels(cfg.platform,cfg.baseUrl);
  if(m&&m.length){st.models=m;if(!m.includes(cfg.model))cfg.model=m[0];mm();save();toast('Models refreshed')}
  else{rf.textContent='↻ Refresh models';rf.disabled=false;toast('Could not refresh models','err')}}}
$('#modelBtn').onclick=e=>{e.stopPropagation();$('#modelMenu').classList.toggle('open')};
$('#modelMenu').onclick=e=>{const i=e.target.closest('.mi');if(!i)return;
 cfg.model=i.dataset.m;save();mm();$('#modelMenu').classList.remove('open')};
document.addEventListener('click',()=>$('#modelMenu').classList.remove('open'));
function enter(){theme();$('#app').classList.add('active');
 $('#provChip').textContent=PLAT[cfg.platform]||cfg.platform;
 $('#heroGreet').textContent=`How can I help, ${cfg.userName}?`;
 if(!st.cur){st.cur=st.chats[0]||null;if(!st.cur)nc(true)}
 sUI();rc();rt();connect();initAccess();setTimeout(()=>$('#input').focus(),40)}
$('#sdot').onclick=()=>{if(!st.online){$('#stext').textContent='Reconnecting…';$('#sdot').className='sdot';
 connect()}};

// ── LAN access control (host side) ─────────────────────────────────
function initAccess(){
 checkHostStatus();
 setInterval(pollAccess,3000);pollAccess();
}
async function checkHostStatus(){
 try{const r=await fetch('/api/access/status');const j=await r.json();st.isHost=!!j.isHost;
  $('#accessSec').style.display=st.isHost?'':'none'}catch(e){}
}
const shownAccess=new Set();
async function pollAccess(){
 if(!st.isHost)return;
 try{
  const r=await fetch('/api/access/pending');const j=await r.json();
  $('#gearBdg').classList.toggle('show',(j.pending||[]).length>0);
  renderDevList(j.pending||[],j.trusted||[]);
  for(const p of (j.pending||[])){
   if(shownAccess.has(p.ip))continue;shownAccess.add(p.ip);
   accessToast(p.ip,p.ua)}
 }catch(e){}
}
function renderDevList(pending,trusted){
 const el=$('#devList');if(!el)return;
 let html='';
 for(const p of pending){
  html+=`<div class="dev"><span class="di">${esc(p.ip)} <span class="you">new request</span></span>
  <div class="da"><button class="n" data-ip="${esc(p.ip)}" data-a="0">Deny</button>
  <button class="y" data-ip="${esc(p.ip)}" data-a="1">Allow</button></div></div>`}
 for(const ip of trusted){
  html+=`<div class="dev"><span class="di">${esc(ip)} ${ip===location.hostname||ip==='127.0.0.1'?'<span class="you">(this device)</span>':''}</span>
  <div class="da">${ip!=='127.0.0.1'?`<button class="n" data-ip="${esc(ip)}" data-a="0">Remove</button>`:''}</div></div>`}
 el.innerHTML=html||'<div class="s2">No other devices have connected.</div>';
 el.onclick=e=>{const b=e.target.closest('button');if(!b)return;respondAccess(b.dataset.ip,b.dataset.a==='1')}
}
async function respondAccess(ip,approve){
 await fetch('/api/access/respond',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({ip,approve})});
 shownAccess.delete(ip);pollAccess();
 document.querySelectorAll(`.tst[data-ip="${ip}"]`).forEach(t=>t.remove())}
function accessToast(ip,ua){
 const d=document.createElement('div');d.className='tst access';d.dataset.ip=ip;
 d.innerHTML=`<b>New device wants to connect</b><span class="ip">${esc(ip)}</span>
  <span class="s2" style="font-size:11px;color:var(--dim)">${esc((ua||'').slice(0,60))}</span>
  <div class="row"><button class="n" data-a="0">Deny</button><button class="y" data-a="1">Allow</button></div>`;
 d.querySelector('.row').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  respondAccess(ip,b.dataset.a==='1');d.remove()};
 $('#toast').appendChild(d)}

function nc(s){const c={id:Date.now(),title:'New chat',msgs:[]};st.chats.unshift(c);st.cur=c;saveC();
 if(!s){rc();rt();$('#input').focus()}}
$('#newChat').onclick=()=>nc();
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();nc()}
  if((e.metaKey||e.ctrlKey)&&e.key==='b'){e.preventDefault();$('#hamBtn').click()}
  if(e.key==='Escape'){if($('#setOv').classList.contains('show'))$('#xSet').click();
   else if(st.busy)stopGen();
   if($('aside').classList.contains('show'))$('#hamBtn').click()}
  if(e.key==='?'&&!e.metaKey&&!e.ctrlKey&&!e.altKey){
   if(document.activeElement===$('#input'))return;
   e.preventDefault();toast('⌘K New chat  ·  ⌘B Sidebar  ·  Esc Stop/settings  ·  ⚙ Settings')}});
function rc(){$('#chatList').innerHTML=st.chats.map(c=>
 `<div class="ci ${c===st.cur?'on':''}" data-id="${c.id}"><span>${esc(c.title)}</span><button class="del" data-del="${c.id}">✕</button></div>`).join('')}
$('#chatList').onclick=e=>{const d=e.target.closest('[data-del]');
 if(d){st.chats=st.chats.filter(c=>c.id!=d.dataset.del);
  if(st.cur?.id==d.dataset.del)st.cur=st.chats[0]||null;if(!st.cur)nc(true);saveC();rc();rt();return}
 const i=e.target.closest('.ci');if(!i)return;st.cur=st.chats.find(c=>c.id==i.dataset.id);rc();rt()};
$('#chatList').ondblclick=e=>{const i=e.target.closest('.ci');if(!i)return;
 const c=st.chats.find(x=>x.id==i.dataset.id);const t=prompt('Rename chat:',c.title);
 if(t){c.title=t.slice(0,60);saveC();rc();if(c===st.cur)$('#chatTitle').textContent=c.title}};
$('#deepBtn').onclick=()=>{cfg.deepResearch=!cfg.deepResearch;save();dr()};
$('#drOff').onclick=e=>{e.stopPropagation();cfg.deepResearch=false;save();dr()};
function dr(){$('#deepBtn').classList.toggle('tog',cfg.deepResearch);
 $('#modeChip').classList.toggle('show',cfg.deepResearch);$('#drChip').classList.toggle('show',cfg.deepResearch)}
function rt(){const has=st.cur.msgs.length>0;$('#main').classList.toggle('empty',!has);
 $('#chatTitle').textContent=has?st.cur.title:'';const t=$('#thread');t.innerHTML='';
 st.cur.msgs.forEach((m,i)=>t.appendChild(me(m,i)));sd(true)}
function me(m,idx){const d=document.createElement('div');d.className=`msg ${m.role==='user'?'user':'agent'}`;
 const isLastAgent=m.role!=='user'&&idx===st.cur.msgs.length-1;
 d.innerHTML=`<div class="av">${m.role==='user'?'🧑':'✦'}</div><div class="bub">
 <div class="who">${m.role==='user'?esc(cfg.userName):'Aria'}<span class="t">${m.time||''}</span>
 ${isLastAgent?'<button class="rg" title="Regenerate">↻</button>':''}
 <button class="cp" title="Copy">⧉</button></div><div class="tools"></div><div class="ct">${md(m.text||'')}</div></div>`;
 d.querySelector('.cp').onclick=()=>{navigator.clipboard?.writeText(m.text||'');
 if(navigator.clipboard)toast('Copied!')};
 const rg=d.querySelector('.rg');if(rg)rg.onclick=()=>regenerate();
 return d}
const sd=i=>{const s=$('#scroll');s.scrollTo({top:s.scrollHeight,behavior:i?'auto':'smooth'});
 s.dispatchEvent(new Event('scroll'))};
const now=()=>new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
function drop(){const m=$('#main');if(!m.classList.contains('empty'))return;
 const d=$('#dock'),a=d.getBoundingClientRect();m.classList.remove('empty');
 const b=d.getBoundingClientRect();
 d.animate([{transform:`translateY(${a.top-b.top}px)`},{transform:'none'}],{duration:280,easing:'cubic-bezier(.2,.8,.2,1)'})}
const inp=$('#input'),sb=$('#send');
inp.oninput=()=>{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,200)+'px';
 sb.disabled=!inp.value.trim()&&!st.busy};
$('#scroll').onscroll=()=>{const s=$('#scroll'),b=$('#scrlBtn');
 b.classList.toggle('show',s.scrollTop<s.scrollHeight-s.clientHeight-80)};
$('#scrlBtn').onclick=()=>{$('#scroll').scrollTo({top:$('#scroll').scrollHeight,behavior:'smooth'})};
$('#hamBtn').onclick=()=>{$('aside').classList.toggle('show');$('#hamBtn').textContent=$('aside').classList.contains('show')?'✕':'☰'};
inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(inp.value.trim())send()}};
sb.onclick=()=>{if(st.busy){stopGen();return}send()};
$('#srow').onclick=e=>{const s=e.target.closest('.sg');if(!s)return;
 inp.value=s.dataset.p;inp.dispatchEvent(new Event('input'));send()};
function stopGen(){st.abort?.abort();fetch('/api/stop',{method:'POST'}).catch(()=>{})}
function regenerate(){
 const c=st.cur;
 // drop trailing assistant message(s) until we hit a user message
 while(c.msgs.length&&c.msgs[c.msgs.length-1].role!=='user')c.msgs.pop();
 rt();runAgent()}
async function send(){
 const text=inp.value.trim();if(!text||st.busy)return;
 inp.value='';inp.style.height='auto';
 const c=st.cur;c.msgs.push({role:'user',text,time:now()});
 if(c.title==='New chat'){c.title=text.slice(0,42)+(text.length>42?'…':'');rc()}
 drop();$('#thread').appendChild(me(c.msgs[c.msgs.length-1],c.msgs.length-1));$('#chatTitle').textContent=c.title;
 sb.disabled=!inp.value.trim();
 await runAgent()}
async function runAgent(){
 const c=st.cur;
 const am={role:'assistant',text:'',time:now()};c.msgs.push(am);
 const el=me(am,c.msgs.length-1);$('#thread').appendChild(el);
 const ce=el.querySelector('.ct'),te=el.querySelector('.tools');
 ce.innerHTML='<div class="pw"><div class="pu"></div>'+(cfg.deepResearch?'Researching…':'Thinking…')+'</div>';sd();
 st.busy=true;sb.classList.add('stop');sb.textContent='■';sb.disabled=false;
 st.abort=new AbortController();
 const cards={};
 const hist=c.msgs.filter(m=>m.text&&m!==am).map(m=>({role:m.role==='user'?'user':'assistant',content:m.text}));
 try{
  const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({cfg,messages:hist}),signal:st.abort.signal});
  if(r.status===403){const j=await r.json();throw new Error(j.error||'Access denied')}
  const rd=r.body.getReader(),dc=new TextDecoder();let buf='';
  while(true){const{done,value}=await rd.read();if(done)break;
   buf+=dc.decode(value,{stream:true});const ps=buf.split('\n\n');buf=ps.pop();
   for(const p of ps){const l=p.replace(/^data:\s*/,'').trim();if(!l)continue;
    let ev;try{ev=JSON.parse(l)}catch(e){continue}
    if(ev.type==='token'){am.text+=ev.text;ce.innerHTML=md(am.text)+'<span class="cur"></span>';sd(true)}
    else if(ev.type==='always_allow'){
     if(ev.flag==='confirmShell')cfg.confirmShell=false;
     if(ev.flag==='confirmWrite')cfg.confirmWrite=false;
     save();sUI();toast('Always-allow saved for this tool type')}
    else if(ev.type==='tool'){
     if(!am.text)ce.innerHTML='';
     let cd=cards[ev.id];
     if(!cd){cd=document.createElement('div');cd.className='tc'+(cfg.expandTools?' open':'');cards[ev.id]=cd;te.appendChild(cd)}
     cd.dataset.det=ev.detail||'';
     const h=`<span>${IC[ev.tool]||'🔧'}</span><b>${ev.tool}</b><span class="det">${esc(ev.detail||'')}</span>`;
     if(ev.status==='gen')
      cd.innerHTML=`<div class="th">${h}<span style="margin-left:auto;font-size:11px;color:var(--dim)">generating</span><div class="sp" style="margin-left:8px"></div></div>`;
     else if(ev.status==='confirm'){
      const flagLabel=ev.tool==='shell'?'shell commands':'file writes';
      cd.innerHTML=`<div class="th">${h}<div class="tconf">
       <label><input type="checkbox" class="always"> always allow ${flagLabel}</label>
       <button class="n" data-a="0">Deny</button><button class="y" data-a="1">Approve</button></div></div>`;
      cd.querySelector('.tconf').onclick=async e2=>{const b=e2.target.closest('button');if(!b)return;
       const always=cd.querySelector('.always')?.checked||false;
       cd.querySelector('.tconf').outerHTML=b.dataset.a==='1'?'<div class="sp"></div>':'<span class="ok2 bad">denied</span>';
       await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:ev.id,approved:b.dataset.a==='1',always})})}}
     else cd.innerHTML=`<div class="th">${h}<div class="sp"></div></div>`;
     sd(true)}
    else if(ev.type==='tool_progress'){const cd=cards[ev.id];if(!cd)continue;
     const d=cd.querySelector('.det');
     if(d)d.textContent=(cd.dataset.det||'')+' · '+(ev.size/1024).toFixed(1)+' KB';sd(true)}
    else if(ev.type==='tool_end'){const cd=cards[ev.id];if(!cd)continue;
     const h=cd.querySelector('.th');h.querySelector('.sp')?.remove();h.querySelector('.tconf')?.remove();
     if(!h.querySelector('.ok2'))h.insertAdjacentHTML('beforeend',`<span class="ok2 ${ev.ok?'':'bad'}">${ev.ok?'✓':'✕'}</span>`);
     if(ev.output){const b=document.createElement('div');b.className='tb';
      b.innerHTML=`<button class="cpo">copy</button>`;b.appendChild(document.createTextNode(ev.output));
      b.querySelector('.cpo').onclick=ev2=>{ev2.stopPropagation();navigator.clipboard?.writeText(ev.output);toast('Output copied')};
      cd.appendChild(b);
      h.onclick=()=>cd.classList.toggle('open')}
     sd(true)}
    else if(ev.type==='error')am.text+=(am.text?'\n\n':'')+'⚠️ '+ev.text}}
 }catch(e){if(e.name!=='AbortError'){am.text+=(am.text?'\n\n':'')+'⚠️ '+
  (e.message&&e.message!=='Failed to fetch'?e.message:
   (st.online?'Connection error':'Model offline — start '+(PLAT[cfg.platform]||'your runtime')+' and reconnect via ⚙.'));
  toast('Error: '+(e.message||(st.online?'Connection error':'Model offline')),'err')}}
 ce.innerHTML=md(am.text)||'<span style="color:var(--dim)">［stopped］</span>';
 st.busy=false;sb.classList.remove('stop');sb.textContent='➤';sb.disabled=!inp.value.trim();
 saveC();sd();
 // re-render to attach the regenerate button to the now-final message
 rt()}
function sUI(){
 $$('#themeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.v===cfg.theme));
 $$('#sw .swb').forEach(s=>s.classList.toggle('sel',s.dataset.c===cfg.accent));
 $('#fsz').value=cfg.fontSize;
 $$('#widthSeg button').forEach(b=>b.classList.toggle('on',b.dataset.v===cfg.width));
 $('#tsTgl').classList.toggle('on',cfg.timestamps);$('#expTgl').classList.toggle('on',cfg.expandTools);
 $('#nm').value=cfg.userName;$('#sp').value=cfg.sysPrompt;
 $('#tp').value=cfg.temp;$('#tv').textContent=(+cfg.temp).toFixed(1);
 $('#shT').classList.toggle('on',cfg.confirmShell);$('#wrT').classList.toggle('on',cfg.confirmWrite);
 $('#connInfo').innerHTML=`${PLAT[cfg.platform]}<span class="s2">${esc(cfg.baseUrl)}</span>`;dr()}
$('#gearBtn').onclick=()=>{sUI();pollAccess();$('#setOv').classList.add('show')};
$('#xSet').onclick=()=>$('#setOv').classList.remove('show');
$('#setOv').onclick=e=>{if(e.target===e.currentTarget)e.currentTarget.classList.remove('show')};
$('#themeSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;cfg.theme=b.dataset.v;save();theme();sUI()};
$('#sw').onclick=e=>{const s=e.target.closest('.swb');if(!s)return;cfg.accent=s.dataset.c;save();theme();sUI()};
$('#fsz').oninput=e=>{cfg.fontSize=+e.target.value;save();theme()};
$('#widthSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;cfg.width=b.dataset.v;save();theme();sUI()};
$('#tsTgl').onclick=()=>{cfg.timestamps=!cfg.timestamps;save();theme();sUI()};
$('#expTgl').onclick=()=>{cfg.expandTools=!cfg.expandTools;save();sUI()};
$('#shT').onclick=()=>{cfg.confirmShell=!cfg.confirmShell;save();sUI()};
$('#wrT').onclick=()=>{cfg.confirmWrite=!cfg.confirmWrite;save();sUI()};
$('#nm').onchange=e=>{cfg.userName=e.target.value.trim()||'user';save();
 $('#heroGreet').textContent=`How can I help, ${cfg.userName}?`};
$('#sp').onchange=e=>{cfg.sysPrompt=e.target.value;save()};
$('#tp').oninput=e=>{cfg.temp=+e.target.value;$('#tv').textContent=cfg.temp.toFixed(1);save()};
$('#exportBtn').onclick=()=>{
 const blob=new Blob([JSON.stringify(st.cur,null,2)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);
 a.download=(st.cur.title||'chat').replace(/[^\w-]+/g,'_')+'.json';a.click();
 setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
$('#reconfig').onclick=()=>{$('#setOv').classList.remove('show');$('#app').classList.remove('active');
 $('#platform').value=cfg.platform;$('#platform').dispatchEvent(new Event('change'));
 $('#setupOv').classList.add('show');goStep(1)};
function toast(t,ty){const d=document.createElement('div');d.className='tst';d.textContent=t;
 const c=$('#toast');c.appendChild(d);setTimeout(()=>{d.style.opacity='0';d.style.transform='translateY(10px)';
 setTimeout(()=>d.remove(),250)},2200)}
})();
</script><div id="toast"></div></body></html>"""

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✦ Aria → http://127.0.0.1:{PORT}")
    print(f"  Workspace: {ROOT}")
    print(f"  LAN: other devices on your network can connect to http://<this-machine-ip>:{PORT}")
    print(f"        (the first device to load the page becomes the trusted host;")
    print(f"         other devices need to be approved from the host's Settings)")
    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
