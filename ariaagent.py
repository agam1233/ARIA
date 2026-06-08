
import requests
import json
import sys
import subprocess
import re
import os
import time
import datetime
import shutil
import threading
import tempfile
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════
#  VERSION & CONSTANTS
# ══════════════════════════════════════════════════════════════════════

VERSION      = "Build 1 Beta"
DEFAULT_MODEL = "qwen3.5:9b"
OLLAMA_BASE  = "http://localhost:11434"
CHAT_URL     = f"{OLLAMA_BASE}/api/chat"
SESSION_DIR  = Path.home() / ".aria_sessions"
CONFIG_FILE  = Path.home() / ".aria_config.json"
MODEL_MEMORY_FILE = Path.home() / ".aria_model_config.json"
BACKUP_DIR   = Path.home() / ".aria_backups"
MAX_TOOL_LOOPS = 10
MAX_OUTPUT_LEN = 8000

SESSION_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════
#  COLORS
# ══════════════════════════════════════════════════════════════════════

class C:
    R = "\033[0m"; B = "\033[1m"; GRAY = "\033[90m"; RED = "\033[91m"
    GREEN = "\033[92m"; YELLOW = "\033[93m"; BLUE = "\033[94m"
    MAGENTA = "\033[95m"; CYAN = "\033[96m"; WHITE = "\033[97m"
    DGREEN = "\033[32m"

def c(color, text): return f"{color}{text}{C.R}"

# ══════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════

def get_terminal_width():
    try: return os.get_terminal_size().columns
    except: return 80

def detect_workspace():
    cwd = Path.cwd()
    info = []
    try:
        root = subprocess.check_output(['git','rev-parse','--show-toplevel'], stderr=subprocess.DEVNULL, text=True).strip()
        branch = subprocess.check_output(['git','branch','--show-current'], stderr=subprocess.DEVNULL, text=True).strip()
        info.append(f"git:{Path(root).name}@{branch}")
    except: pass
    if os.environ.get('VIRTUAL_ENV'):
        info.append(f"venv:{Path(os.environ['VIRTUAL_ENV']).name}")
    return ' · '.join(info) if info else None

def truncate_output(text, max_len=MAX_OUTPUT_LEN):
    if len(text) <= max_len: return text
    return text[:max_len] + f"\n... (truncated, {len(text)-max_len} chars omitted)"

# ══════════════════════════════════════════════════════════════════════
#  DANGEROUS COMMAND DETECTION
# ══════════════════════════════════════════════════════════════════════

DANGEROUS_PATTERNS = [
    r'rm\s+(-rf?|--recursive)?\s*/', r'sudo\s+', r'dd\s+', r'mkfs',
    r'>\s*/dev/sd[a-z]', r':\(\)\s*{\s*:;\s*};:', r'chmod\s+777\s+/',
    r'curl.*\|\s*sh', r'wget.*\|\s*sh', r'killall', r'pkill', r'kill\s+-9',
    r'nc\s+-l', r'nc\s+-e', r'python\s+-c\s+[\'"]import\s+os'
]

def is_dangerous_command(cmd):
    cmd_lower = cmd.lower()
    return any(re.search(p, cmd_lower) for p in DANGEROUS_PATTERNS)

def danger_confirm(action_desc, details=""):
    print(f"\n  {c(C.RED+C.B, '⚠  DANGEROUS ACTION REQUESTED')}")
    print(f"  {c(C.YELLOW, 'Action  :')} {c(C.WHITE+C.B, action_desc)}")
    if details:
        print(f"  {c(C.YELLOW, 'Details :')} {c(C.GRAY, details[:200])}")
    print(f"  Type {c(C.RED+C.B, 'YES')} to allow, anything else to cancel.")
    return input(f"  {c(C.RED,'▶')} ").strip() == "YES"

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

DEFAULT_CFG = {
    "model": DEFAULT_MODEL, "temperature": 0.4, "top_p": 0.95,
    "ctx": 64000, "max_tokens": -1, "stream": True, "search_n": 5,
    "allow_shell": False, "allow_network": True, "allow_write": True, "danger_confirm": True,
}
cfg = dict(DEFAULT_CFG)

def load_config():
    global cfg
    if CONFIG_FILE.exists():
        try: cfg.update(json.loads(CONFIG_FILE.read_text()))
        except: pass
    for k, v in DEFAULT_CFG.items():
        if k not in cfg: cfg[k] = v

def save_config():
    try: CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except: pass

# Persistent model memory (user preferences)
DEFAULT_MEMORY = {
    "browser_cmd": "firefox", "editor_cmd": "code",
    "default_project": str(Path.home() / "projects"), "shell_enabled": False
}
model_memory = dict(DEFAULT_MEMORY)

def load_model_memory():
    global model_memory
    if MODEL_MEMORY_FILE.exists():
        try: model_memory.update(json.loads(MODEL_MEMORY_FILE.read_text()))
        except: pass
    for k, v in DEFAULT_MEMORY.items():
        if k not in model_memory: model_memory[k] = v

def save_model_memory():
    try: MODEL_MEMORY_FILE.write_text(json.dumps(model_memory, indent=2))
    except: pass

def first_time_setup():
    print(f"\n  {c(C.CYAN+C.B, '✨ First-time setup')} {c(C.GRAY, 'Let me know your system')}")
    print(f"  {c(C.GRAY, '────────────────────────────────────────────────────────')}")
    browser = input(f"  {c(C.YELLOW, 'Default browser command')} {c(C.GRAY, '[firefox]')}: ").strip()
    model_memory["browser_cmd"] = browser if browser else "firefox"
    editor = input(f"  {c(C.YELLOW, 'Default editor command')} {c(C.GRAY, '[code]')}: ").strip()
    model_memory["editor_cmd"] = editor if editor else "code"
    proj = input(f"  {c(C.YELLOW, 'Projects folder')} {c(C.GRAY, f'[{DEFAULT_MEMORY["default_project"]}]')}: ").strip()
    model_memory["default_project"] = proj if proj else DEFAULT_MEMORY["default_project"]
    Path(model_memory["default_project"]).mkdir(parents=True, exist_ok=True)
    enable = input(f"  {c(C.YELLOW, 'Enable shell access?')} {c(C.GRAY, '(yes/no) [yes]')}: ").strip().lower()
    model_memory["shell_enabled"] = enable in ('yes', 'y', 'true', '')
    save_model_memory()
    cfg["allow_shell"] = model_memory["shell_enabled"]
    save_config()
    print(f"\n  {c(C.GREEN,'✓')}  Preferences saved. Shell: {'ON' if cfg['allow_shell'] else 'OFF'}")
    time.sleep(1)

# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT (injects user preferences)
# ══════════════════════════════════════════════════════════════════════

def system_prompt():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cwd = os.getcwd()
    ws = detect_workspace()
    shell_status = "ENABLED (dangerous blocked)" if cfg["allow_shell"] else "DISABLED"
    return f"""Your ARIA, an Autonomous Reasoning Intelligent Agent. Be direct and efficient.

[ENVIRONMENT]
Time: {now} | CWD: {cwd} | Workspace: {ws or 'none'}
Security: Shell={shell_status} | Network={cfg["allow_network"]}

[USER PREFERENCES]
- Browser: `{model_memory["browser_cmd"]}` (use this to open URLs)
- Editor: `{model_memory["editor_cmd"]}`
- Projects: `{model_memory["default_project"]}`

[TOOLS] – Use ONE tag at a time, wait for result.
<search>query</search>
<read>file</read>
<summarize>file</summarize>
<http url="URL"/>
<execute>command</execute>
(if you need to write a file use touch in execute.)
<pyeval>python code</pyeval>
<listdir>path</listdir>
<glob>pattern</glob>
<grep>pattern file</grep>
<edit file="path" pattern="regex">replacement</edit>
<move src="from" dest="to"/>
<copy src="from" dest="to"/>
<cd>path</cd>

Example: <execute>{model_memory["browser_cmd"]} https://youtube.com</execute>
Do not nest tags. Close all tags properly.
ALWAYS code the best html possible.
"""

# ══════════════════════════════════════════════════════════════════════
#  SPINNER & UI
# ══════════════════════════════════════════════════════════════════════

class Spinner:
    def __init__(self, msg="thinking"):
        self.msg = msg
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._spin, daemon=True)
    def _spin(self):
        frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r  {c(C.CYAN, frames[i%10])} {c(C.GRAY, self.msg)}   ")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1
    def start(self): self._t.start(); return self
    def stop(self):
        self._stop.set(); self._t.join(timeout=0.5)
        sys.stdout.write(f"\r{' '*60}\r"); sys.stdout.flush()

def ok(m): print(f"  {c(C.GREEN,'✓')}  {m}")
def warn(m): print(f"  {c(C.YELLOW,'⚠')}  {m}")
def err(m): print(f"  {c(C.RED,'✗')}  {m}")

def banner():
    print(f"\n  {c(C.CYAN+C.B, 'ARIA v' + VERSION)} {c(C.GRAY, '· Autonomous Reasoning Agent')}")
    print(f"  {c(C.GRAY, '────────────────────────────────────────────────────────')}")
    print(f"  {c(C.GRAY, f'Model: {cfg['model']} | Ctx: {cfg['ctx']} | Temp: {cfg['temperature']}')}")
    print(f"  {c(C.GRAY, f'Safety: Shell={cfg['allow_shell']} Network={cfg['allow_network']} Write={cfg['allow_write']}')}\n")

def prompt_line():
    ws = detect_workspace()
    ws_str = f" {c(C.DGREEN, ws)}" if ws else ""
    return f"\n  {c(C.GREEN+C.B,'▶')} {c(C.WHITE+C.B,'You')}{ws_str}: "

# ══════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════

def change_directory(path):
    p = Path(path).expanduser().resolve()
    if not p.exists(): return f"Error: {path} does not exist"
    if not p.is_dir(): return f"Error: {path} not a directory"
    try:
        os.chdir(p)
        return f"Changed directory to {p}"
    except Exception as e: return f"cd failed: {e}"

def search_web(query):
    if not cfg["allow_network"]: return "Network disabled"
    try:
        url = "https://lite.duckduckgo.com/lite/"
        resp = requests.post(url, data={"q": query, "d": "on"}, timeout=10,
                             headers={"User-Agent": "ARIA/6.0"})
        resp.raise_for_status()
        results, lines = [], resp.text.splitlines()
        for i, line in enumerate(lines):
            if "result-link" in line.lower() and i+2 < len(lines):
                title = re.sub(r'<[^>]+>', '', lines[i+1]).strip()
                url_match = re.search(r'href="([^"]+)"', lines[i+2])
                if url_match:
                    results.append(f"{title}\n  {url_match.group(1)}")
                if len(results) >= cfg["search_n"]: break
        return "\n\n".join(results) if results else "No results"
    except Exception as e: return f"Search error: {e}"

def http_fetch(url):
    if not cfg["allow_network"]: return "Network disabled"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ARIA/6.0"})
        resp.raise_for_status()
        if 'application/json' in resp.headers.get('content-type', ''):
            return json.dumps(resp.json(), indent=2)[:MAX_OUTPUT_LEN]
        return truncate_output(resp.text)
    except Exception as e: return f"HTTP error: {e}"

def summarize_file(filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found"
    if p.is_dir(): return f"Error: {filepath} is a directory"
    stat = p.stat()
    try:
        with p.open('r', errors='replace') as f:
            lines = sum(1 for _ in f)
            f.seek(0)
            words = sum(len(line.split()) for line in f)
    except:
        lines = words = -1
    preview = ""
    if stat.st_size < 50000 and stat.st_size > 0:
        preview = f"\n\nFirst 20 lines:\n" + "\n".join(p.read_text(errors='replace').splitlines()[:20])
    return f"File: {p.name}\nSize: {stat.st_size} bytes\nLines: {lines}\nWords: {words}{preview}"

def python_eval(code):
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = tempfile.StringIO()
        sys.stderr = tempfile.StringIO()
        exec_globals = {"__name__": "__aria_exec__"}
        exec(code, exec_globals)
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        return truncate_output(out + (f"\n[stderr]:\n{err}" if err else "")) or "(no output)"
    except Exception as e:
        return f"Python error: {e}"
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

def list_directory(path):
    p = Path(path).expanduser().resolve()
    if not p.exists(): return f"Error: {path} does not exist"
    if not p.is_dir(): return f"Error: {path} not a directory"
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"📁 {i.name}" if i.is_dir() else f"📄 {i.name}" for i in items[:100]]
        if len(items) > 100: lines.append(f"... and {len(items)-100} more")
        return "\n".join(lines) if lines else "(empty)"
    except Exception as e: return f"Listdir error: {e}"

def glob_files(pattern):
    try:
        matches = sorted(Path.cwd().glob(pattern))
        return "\n".join(str(p) for p in matches[:200]) or "No matches"
    except Exception as e: return f"Glob error: {e}"

def grep_file(pattern, filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found"
    try:
        lines = p.read_text(errors='replace').splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append(f"{i}: {line.strip()[:200]}")
                if len(results) >= 50: break
        return "\n".join(results) if results else "No matches"
    except Exception as e: return f"Grep error: {e}"

def edit_file(filepath, pattern, replacement):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found"
    try:
        original = p.read_text()
        new_content, count = re.subn(pattern, replacement, original)
        if count == 0: return "No matches found"
        backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
        shutil.copy(p, backup)
        p.write_text(new_content)
        return f"Replaced {count} occurrence(s) in {filepath}. Backup: {backup.name}"
    except Exception as e: return f"Edit failed: {e}"

def move_file(src, dst):
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists(): return f"Error: source {src} not found"
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return f"Moved {src} -> {dst}"
    except Exception as e: return f"Move failed: {e}"

def copy_file(src, dst):
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists(): return f"Error: source {src} not found"
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)
        return f"Copied {src} -> {dst}"
    except Exception as e: return f"Copy failed: {e}"

def run_shell_command(cmd):
    if not cfg["allow_shell"]: return "Shell commands disabled. Enable with /allow shell true"
    if not cmd: return "Empty command"
    if is_dangerous_command(cmd):
        if cfg["danger_confirm"] and not danger_confirm("Potentially dangerous command", cmd):
            return "User denied"
        else:
            return "Command blocked for safety"
    spin = Spinner("running command").start()
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60, cwd=os.getcwd())
        spin.stop()
        out = res.stdout + res.stderr
        return f"Exit code: {res.returncode}\nOutput:\n{truncate_output(out.strip() or '(no output)')}"
    except subprocess.TimeoutExpired:
        spin.stop()
        return "Command timed out after 60 seconds"
    except Exception as e:
        spin.stop()
        return f"Execution failed: {e}"

def write_file(filepath, content):
    if not cfg["allow_write"]: return "File write disabled"
    p = Path(filepath).expanduser()
    try:
        if p.exists() and cfg["danger_confirm"]:
            if not danger_confirm("Overwrite file", f"Overwrite {filepath}?"):
                return "User denied overwrite"
            backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
            shutil.copy(p, backup)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {filepath}"
    except Exception as e: return f"Write failed: {e}"

def read_file(filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found"
    if p.is_dir(): return f"Error: {filepath} is a directory"
    try:
        content = p.read_text(errors='replace')
        return f"Contents of {filepath}:\n\n{truncate_output(content)}"
    except Exception as e: return f"Read error: {e}"

# ══════════════════════════════════════════════════════════════════════
#  TOOL PROCESSOR
# ══════════════════════════════════════════════════════════════════════

def process_tools(text):
    # cd
    cd_match = re.search(r'<cd>(.*?)</cd>', text, re.IGNORECASE | re.DOTALL)
    if cd_match:
        path = cd_match.group(1).strip()
        print(f"\n  {c(C.MAGENTA,'📂')} {c(C.GRAY,'cd:')} {c(C.WHITE, path)}")
        return True, change_directory(path)

    # Standard tools
    tools = [
        ('execute', run_shell_command, r'<execute>(.*?)</execute>', lambda m: m.group(1).strip()),
        ('write', write_file, r'<write\s+file=["\']?([^"\'>\s]+)["\']?[^>]*>(.*?)</write>', lambda m: (m.group(1), m.group(2).strip())),
        ('read', read_file, r'<read>(.*?)</read>', lambda m: m.group(1).strip()),
        ('summarize', summarize_file, r'<summarize>(.*?)</summarize>', lambda m: m.group(1).strip()),
        ('search', search_web, r'<search>(.*?)</search>', lambda m: m.group(1).strip()),
        ('listdir', list_directory, r'<listdir>(.*?)</listdir>', lambda m: m.group(1).strip()),
        ('glob', glob_files, r'<glob>(.*?)</glob>', lambda m: m.group(1).strip()),
        ('grep', grep_file, r'<grep>(.*?)</grep>', lambda m: m.group(1).strip().split(maxsplit=1)),
        ('pyeval', python_eval, r'<pyeval>(.*?)</pyeval>', lambda m: m.group(1).strip()),
    ]
    for tag, handler, pattern, extractor in tools:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            print(f"\n  {c(C.MAGENTA,'⚙️')} {c(C.GRAY, f'{tag}:')} {c(C.WHITE, (match.group(1) or '').strip()[:80])}")
            args = extractor(match)
            result = handler(*args) if isinstance(args, tuple) else handler(args)
            return True, result

    # HTTP
    http_match = re.search(r'<http\s+url=["\']?([^"\'>\s]+)["\']?\s*/?>', text, re.IGNORECASE)
    if not http_match:
        http_match = re.search(r'<http>(.*?)</http>', text, re.IGNORECASE | re.DOTALL)
    if http_match:
        url = http_match.group(1).strip()
        print(f"\n  {c(C.MAGENTA,'🌐')} {c(C.GRAY,'http:')} {c(C.WHITE, url)}")
        return True, http_fetch(url)

    # Edit
    edit_match = re.search(r'<edit\s+file=["\']?([^"\'>\s]+)["\']?\s+pattern=["\']?([^"\'>\s]+)["\']?[^>]*>(.*?)</edit>', text, re.IGNORECASE | re.DOTALL)
    if edit_match:
        filepath, pattern, replacement = edit_match.group(1), edit_match.group(2), edit_match.group(3)
        print(f"\n  {c(C.MAGENTA,'✏️')} {c(C.GRAY,'edit:')} {c(C.WHITE, filepath)}")
        return True, edit_file(filepath, pattern, replacement)

    # Move, Copy
    for tag in ['move', 'copy']:
        m = re.search(rf'<{tag}\s+([^>]+?)\s*/?>', text, re.IGNORECASE)
        if m:
            attrs = m.group(1)
            src = re.search(r'src=["\']?([^"\'>\s]+)', attrs)
            dst = re.search(r'dest=["\']?([^"\'>\s]+)', attrs)
            if src and dst:
                print(f"\n  {c(C.MAGENTA,'📦')} {c(C.GRAY, f'{tag}:')} {c(C.WHITE, src.group(1))} -> {dst.group(1)}")
                if tag == 'move':
                    return True, move_file(src.group(1), dst.group(1))
                else:
                    return True, copy_file(src.group(1), dst.group(1))
    return False, ""

# ══════════════════════════════════════════════════════════════════════
#  INFERENCE & COMMANDS
# ══════════════════════════════════════════════════════════════════════

def run_inference(messages):
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": cfg["stream"],
        "options": {
            "temperature": cfg["temperature"],
            "num_ctx": cfg["ctx"],
            "top_p": cfg["top_p"],
        }
    }
    if cfg["max_tokens"] > 0:
        payload["options"]["num_predict"] = cfg["max_tokens"]
    
    if cfg["stream"]:
        print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: ", end="", flush=True)
        full = ""
        try:
            resp = requests.post(CHAT_URL, json=payload, stream=True, timeout=120)
            for line in resp.iter_lines():
                if line:
                    data = json.loads(line)
                    chunk = data.get("message", {}).get("content", "")
                    full += chunk
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    if data.get("done"):
                        break
            print()
            return full
        except Exception as e:
            err(f"Ollama error: {e}")
            return None
    else:
        spin = Spinner("thinking").start()
        try:
            resp = requests.post(CHAT_URL, json=payload, timeout=120)
            spin.stop()
            if resp.status_code == 200:
                reply = resp.json().get("message", {}).get("content", "")
                print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: {reply}\n")
                return reply
            else:
                err(f"Ollama error: {resp.status_code}")
                return None
        except Exception as e:
            spin.stop()
            err(f"Ollama error: {e}")
            return None

def handle_command(cmd):
    if cmd == '/exit':
        sys.exit(0)
    elif cmd == '/clear':
        return "clear"
    elif cmd.startswith('/model '):
        new_model = cmd[7:].strip()
        if new_model:
            cfg['model'] = new_model
            save_config()
            ok(f"Model switched to {new_model}")
        else:
            warn("Usage: /model <name>")
    elif cmd.startswith('/allow '):
        parts = cmd[7:].split()
        if len(parts) == 2:
            feature, state = parts[0].lower(), parts[1].lower()
            if feature in ('shell', 'network', 'write'):
                if state == 'true':
                    cfg[f'allow_{feature}'] = True
                    ok(f"{feature} access enabled")
                    if feature == 'shell':
                        warn("Shell commands can be dangerous. Use responsibly.")
                elif state == 'false':
                    cfg[f'allow_{feature}'] = False
                    ok(f"{feature} access disabled")
                else:
                    warn("Use true/false")
                save_config()
            else:
                warn("Allowed features: shell, network, write")
        else:
            warn("Usage: /allow <shell|network|write> <true|false>")
    elif cmd == '/reconfigure':
        first_time_setup()
        return "reload"
    elif cmd == '/save':
        session_file = SESSION_DIR / f"session_{int(time.time())}.json"
        session_data = {
            "timestamp": time.time(),
            "messages": messages_history,
            "cfg": cfg,
            "cwd": os.getcwd()
        }
        session_file.write_text(json.dumps(session_data, indent=2))
        ok(f"Session saved to {session_file}")
    elif cmd.startswith('/load '):
        name = cmd[6:].strip()
        session_file = Path(name)
        if not session_file.exists():
            session_file = SESSION_DIR / name
            if not session_file.exists():
                err("Session file not found")
                return
        try:
            data = json.loads(session_file.read_text())
            messages_history[:] = data.get("messages", [])
            cfg.update(data.get("cfg", {}))
            saved_cwd = data.get("cwd")
            if saved_cwd and Path(saved_cwd).exists():
                os.chdir(saved_cwd)
            ok(f"Loaded session from {session_file}")
            return "load"
        except Exception as e:
            err(f"Load failed: {e}")
    elif cmd == '/help':
        print("""
        /exit                – Exit ARIA
        /clear               – Clear conversation
        /model <name>        – Switch model
        /allow <feature> tf  – shell, network, write
        /reconfigure         – Run first-time setup again
        /save                – Save session
        /load <file>         – Load session
        /help                – This help
        """)
    else:
        warn(f"Unknown command: {cmd}")
    return None

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

messages_history = []

def main():
    global messages_history
    load_config()
    load_model_memory()
    if not MODEL_MEMORY_FILE.exists():
        first_time_setup()
    else:
        cfg["allow_shell"] = model_memory.get("shell_enabled", False)
        save_config()
    banner()
    messages_history = [{"role": "system", "content": system_prompt()}]
    while True:
        try:
            user_input = input(prompt_line()).strip()
            if not user_input:
                continue
            if user_input.startswith('/'):
                cmd_res = handle_command(user_input)
                if cmd_res == "clear":
                    messages_history = [{"role": "system", "content": system_prompt()}]
                    ok("Conversation cleared.")
                elif cmd_res == "reload":
                    messages_history[0] = {"role": "system", "content": system_prompt()}
                    ok("Preferences reloaded.")
                continue
            messages_history.append({"role": "user", "content": user_input})
            for _ in range(MAX_TOOL_LOOPS):
                reply = run_inference(messages_history)
                if not reply:
                    break
                messages_history.append({"role": "assistant", "content": reply})
                tool_used, tool_result = process_tools(reply)
                if tool_used:
                    print(f"  {c(C.GRAY,'[Tool executed, feeding result back...]')}")
                    messages_history.append({
                        "role": "user",
                        "content": f"[Tool Result]\n{tool_result}\n\nProceed with the next step or provide final answer."
                    })
                else:
                    break
        except KeyboardInterrupt:
            print(f"\n  {c(C.GRAY,'Ctrl+C — type /exit to quit')}")
        except EOFError:
            print()
            break

if __name__ == "__main__":
    main()