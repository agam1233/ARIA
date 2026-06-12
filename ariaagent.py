#!/usr/bin/env python3
"""
Aria — Autonomous Reasoning Intelligent Agent. Standalone local AI agent with a web UI.
Run:  python aria.py        (stdlib only, no installs)
Opens http://127.0.0.1:8400 — tools operate in the launch directory.
"""
import json, os, re, subprocess, threading, uuid, html as htmlmod
import urllib.request, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8400
ROOT = os.path.realpath(os.getcwd())
PENDING = {}
MAX_TURNS = 10
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"}

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
- NEVER describe a command in prose instead of calling it. NEVER invent tool results.
- One tool call per reply. When done with tools, write a normal final answer (no JSON, no blocks).
""".strip()

# ── tools ──────────────────────────────────────────────────────────
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
    try:
        r = subprocess.run(a["command"], shell=True, cwd=ROOT, timeout=60,
                           capture_output=True, text=True)
        out = (r.stdout + (("\n" + r.stderr) if r.stderr else "")).strip() or "(no output)"
        return f"exit {r.returncode}\n{out[:6000]}"
    except subprocess.TimeoutExpired:
        return "ERROR: timed out (60s)"

def _strip_tags(s):
    return htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()

def _ddg_url(href):
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
    return urllib.parse.unquote(q[0]) if q else href

def t_search(a):
    query, errs = a["query"], []
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request("https://lite.duckduckgo.com/lite/", data=data,
                                     headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
        page = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
        hits = re.findall(r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', page, re.S) \
            or re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        out, seen = [], set()
        for href, title in hits:
            url = _ddg_url(href)
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append(f"- {_strip_tags(title)}\n  {url}")
            if len(out) >= 6:
                break
        if out:
            return "\n".join(out)
        errs.append("lite: 0 results")
    except Exception as e:
        errs.append(f"lite: {e}")
    try:
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query), headers=UA)
        page = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
        out, seen = [], set()
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
            url = _ddg_url(m.group(1))
            if url in seen:
                continue
            seen.add(url)
            out.append(f"- {_strip_tags(m.group(2))}\n  {url}")
            if len(out) >= 6:
                break
        if out:
            return "\n".join(out)
        errs.append("html: 0 results")
    except Exception as e:
        errs.append(f"html: {e}")
    return "ERROR: search unavailable (" + "; ".join(errs) + "). Tell the user search failed; do not invent results."

def t_browse(a):
    req = urllib.request.Request(a["url"], headers=UA)
    page = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
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
            # candidate hold positions for anything that may be a tool call
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

            # live "generating" card + progress counter
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

            # complete call?
            wm = WRITE_RE.search(full)
            if wm:
                e = full.find("<<end>>", wm.end())
                if e != -1:
                    call = ("write", wm, full[wm.end():e].rstrip("\n")); break
            if '"tool"' in full:
                obj, s, _ = extract_tool(full)
                if obj:
                    call = ("json", obj, s); break

        # stream ended with an unterminated write block: accept the rest as content
        if call is None:
            wm = WRITE_RE.search(full)
            if wm and len(full) > wm.end():
                call = ("write", wm, full[wm.end():].rstrip("\n"))

        if call is None:
            if '"tool"' in full or "<<tool" in full or "<<write:" in full:   # malformed
                if gen_id:
                    emit({"type": "tool_end", "id": gen_id, "ok": False, "output": "malformed tool call"})
                bad += 1
                if bad > 2:
                    emit({"type": "token", "text": "\n\n⚠️ The model kept producing malformed tool calls. Try a larger / more instruction-tuned model."})
                    emit({"type": "done"}); return
                messages += [{"role": "assistant", "content": full},
                             {"role": "user", "content": 'Your tool call was malformed. Use the <<write:path>> block for files, or ONLY valid JSON like {"tool":"shell","args":{"command":"ls"}}.'}]
                continue
            if len(full) > emitted:                                          # plain answer
                emit({"type": "token", "text": full[emitted:]})
            emit({"type": "done"}); return

        # resolve the call
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
                ev = threading.Event(); PENDING[cid] = {"ev": ev, "approved": None}
                ev.wait(300)
                if not PENDING.pop(cid, {}).get("approved"):
                    emit({"type": "tool_end", "id": cid, "ok": False, "output": "Denied by user"})
                    messages += [{"role": "assistant", "content": full},
                                 {"role": "user", "content": f"Tool result for {name}:\nUser DENIED this action. Do not retry it. Adjust or explain."}]
                    continue
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

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/":
            b = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p.path == "/api/models":
            q = urllib.parse.parse_qs(p.query)
            plat, url = q.get("platform", [""])[0], q.get("url", [""])[0].rstrip("/")
            try:
                if plat == "ollama":
                    r = json.loads(urllib.request.urlopen(url + "/api/tags", timeout=4).read())
                    models = [m["name"] for m in r.get("models", [])]
                else:
                    if not url.endswith("/v1"): url += "/v1"
                    r = json.loads(urllib.request.urlopen(url + "/models", timeout=4).read())
                    models = [m["id"] for m in r.get("data", [])]
                self._json({"ok": True, "models": models, "cwd": ROOT})
            except Exception as e:
                self._json({"ok": False, "error": str(e), "cwd": ROOT})
        else:
            self.send_error(404)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if self.path == "/api/approve":
            e = PENDING.get(data.get("id"))
            if e: e["approved"] = bool(data.get("approved")); e["ev"].set()
            self._json({"ok": True})
        elif self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache"); self.end_headers()
            def emit(o):
                self.wfile.write(f"data: {json.dumps(o)}\n\n".encode()); self.wfile.flush()
            try:
                run_agent(data["cfg"], data["messages"], emit)
            except (BrokenPipeError, ConnectionError):
                pass
            except Exception as e:
                try: emit({"type": "error", "text": str(e)}); emit({"type": "done"})
                except Exception: pass
        else:
            self.send_error(404)

# ── UI ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aria</title><style>
:root{--bg:#fff;--soft:#f6f6f8;--softer:#fafafb;--bd:#e6e7eb;--bdh:#d2d5dc;--tx:#16181d;--mut:#6b7280;--dim:#9aa0ad;
--ac:#16181d;--acs:rgba(22,24,29,.08);--ok:#16a36b;--err:#d92d3f;--warn:#c98a1b;--rs:10px;--msg:14.5px;--maxw:740px;
--ink:linear-gradient(120deg,#15171c,#4b4f59 45%,#8a8f9b);--pre:#16181d;--pretx:#e6e8ee;
--mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;--sans:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,sans-serif;
--sh:0 1px 2px rgba(20,22,28,.05),0 8px 28px rgba(20,22,28,.08);--ease:cubic-bezier(.2,.8,.2,1)}
[data-theme=dark]{--bg:#101114;--soft:#16171b;--softer:#1a1b20;--bd:#26282e;--bdh:#34373f;--tx:#ececf0;--mut:#9aa1ad;
--dim:#646c7a;--acs:rgba(236,236,240,.08);--ink:linear-gradient(120deg,#f2f3f6,#b9bec9 45%,#7e8693);--pre:#0a0b0d;
--sh:0 1px 2px rgba(0,0,0,.3),0 8px 28px rgba(0,0,0,.35)}
[data-theme=dark] .splashbg{background:linear-gradient(160deg,#16171b,#101114)!important}
*{margin:0;padding:0;box-sizing:border-box}html,body{height:100%}
body{font-family:var(--sans);background:var(--bg);color:var(--tx);font-size:14px;line-height:1.55;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
input,select,textarea{font:inherit;color:inherit}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:var(--bdh);border-radius:8px;border:2px solid var(--bg)}
#splash{position:fixed;inset:0;z-index:300;display:flex;align-items:center;justify-content:center;
background:linear-gradient(160deg,#fff,#f4f5f7 45%,#eceef2);transition:opacity .4s}
#splash.fade{opacity:0;pointer-events:none}
#splash h1{font-size:clamp(32px,6vw,54px);font-weight:700;letter-spacing:-.03em;background:var(--ink);
-webkit-background-clip:text;background-clip:text;color:transparent;opacity:0;animation:wIn .6s var(--ease) .05s forwards}
@keyframes wIn{from{opacity:0;transform:translateY(12px);filter:blur(4px)}to{opacity:1;transform:none;filter:none}}
#setupOv{position:fixed;inset:0;z-index:250;display:none;align-items:center;justify-content:center;background:var(--soft);padding:24px}
#setupOv.show{display:flex}
.panel{width:min(480px,100%);background:var(--bg);border:1px solid var(--bd);border-radius:18px;padding:32px 34px 26px;box-shadow:var(--sh);animation:wIn .35s var(--ease)}
.step{display:none}.step.active{display:block}
.panel h2{font-size:20px;font-weight:700;background:var(--ink);-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:4px}
.panel .sub{color:var(--mut);font-size:13.5px;margin-bottom:20px}
.f{margin-bottom:13px}.f label{display:block;font-size:12px;font-weight:600;color:var(--mut);margin-bottom:5px}
.f select,.f input{width:100%;padding:10px 12px;border:1px solid var(--bd);border-radius:var(--rs);background:var(--softer);outline:none}
.f input{font-family:var(--mono);font-size:13px}
.tline{display:flex;align-items:center;gap:8px;font-size:12.5px;min-height:18px;margin:-3px 0 9px}
.tline .d{width:8px;height:8px;border-radius:50%}
.acts{display:flex;justify-content:space-between;align-items:center;margin-top:18px}
.btn{padding:10px 22px;border-radius:11px;background:var(--ac);color:var(--bg);font-weight:600}
[data-theme=dark] .btn{background:#ececf0;color:#16181d}
.btn:disabled{opacity:.35;cursor:not-allowed}
.bg2{color:var(--mut);padding:10px 12px;border-radius:11px}.bg2:hover{background:var(--soft);color:var(--tx)}
#app{display:none;height:100%}#app.active{display:flex}
aside{width:256px;flex-shrink:0;background:var(--soft);border-right:1px solid var(--bd);display:flex;flex-direction:column}
.shead{display:flex;align-items:center;gap:10px;padding:15px 16px 10px}
.mark{width:28px;height:28px;border-radius:8px;background:var(--ac);color:var(--bg);display:flex;align-items:center;justify-content:center;font-size:13px}
[data-theme=dark] .mark{background:#ececf0;color:#16181d}
.shead b{font-size:15px}
.nav{padding:0 10px 6px;display:flex;flex-direction:column;gap:2px}
.nbtn{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:var(--rs);font-size:13.5px;font-weight:600;width:100%;text-align:left}
.nbtn:hover{background:var(--acs)}.nbtn .ni{width:18px;text-align:center}
.nbtn kbd{margin-left:auto;font-size:10px;color:var(--dim);border:1px solid var(--bd);border-radius:5px;padding:1px 5px;font-family:var(--mono)}
.nbtn.tog{background:var(--acs)}
.slabel{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);font-weight:700;padding:10px 18px 5px}
#chatList{flex:1;overflow-y:auto;padding:0 10px 10px}
.ci{display:flex;align-items:center;width:100%;padding:7px 8px 7px 11px;border-radius:var(--rs);color:var(--mut);font-size:13.5px;gap:6px;cursor:pointer}
.ci span{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci:hover{background:var(--acs);color:var(--tx)}.ci.on{background:var(--acs);color:var(--tx);font-weight:600}
.ci .del{opacity:0;color:var(--dim);font-size:12px;padding:2px 5px;border-radius:6px}
.ci:hover .del{opacity:1}.ci .del:hover{color:var(--err)}
.sfoot{padding:11px 14px;border-top:1px solid var(--bd);display:flex;align-items:center;gap:9px}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--warn);flex-shrink:0}.sdot.ok{background:var(--ok)}.sdot.err{background:var(--err)}
.sfoot small{color:var(--mut);font-size:11.5px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gear{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--mut)}
.gear:hover{background:var(--acs);color:var(--tx)}
main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
.top{height:52px;flex-shrink:0;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:12px;padding:0 14px}
.mdd{position:relative}
.mdd>button{display:flex;align-items:center;gap:8px;padding:6px 11px;border-radius:10px;font-weight:700}
.mdd>button:hover{background:var(--soft)}.mdd .car{color:var(--dim);font-size:10px}
.menu{position:absolute;top:calc(100% + 6px);left:0;min-width:230px;max-height:300px;overflow-y:auto;background:var(--bg);border:1px solid var(--bd);border-radius:12px;box-shadow:var(--sh);padding:5px;z-index:50;display:none}
.menu.open{display:block}
.mi{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:8px 11px;border-radius:8px;font-size:13.5px}
.mi:hover{background:var(--soft)}.mi .chk{margin-left:auto;font-weight:700;opacity:0}.mi.sel .chk{opacity:1}
.top .title{font-weight:500;color:var(--mut);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mode{margin-left:auto;display:none;font-size:12px;font-weight:700;background:var(--acs);border-radius:99px;padding:5px 12px}
.mode.show{display:flex}
#stage{flex:1;display:flex;flex-direction:column;min-height:0}
#scroll{flex:1;overflow-y:auto}
#thread{max-width:var(--maxw);margin:0 auto;padding:26px 22px 12px;display:flex;flex-direction:column;gap:20px}
#heroWrap{flex:1;display:none;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:24px}
main.empty #scroll{display:none}main.empty #heroWrap{display:flex}
#heroGreet{font-size:clamp(22px,3.2vw,32px);font-weight:700;letter-spacing:-.025em;background:var(--ink);
-webkit-background-clip:text;background-clip:text;color:transparent}
.srow{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:640px}
.sg{padding:7px 13px;border:1px solid var(--bd);border-radius:99px;background:var(--bg);font-size:12.5px;color:var(--mut)}
.sg:hover{color:var(--tx);box-shadow:var(--sh)}
#dock{padding:8px 22px 14px;flex-shrink:0}
main.empty #dock{position:absolute;left:0;right:0;top:50%;transform:translateY(calc(-50% + 26px));padding:0 22px}
.comp{max-width:var(--maxw);margin:0 auto;background:var(--bg);border:1px solid var(--bd);border-radius:18px;padding:12px 13px 9px;box-shadow:var(--sh)}
.comp:focus-within{border-color:var(--bdh)}
.comp textarea{width:100%;border:none;outline:none;resize:none;font-size:15px;max-height:200px;line-height:1.5;background:none}
.cbar{display:flex;align-items:center;gap:6px;margin-top:7px}
.chip{font-size:11.5px;color:var(--mut);border:1px solid var(--bd);border-radius:99px;padding:3px 10px;background:var(--softer)}
.chip.dr{display:none;border-color:transparent;background:var(--acs);font-weight:700;color:var(--tx)}
.chip.dr.show{display:inline-flex;gap:5px}
#send{margin-left:auto;width:34px;height:34px;border-radius:11px;background:var(--ac);color:var(--bg);display:flex;align-items:center;justify-content:center;transition:transform .1s}
[data-theme=dark] #send{background:#ececf0;color:#16181d}
#send:hover{transform:scale(1.06)}#send:disabled{opacity:.22;cursor:not-allowed;transform:none}
#send.stop{background:var(--err);color:#fff}
.fnote{max-width:var(--maxw);margin:7px auto 0;text-align:center;color:var(--dim);font-size:11px}
main.empty .fnote{display:none}
.msg{display:flex;gap:13px;animation:mIn .15s var(--ease);position:relative}
@keyframes mIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.av{width:28px;height:28px;border-radius:9px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px;margin-top:2px}
.msg.user .av{background:var(--acs)}.msg.agent .av{background:var(--ac);color:var(--bg)}
[data-theme=dark] .msg.agent .av{background:#ececf0;color:#16181d}
.bub{flex:1;min-width:0}
.who{font-size:12px;font-weight:700;margin-bottom:2px;display:flex;align-items:center;gap:8px}
.who .t{font-weight:400;color:var(--dim);font-size:11px}body.nots .who .t{display:none}
.cp{opacity:0;margin-left:auto;color:var(--dim);font-size:12px;padding:2px 7px;border-radius:6px}
.msg:hover .cp{opacity:1}.cp:hover{background:var(--soft);color:var(--tx)}
.ct{font-size:var(--msg);white-space:pre-wrap;word-wrap:break-word}
.ct code{font-family:var(--mono);background:var(--soft);border:1px solid var(--bd);padding:1px 5px;border-radius:5px;font-size:.86em}
.ct pre{background:var(--pre);border-radius:var(--rs);padding:13px;overflow-x:auto;margin:9px 0;white-space:pre}
.ct pre code{background:none;border:none;padding:0;color:var(--pretx);font-size:12.8px}
.cur{display:inline-block;width:8px;height:15px;background:var(--tx);vertical-align:-2px;border-radius:2px;animation:bl 1s steps(1) infinite}
@keyframes bl{50%{opacity:0}}
.pw{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:12.5px;padding:4px 0}
.pu{width:13px;height:13px;border-radius:50%;background:var(--tx);animation:pu 1.1s ease-in-out infinite}
@keyframes pu{0%,100%{transform:scale(.55);opacity:.4}50%{transform:scale(1);opacity:1}}
.tc{margin:7px 0 3px;border:1px solid var(--bd);border-radius:var(--rs);background:var(--bg);overflow:hidden;font-size:13px}
.th{display:flex;align-items:center;gap:8px;padding:8px 12px;color:var(--mut);cursor:pointer;flex-wrap:wrap}
.th b{color:var(--tx);font-size:12.5px}
.th .det{font-family:var(--mono);font-size:11.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;max-width:320px;white-space:nowrap}
.th .sp{width:12px;height:12px;border:2px solid var(--bdh);border-top-color:var(--tx);border-radius:50%;animation:rot .6s linear infinite;margin-left:auto}
@keyframes rot{to{transform:rotate(360deg)}}
.th .ok2{margin-left:auto;font-size:12px;font-weight:700;color:var(--ok)}.th .ok2.bad{color:var(--err)}
.tconf{margin-left:auto;display:flex;gap:6px}
.tconf button{font-size:11.5px;font-weight:700;padding:4px 12px;border-radius:99px}
.tconf .y{background:var(--ac);color:var(--bg)}[data-theme=dark] .tconf .y{background:#ececf0;color:#16181d}
.tconf .n{border:1px solid var(--bd);color:var(--mut)}
.tb{display:none;border-top:1px solid var(--bd);padding:9px 12px;font-family:var(--mono);font-size:12.2px;color:var(--mut);white-space:pre-wrap;max-height:170px;overflow-y:auto;background:var(--softer)}
.tc.open .tb{display:block}
#setOv{position:fixed;inset:0;z-index:130;background:rgba(0,0,0,.3);display:none}
#setOv.show{display:block}
.set{position:absolute;top:0;right:0;bottom:0;width:min(380px,100%);background:var(--bg);border-left:1px solid var(--bd);
box-shadow:var(--sh);padding:22px 24px;overflow-y:auto;transform:translateX(100%);transition:transform .22s var(--ease)}
#setOv.show .set{transform:none}
.set h3{font-size:17px}.set .x{position:absolute;top:16px;right:16px;width:28px;height:28px;border-radius:8px;color:var(--mut)}
.set .x:hover{background:var(--soft)}
.sec{margin-top:20px}.sec>label{display:block;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
.sr{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 0;font-size:13.5px}
.sr .s2{color:var(--dim);font-size:11.5px;display:block}
.sr input[type=range]{width:120px;accent-color:var(--tx)}
.sw{display:flex;gap:7px}
.swb{width:26px;height:26px;border-radius:50%;border:2px solid transparent}
.swb.sel{border-color:var(--tx);box-shadow:inset 0 0 0 2px var(--bg)}
.seg{display:flex;border:1px solid var(--bd);border-radius:99px;overflow:hidden}
.seg button{padding:4px 12px;font-size:12px;font-weight:600;color:var(--mut)}
.seg button.on{background:var(--ac);color:var(--bg)}[data-theme=dark] .seg button.on{background:#ececf0;color:#16181d}
.tgl{width:34px;height:19px;border-radius:99px;background:var(--bdh);position:relative;transition:background .15s;flex-shrink:0}
.tgl::after{content:'';position:absolute;top:2px;left:2px;width:15px;height:15px;border-radius:50%;background:var(--bg);transition:left .15s}
.tgl.on{background:var(--ok)}.tgl.on::after{left:17px}
.set textarea,.set input[type=text]{width:100%;padding:8px 11px;border:1px solid var(--bd);border-radius:var(--rs);background:var(--softer);outline:none;font-size:13px}
.set textarea{resize:vertical;min-height:60px}
.lk{font-size:13px;font-weight:600;padding:7px 0;color:var(--tx);text-decoration:underline}
@media(max-width:760px){aside{display:none}}
</style></head><body>
<div id="splash" class="splashbg"><h1 id="wt">Welcome, user.</h1></div>
<div id="setupOv"><div class="panel">
 <div class="step active" data-step="1"><h2>Choose your platform</h2><p class="sub">Which local model runtime are you using?</p>
  <div class="f"><label>Platform</label><select id="platform"><option value="" disabled selected>Select…</option>
   <option value="ollama">Ollama</option><option value="lmstudio">LM Studio</option>
   <option value="opencode">OpenCode</option><option value="other">Something else (OpenAI-compatible)</option></select></div>
  <div class="acts"><span></span><button class="btn" id="s2" disabled>Continue</button></div></div>
 <div class="step" data-step="2"><h2>Set it up</h2><p class="sub">Reconfigure anytime in ⚙ Settings.</p>
  <div class="f"><label>Endpoint URL</label><input type="text" id="endpoint" spellcheck="false"></div>
  <div class="tline" id="tline"></div>
  <div class="f"><label>Default model</label><select id="setupModel"><option value="">— test to list models —</option></select></div>
  <div class="acts"><button class="bg2" id="b1">← Back</button>
   <div style="display:flex;gap:8px"><button class="bg2" id="testBtn">Test</button><button class="btn" id="finishBtn">Start →</button></div></div></div>
</div></div>
<div id="app"><aside>
 <div class="shead"><div class="mark">✦</div><b>Aria</b></div>
 <div class="nav">
  <button class="nbtn" id="newChat"><span class="ni">✚</span>New Chat<kbd>⌘K</kbd></button>
  <button class="nbtn" id="deepBtn"><span class="ni">◎</span>Deep research</button></div>
 <div class="slabel">Chats</div><div id="chatList"></div>
 <div class="sfoot"><div class="sdot" id="sdot"></div><small id="stext">Connecting…</small>
  <button class="gear" id="gearBtn">⚙</button></div></aside>
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
  <div id="dock"><div class="comp">
    <textarea id="input" rows="1" placeholder="Message Aria…"></textarea>
    <div class="cbar"><span class="chip" id="provChip">—</span>
     <span class="chip dr" id="drChip">◎ Deep research <button id="drOff">✕</button></span>
     <button id="send" disabled>➤</button></div></div>
   <div class="fnote">Real tools on your machine · Esc to stop · approve shell &amp; write actions</div></div>
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
const st={chats:[],cur:null,busy:false,abort:null,online:false,models:[]};
try{st.chats=JSON.parse(localStorage.getItem(KC))||[]}catch(e){}
const save=()=>localStorage.setItem(K,JSON.stringify(cfg)),saveC=()=>localStorage.setItem(KC,JSON.stringify(st.chats));
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const md=s=>{let o=esc(s);o=o.replace(/```(\w*)\n([\s\S]*?)(```|$)/g,(_,l,c)=>`<pre><code>${c}</code></pre>`);
 o=o.replace(/`([^`\n]+)`/g,'<code>$1</code>');return o.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')};
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
const goStep=n=>$$('.step').forEach(s=>s.classList.toggle('active',s.dataset.step==n));
$('#platform').onchange=e=>$('#s2').disabled=!e.target.value;
$('#s2').onclick=()=>{$('#endpoint').value=cfg?.baseUrl||URLS[$('#platform').value];$('#tline').innerHTML='';goStep(2)};
$('#b1').onclick=()=>goStep(1);
async function getModels(p,u){try{const r=await fetch(`/api/models?platform=${encodeURIComponent(p)}&url=${encodeURIComponent(u)}`);
 const j=await r.json();return j.ok?j.models:null}catch(e){return null}}
$('#testBtn').onclick=async()=>{const l=$('#tline');
 l.innerHTML='<span class="d" style="background:var(--warn)"></span>Testing…';
 const m=await getModels($('#platform').value,$('#endpoint').value.trim());
 if(m){l.innerHTML=`<span class="d" style="background:var(--ok)"></span><span style="color:var(--ok)">Connected — ${m.length} models</span>`;
  $('#setupModel').innerHTML=m.map(x=>`<option>${x}</option>`).join('')}
 else l.innerHTML='<span class="d" style="background:var(--err)"></span><span style="color:var(--err)">Unreachable — start your runtime</span>'};
$('#finishBtn').onclick=()=>{cfg={...DEF,...(cfg||{}),platform:$('#platform').value,
 baseUrl:$('#endpoint').value.trim().replace(/\/$/,''),model:$('#setupModel').value||''};
 save();$('#setupOv').classList.remove('show');enter()};
async function connect(){const m=await getModels(cfg.platform,cfg.baseUrl);
 if(m&&m.length){st.online=true;st.models=m;if(!m.includes(cfg.model))cfg.model=m[0];
  $('#sdot').className='sdot ok';$('#stext').textContent='Connected · '+cfg.baseUrl.replace(/^https?:\/\//,'')}
 else{st.online=false;st.models=[];$('#sdot').className='sdot err';$('#stext').textContent='Offline — start '+(PLAT[cfg.platform]||'runtime')}
 mm();save()}
function mm(){$('#modelName').textContent=cfg.model||'—';
 $('#modelMenu').innerHTML=st.models.map(m=>`<button class="mi ${m===cfg.model?'sel':''}" data-m="${m}">${m}<span class="chk">✓</span></button>`).join('')
 ||'<div style="padding:10px;color:var(--dim);font-size:12.5px">No models found</div>'}
$('#modelBtn').onclick=e=>{e.stopPropagation();$('#modelMenu').classList.toggle('open')};
$('#modelMenu').onclick=e=>{const i=e.target.closest('.mi');if(!i)return;
 cfg.model=i.dataset.m;save();mm();$('#modelMenu').classList.remove('open')};
document.addEventListener('click',()=>$('#modelMenu').classList.remove('open'));
function enter(){theme();$('#app').classList.add('active');
 $('#provChip').textContent=PLAT[cfg.platform]||cfg.platform;
 $('#heroGreet').textContent=`How can I help, ${cfg.userName}?`;
 if(!st.cur){st.cur=st.chats[0]||null;if(!st.cur)nc(true)}
 sUI();rc();rt();connect();setTimeout(()=>$('#input').focus(),40)}
function nc(s){const c={id:Date.now(),title:'New chat',msgs:[]};st.chats.unshift(c);st.cur=c;saveC();
 if(!s){rc();rt();$('#input').focus()}}
$('#newChat').onclick=()=>nc();
document.addEventListener('keydown',e=>{
 if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();nc()}
 if(e.key==='Escape'&&st.busy)st.abort?.abort()});
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
 st.cur.msgs.forEach(m=>t.appendChild(me(m)));sd(true)}
function me(m){const d=document.createElement('div');d.className=`msg ${m.role==='user'?'user':'agent'}`;
 d.innerHTML=`<div class="av">${m.role==='user'?'🧑':'✦'}</div><div class="bub">
 <div class="who">${m.role==='user'?esc(cfg.userName):'Aria'}<span class="t">${m.time||''}</span>
 <button class="cp" title="Copy">⧉</button></div><div class="tools"></div><div class="ct">${md(m.text||'')}</div></div>`;
 d.querySelector('.cp').onclick=()=>navigator.clipboard?.writeText(m.text||'');return d}
const sd=i=>{const s=$('#scroll');s.scrollTo({top:s.scrollHeight,behavior:i?'auto':'smooth'})};
const now=()=>new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
function drop(){const m=$('#main');if(!m.classList.contains('empty'))return;
 const d=$('#dock'),a=d.getBoundingClientRect();m.classList.remove('empty');
 const b=d.getBoundingClientRect();
 d.animate([{transform:`translateY(${a.top-b.top}px)`},{transform:'none'}],{duration:280,easing:'cubic-bezier(.2,.8,.2,1)'})}
const inp=$('#input'),sb=$('#send');
inp.oninput=()=>{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,200)+'px';
 sb.disabled=!inp.value.trim()&&!st.busy};
inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(inp.value.trim())send()}};
sb.onclick=()=>{if(st.busy){st.abort?.abort();return}send()};
$('#srow').onclick=e=>{const s=e.target.closest('.sg');if(!s)return;
 inp.value=s.dataset.p;inp.dispatchEvent(new Event('input'));send()};
async function send(){
 const text=inp.value.trim();if(!text||st.busy)return;
 inp.value='';inp.style.height='auto';
 const c=st.cur;c.msgs.push({role:'user',text,time:now()});
 if(c.title==='New chat'){c.title=text.slice(0,42)+(text.length>42?'…':'');rc()}
 drop();$('#thread').appendChild(me(c.msgs[c.msgs.length-1]));$('#chatTitle').textContent=c.title;
 const am={role:'assistant',text:'',time:now()};c.msgs.push(am);
 const el=me(am);$('#thread').appendChild(el);
 const ce=el.querySelector('.ct'),te=el.querySelector('.tools');
 ce.innerHTML='<div class="pw"><div class="pu"></div>'+(cfg.deepResearch?'Researching…':'Thinking…')+'</div>';sd();
 st.busy=true;sb.classList.add('stop');sb.textContent='■';sb.disabled=false;
 st.abort=new AbortController();
 const cards={};
 const hist=c.msgs.filter(m=>m.text&&m!==am).map(m=>({role:m.role==='user'?'user':'assistant',content:m.text}));
 try{
  const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({cfg,messages:hist}),signal:st.abort.signal});
  const rd=r.body.getReader(),dc=new TextDecoder();let buf='';
  while(true){const{done,value}=await rd.read();if(done)break;
   buf+=dc.decode(value,{stream:true});const ps=buf.split('\n\n');buf=ps.pop();
   for(const p of ps){const l=p.replace(/^data:\s*/,'').trim();if(!l)continue;
    let ev;try{ev=JSON.parse(l)}catch(e){continue}
    if(ev.type==='token'){am.text+=ev.text;ce.innerHTML=md(am.text)+'<span class="cur"></span>';sd(true)}
    else if(ev.type==='tool'){
     if(!am.text)ce.innerHTML='';
     let cd=cards[ev.id];
     if(!cd){cd=document.createElement('div');cd.className='tc'+(cfg.expandTools?' open':'');cards[ev.id]=cd;te.appendChild(cd)}
     cd.dataset.det=ev.detail||'';
     const h=`<span>${IC[ev.tool]||'🔧'}</span><b>${ev.tool}</b><span class="det">${esc(ev.detail||'')}</span>`;
     if(ev.status==='gen')
      cd.innerHTML=`<div class="th">${h}<span style="margin-left:auto;font-size:11px;color:var(--dim)">generating</span><div class="sp" style="margin-left:8px"></div></div>`;
     else if(ev.status==='confirm'){
      cd.innerHTML=`<div class="th">${h}<div class="tconf"><button class="n" data-a="0">Deny</button><button class="y" data-a="1">Approve</button></div></div>`;
      cd.querySelector('.tconf').onclick=async e2=>{const b=e2.target.closest('button');if(!b)return;
       cd.querySelector('.tconf').outerHTML=b.dataset.a==='1'?'<div class="sp"></div>':'<span class="ok2 bad">denied</span>';
       await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:ev.id,approved:b.dataset.a==='1'})})}}
     else cd.innerHTML=`<div class="th">${h}<div class="sp"></div></div>`;
     sd(true)}
    else if(ev.type==='tool_progress'){const cd=cards[ev.id];if(!cd)continue;
     const d=cd.querySelector('.det');
     if(d)d.textContent=(cd.dataset.det||'')+' · '+(ev.size/1024).toFixed(1)+' KB';sd(true)}
    else if(ev.type==='tool_end'){const cd=cards[ev.id];if(!cd)continue;
     const h=cd.querySelector('.th');h.querySelector('.sp')?.remove();h.querySelector('.tconf')?.remove();
     if(!h.querySelector('.ok2'))h.insertAdjacentHTML('beforeend',`<span class="ok2 ${ev.ok?'':'bad'}">${ev.ok?'✓':'✕'}</span>`);
     if(ev.output){const b=document.createElement('div');b.className='tb';b.textContent=ev.output;cd.appendChild(b);
      h.onclick=()=>cd.classList.toggle('open')}
     sd(true)}
    else if(ev.type==='error')am.text+=(am.text?'\n\n':'')+'⚠️ '+ev.text}}
 }catch(e){if(e.name!=='AbortError')am.text+=(am.text?'\n\n':'')+'⚠️ '+
  (st.online?(e.message||'Connection error'):'Model offline — start '+(PLAT[cfg.platform]||'your runtime')+' and reconnect via ⚙.')}
 ce.innerHTML=md(am.text)||'<span style="color:var(--dim)">［stopped］</span>';
 st.busy=false;sb.classList.remove('stop');sb.textContent='➤';sb.disabled=!inp.value.trim();
 saveC();sd()}
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
$('#gearBtn').onclick=()=>{sUI();$('#setOv').classList.add('show')};
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
$('#reconfig').onclick=()=>{$('#setOv').classList.remove('show');$('#app').classList.remove('active');
 $('#platform').value=cfg.platform;$('#platform').dispatchEvent(new Event('change'));
 $('#setupOv').classList.add('show');goStep(1)};
})();
</script></body></html>"""

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"✦ Aria → http://127.0.0.1:{PORT}\n  Workspace: {ROOT}")
    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
