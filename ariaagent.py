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
import io
import difflib
from pathlib import Path

# Wrap readline to prevent crashes on systems (like Windows) without it
try:
    import readline
except ImportError:
    readline = None

# ══════════════════════════════════════════════════════════════════════
#  VERSION & CONSTANTS
# ══════════════════════════════════════════════════════════════════════

VERSION       = " BETA 0.10"
DEFAULT_MODEL = "qwen3.5:9b"
OLLAMA_BASE   = "http://localhost:11434"
CHAT_URL      = f"{OLLAMA_BASE}/api/chat"
SESSION_DIR   = Path.home() / ".aria_sessions"
CONFIG_FILE   = Path.home() / ".aria_config.json"
MODEL_MEMORY_FILE = Path.home() / ".aria_model_config.json"
BACKUP_DIR    = Path.home() / ".aria_backups"
HISTORY_FILE  = Path.home() / ".aria_history"
MAX_TOOL_LOOPS = 10
MAX_OUTPUT_LEN = 8000

SESSION_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════
#  COLORS
# ══════════════════════════════════════════════════════════════════════

class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"; ITALIC = "\033[3m"
    GRAY = "\033[90m"; RED = "\033[91m"; GREEN = "\033[92m"
    YELLOW = "\033[93m"; BLUE = "\033[94m"; MAGENTA = "\033[95m"
    CYAN = "\033[96m"; WHITE = "\033[97m"; DGREEN = "\033[32m"
    BG_RED = "\033[41m"; BG_YELLOW = "\033[43m"; BG_GREEN = "\033[42m"
    BG_BLUE = "\033[44m"; BG_CYAN = "\033[46m"

def c(color, text): return f"{color}{text}{C.R}"

# ══════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════

_start_time = time.time()
_msg_count  = 0

def get_terminal_width():
    try:    return os.get_terminal_size().columns
    except: return 80

def rule(char="─", color=C.GRAY):
    w = min(get_terminal_width() - 4, 72)
    print(f"  {c(color, char * w)}")

def detect_workspace():
    cwd = Path.cwd()
    info = []
    try:
        root   = subprocess.check_output(['git','rev-parse','--show-toplevel'], stderr=subprocess.DEVNULL, text=True).strip()
        branch = subprocess.check_output(['git','branch','--show-current'],     stderr=subprocess.DEVNULL, text=True).strip()
        info.append(f"git:{Path(root).name}@{branch}")
    except: pass
    if os.environ.get('VIRTUAL_ENV'):
        info.append(f"venv:{Path(os.environ['VIRTUAL_ENV']).name}")
    return ' · '.join(info) if info else None

def truncate_output(text, max_len=MAX_OUTPUT_LEN):
    if len(text) <= max_len: return text
    omitted = len(text) - max_len
    return text[:max_len] + f"\n{c(C.YELLOW,'…')} {c(C.GRAY, f'{omitted:,} chars omitted')}"

def uptime_str():
    s = int(time.time() - _start_time)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def suggest_command(bad_cmd, known_cmds):
    matches = difflib.get_close_matches(bad_cmd, known_cmds, n=1, cutoff=0.6)
    return matches[0] if matches else None

# ══════════════════════════════════════════════════════════════════════
#  READLINE HISTORY
# ══════════════════════════════════════════════════════════════════════

def setup_readline():
    if readline is None:
        return
    try:
        if HISTORY_FILE.exists():
            readline.read_history_file(str(HISTORY_FILE))
        readline.set_history_length(500)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass

def save_readline_history():
    if readline is None:
        return
    try:
        readline.write_history_file(str(HISTORY_FILE))
    except Exception:
        pass

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
    return any(re.search(p, cmd.lower()) for p in DANGEROUS_PATTERNS)

def danger_confirm(action_desc, details=""):
    print()
    rule("─", C.RED)
    print(f"  {c(C.RED+C.B, '⚠  DANGEROUS ACTION')}  {c(C.GRAY, action_desc)}")
    if details:
        print(f"  {c(C.GRAY, details[:200])}")
    rule("─", C.RED)
    ans = input(f"  {c(C.RED+C.B, 'Type YES to allow:')} ").strip()
    return ans == "YES"

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

DEFAULT_CFG = {
    "model": DEFAULT_MODEL, "temperature": 0.4, "top_p": 0.95,
    "ctx": 64000, "max_tokens": -1, "stream": True, "search_n": 5,
    "allow_shell": False, "allow_network": True, "allow_write": True,
    "danger_confirm": True,
}
cfg = dict(DEFAULT_CFG)

def load_config():
    global cfg
    if CONFIG_FILE.exists():
        try: cfg.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
        except: pass
    for k, v in DEFAULT_CFG.items():
        if k not in cfg: cfg[k] = v

def save_config():
    try: CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    except: pass

DEFAULT_MEMORY = {
    "browser_cmd": "firefox", "editor_cmd": "code",
    "default_project": str(Path.home() / "projects"), "shell_enabled": False
}
model_memory = dict(DEFAULT_MEMORY)

def load_model_memory():
    global model_memory
    if MODEL_MEMORY_FILE.exists():
        try: model_memory.update(json.loads(MODEL_MEMORY_FILE.read_text(encoding='utf-8')))
        except: pass
    for k, v in DEFAULT_MEMORY.items():
        if k not in model_memory: model_memory[k] = v

def save_model_memory():
    try: MODEL_MEMORY_FILE.write_text(json.dumps(model_memory, indent=2), encoding='utf-8')
    except: pass

def first_time_setup():
    print(f"\n  {c(C.CYAN+C.B, '✨ First-time setup')}")
    rule()
    browser = input(f"  {c(C.YELLOW, 'Default browser')}  {c(C.GRAY,'[firefox]:')} ").strip()
    model_memory["browser_cmd"] = browser or "firefox"
    editor  = input(f"  {c(C.YELLOW, 'Default editor')}   {c(C.GRAY,'[code]:')} ").strip()
    model_memory["editor_cmd"]  = editor or "code"
    default_proj = DEFAULT_MEMORY["default_project"]
    proj    = input(f"  {c(C.YELLOW, 'Projects folder')} {c(C.GRAY, '[' + default_proj + ']:')} ").strip()
    model_memory["default_project"] = proj or DEFAULT_MEMORY["default_project"]
    Path(model_memory["default_project"]).mkdir(parents=True, exist_ok=True)
    enable = input(f"  {c(C.YELLOW, 'Enable shell?')}    {c(C.GRAY,'(yes/no) [yes]:')} ").strip().lower()
    model_memory["shell_enabled"] = enable in ('yes', 'y', 'true', '')
    save_model_memory()
    cfg["allow_shell"] = model_memory["shell_enabled"]
    save_config()
    rule()
    ok(f"Saved. Shell: {'ON' if cfg['allow_shell'] else 'OFF'}")
    time.sleep(0.8)

# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════

def system_prompt():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cwd = os.getcwd()
    ws  = detect_workspace()
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
<write file="path">content</write>
<summarize>file</summarize>
<http url="URL"/>
<execute>command</execute>
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
#  SPINNER
# ══════════════════════════════════════════════════════════════════════

SPINNER_MSGS = {
    "thinking":   ["thinking…", "reasoning…", "processing…", "computing…"],
    "searching":  ["searching the web…", "fetching results…", "looking it up…"],
    "running":    ["running command…", "executing…", "spawning process…"],
    "reading":    ["reading file…", "loading…", "parsing…"],
    "fetching":   ["fetching URL…", "downloading…", "requesting…"],
}

class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    def __init__(self, kind="thinking"):
        msgs = SPINNER_MSGS.get(kind, SPINNER_MSGS["thinking"])
        self.msgs  = msgs
        self._stop = threading.Event()
        self._t    = threading.Thread(target=self._spin, daemon=True)
    def _spin(self):
        i = 0
        while not self._stop.is_set():
            msg = self.msgs[i // 12 % len(self.msgs)]
            sys.stdout.write(f"\r  {c(C.CYAN, self.FRAMES[i % 10])} {c(C.GRAY, msg)}   ")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1
    def start(self):  self._t.start(); return self
    def stop(self):
        self._stop.set(); self._t.join(timeout=0.5)
        sys.stdout.write(f"\r{' ' * 60}\r"); sys.stdout.flush()

# ══════════════════════════════════════════════════════════════════════
#  STATUS HELPERS
# ══════════════════════════════════════════════════════════════════════

def ok(m):   print(f"  {c(C.GREEN,  '✓')}  {m}")
def warn(m): print(f"  {c(C.YELLOW, '⚠')}  {m}")
def err(m):  print(f"  {c(C.RED,    '✗')}  {m}")
def info(m): print(f"  {c(C.BLUE,   '·')}  {c(C.GRAY, m)}")

def tool_header(icon, tag, detail=""):
    trunc = detail[:70] + ("…" if len(detail) > 70 else "")
    print(f"\n  {c(C.MAGENTA, icon)} {c(C.GRAY+C.DIM, tag+':')} {c(C.WHITE, trunc)}")
#note from the dev, what you doing here?
def tool_result_box(result, success=True):
    color  = C.DGREEN if success else C.RED
    lines  = result.splitlines()
    prefix = f"  {c(color, '│')} "
    for ln in lines[:30]:
        print(f"{prefix}{c(C.GRAY, ln)}")
    if len(lines) > 30:
        print(f"{prefix}{c(C.GRAY+C.DIM, f'  … {len(lines)-30} more lines')}")

# ══════════════════════════════════════════════════════════════════════
#  BANNER & PROMPT
# ══════════════════════════════════════════════════════════════════════

def check_ollama():
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        if cfg["model"] in models or any(cfg["model"].split(":")[0] in m for m in models):
            return True, models
        return False, models
    except:
        return None, []

def banner():
    w = min(get_terminal_width(), 76)
    print()
    print(f"  {c(C.CYAN+C.B, 'ARIA')} {c(C.GRAY, 'v'+VERSION)}  {c(C.DIM+C.GRAY, '·  Autonomous Reasoning Agent')}")
    rule()

    online, models = check_ollama()
    if online is None:
        model_status = c(C.RED, '✗ ollama offline')
    elif not online:
        model_status = c(C.YELLOW, f'⚠ model not found  (available: {", ".join(models[:3])})')
    else:
        model_status = c(C.GREEN, '✓ ready')

    shell_col = C.GREEN if cfg['allow_shell']   else C.GRAY
    net_col   = C.GREEN if cfg['allow_network'] else C.GRAY
    write_col = C.GREEN if cfg['allow_write']   else C.GRAY

    print(f"  {c(C.GRAY,'model')}   {c(C.WHITE, cfg['model'])}  {model_status}")
    ctx_str = f"{cfg['ctx']:,}"
    print(f"  {c(C.GRAY,'ctx')}     {c(C.WHITE, ctx_str)}  {c(C.GRAY,'·')}  {c(C.GRAY,'temp')} {c(C.WHITE, cfg['temperature'])}")
    print(f"  {c(C.GRAY,'access')}  shell {c(shell_col, '●')}  network {c(net_col, '●')}  write {c(write_col, '●')}")
    rule()
    info("type /help for commands, ↑↓ for history")
    print()

def prompt_line():
    global _msg_count
    ws     = detect_workspace()
    ws_str = f" {c(C.DGREEN, ws)}" if ws else ""
    cwd    = c(C.GRAY+C.DIM, Path.cwd().name)
    count  = c(C.GRAY+C.DIM, f"#{_msg_count+1}")
    return f"\n  {count} {cwd}{ws_str}\n  {c(C.GREEN+C.B,'▶')} {c(C.WHITE+C.B,'you')}: "

# ══════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════

def change_directory(path):
    p = Path(path).expanduser().resolve()
    if not p.exists():  return f"Error: {path} does not exist", False
    if not p.is_dir():  return f"Error: {path} not a directory", False
    try:
        os.chdir(p)
        return f"Changed directory to {p}", True
    except Exception as e:
        return f"cd failed: {e}", False

def search_web(query):
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        import urllib.parse
        import html
        
        # Use html.duckduckgo for more predictable DOM and spoof a standard browser
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        
        resp = requests.post(url, data={"q": query}, timeout=15, headers=headers)
        resp.raise_for_status()

        results = []
        # Use finditer to search across the entire HTML string, ignoring line breaks
        pattern = r'<a class="result__url" href="([^"]+)".*?>(.*?)</a>'
        
        for match in re.finditer(pattern, resp.text, re.IGNORECASE | re.DOTALL):
            link = match.group(1)
            title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            title = html.unescape(title)

            # DuckDuckGo wraps links in a redirect tracker; extract the real URL
            if 'uddg=' in link:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse("http:" + link).query)
                link = qs.get('uddg', [link])[0]

            results.append(f"{title}\n  {link}")
            if len(results) >= cfg.get("search_n", 5): break
            
        out = "\n\n".join(results) if results else "No results. (HTML structure may have changed or the request was blocked)."
        return out, bool(results)
        
    except Exception as e:
        return f"Search error: {e}", False

def http_fetch(url):
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ARIA/6.0"})
        resp.raise_for_status()
        if 'application/json' in resp.headers.get('content-type', ''):
            try:
                out = json.dumps(resp.json(), indent=2)[:MAX_OUTPUT_LEN]
            except ValueError:
                out = truncate_output(resp.text)
        else:
            out = truncate_output(resp.text)
        return out, True
    except Exception as e:
        return f"HTTP error: {e}", False

def summarize_file(filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    if p.is_dir():     return f"Error: {filepath} is a directory", False
    stat = p.stat()
    try:
        with p.open('r', encoding='utf-8', errors='replace') as f:
            lines = sum(1 for _ in f)
            f.seek(0)
            words = sum(len(l.split()) for l in f)
    except:
        lines = words = -1
    preview = ""
    if 0 < stat.st_size < 50000:
        preview = "\n\nFirst 20 lines:\n" + "\n".join(p.read_text(encoding='utf-8', errors='replace').splitlines()[:20])
    out = f"File: {p.name}\nSize: {stat.st_size:,} bytes\nLines: {lines:,}\nWords: {words:,}{preview}"
    return out, True

def python_eval(code):
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        exec(code, {"__name__": "__aria_exec__"})
        out = sys.stdout.getvalue()
        err_out = sys.stderr.getvalue()
        result = truncate_output(out + (f"\n[stderr]:\n{err_out}" if err_out else "")) or "(no output)"
        return result, True
    except Exception as e:
        return f"Python error: {e}", False
    finally:
        sys.stdout, sys.stderr = old_out, old_err

def list_directory(path):
    p = Path(path).expanduser().resolve()
    if not p.exists(): return f"Error: {path} does not exist", False
    if not p.is_dir(): return f"Error: {path} not a directory", False
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = []
        for i in items[:100]:
            size = ""
            if i.is_file():
                s = i.stat().st_size
                size = f"  {c(C.GRAY+C.DIM, f'{s:,}B')}" if s < 1024 else f"  {c(C.GRAY+C.DIM, f'{s//1024}KB')}"
            icon = "📁" if i.is_dir() else "📄"
            lines.append(f"{icon} {i.name}{size}")
        if len(items) > 100:
            lines.append(f"… and {len(items)-100} more")
        return "\n".join(lines) if lines else "(empty)", True
    except Exception as e:
        return f"Listdir error: {e}", False

def glob_files(pattern):
    try:
        matches = sorted(Path.cwd().glob(pattern))
        out = "\n".join(str(p) for p in matches[:200]) or "No matches"
        return out, bool(matches)
    except Exception as e:
        return f"Glob error: {e}", False

def grep_file(pattern, filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    try:
        lines   = p.read_text(encoding='utf-8', errors='replace').splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append(f"{c(C.GRAY+C.DIM, str(i).rjust(4))}  {line.strip()[:200]}")
                if len(results) >= 50: break
        out = "\n".join(results) if results else "No matches"
        return out, bool(results)
    except Exception as e:
        return f"Grep error: {e}", False

def edit_file(filepath, pattern, replacement):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    try:
        original = p.read_text(encoding='utf-8')
        new_content, count = re.subn(pattern, replacement, original)
        if count == 0: return "No matches found", False
        backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
        shutil.copy(p, backup)
        p.write_text(new_content, encoding='utf-8')
        return f"Replaced {count} occurrence(s) in {filepath}\nBackup: {backup.name}", True
    except Exception as e:
        return f"Edit failed: {e}", False

def move_file(src, dst):
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists(): return f"Error: source {src} not found", False
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return f"Moved {src} → {dst}", True
    except Exception as e:
        return f"Move failed: {e}", False

def copy_file(src, dst):
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists(): return f"Error: source {src} not found", False
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)
        return f"Copied {src} → {dst}", True
    except Exception as e:
        return f"Copy failed: {e}", False

def run_shell_command(cmd):
    if not cfg["allow_shell"]:
        return "Shell disabled — enable with /allow shell true", False
    if not cmd: return "Empty command", False
    if is_dangerous_command(cmd):
        if cfg["danger_confirm"] and not danger_confirm("Dangerous command", cmd):
            return "User denied", False
        else:
            return "Command blocked for safety", False
    spin = Spinner("running").start()
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60, cwd=os.getcwd())
        spin.stop()
        out = res.stdout + res.stderr
        ok_flag = res.returncode == 0
        prefix = c(C.GREEN if ok_flag else C.RED, f"exit {res.returncode}")
        return f"{prefix}\n{truncate_output(out.strip() or '(no output)')}", ok_flag
    except subprocess.TimeoutExpired:
        spin.stop()
        return "Command timed out after 60s", False
    except Exception as e:
        spin.stop()
        return f"Execution failed: {e}", False

def write_file(filepath, content):
    if not cfg["allow_write"]: return "File write disabled", False
    p = Path(filepath).expanduser()
    try:
        if p.exists() and cfg["danger_confirm"]:
            if not danger_confirm("Overwrite file", f"Overwrite {filepath}?"):
                return "User denied overwrite", False
            backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
            shutil.copy(p, backup)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return f"Wrote {len(content):,} chars to {filepath}", True
    except Exception as e:
        return f"Write failed: {e}", False

def read_file(filepath):
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    if p.is_dir():     return f"Error: {filepath} is a directory", False
    try:
        content = p.read_text(encoding='utf-8', errors='replace')
        return f"Contents of {filepath}:\n\n{truncate_output(content)}", True
    except Exception as e:
        return f"Read error: {e}", False

# ══════════════════════════════════════════════════════════════════════
#  TOOL PROCESSOR
# ══════════════════════════════════════════════════════════════════════

def process_tools(text):
    # cd
    cd_m = re.search(r'<cd>(.*?)</cd>', text, re.IGNORECASE | re.DOTALL)
    if cd_m:
        path = cd_m.group(1).strip()
        tool_header("📂", "cd", path)
        result, ok_f = change_directory(path)
        tool_result_box(result, ok_f)
        return True, result

    # standard tools
    tools = [
        ('execute', run_shell_command,
         r'<execute>(.*?)</execute>', lambda m: m.group(1).strip(), "⚡", "running"),
        ('write', write_file,
         r'<write\s+file=["\']([^"\']+)["\'][^>]*>(.*?)</write>',
         lambda m: (m.group(1), m.group(2).strip()), "💾", "reading"),
        ('read', read_file,
         r'<read>(.*?)</read>', lambda m: m.group(1).strip(), "📖", "reading"),
        ('summarize', summarize_file,
         r'<summarize>(.*?)</summarize>', lambda m: m.group(1).strip(), "📋", "reading"),
        ('search', search_web,
         r'<search>(.*?)</search>', lambda m: m.group(1).strip(), "🔍", "searching"),
        ('listdir', list_directory,
         r'<listdir>(.*?)</listdir>', lambda m: m.group(1).strip(), "📁", "reading"),
        ('glob', glob_files,
         r'<glob>(.*?)</glob>', lambda m: m.group(1).strip(), "🔎", "reading"),
        ('grep', grep_file,
         r'<grep>(.*?)</grep>',
         lambda m: m.group(1).strip().split(maxsplit=1), "🔬", "reading"),
        ('pyeval', python_eval,
         r'<pyeval>(.*?)</pyeval>', lambda m: m.group(1).strip(), "🐍", "thinking"),
    ]
    for tag, handler, pattern, extractor, icon, spin_kind in tools:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            detail = (match.group(1) or "").strip()
            tool_header(icon, tag, detail)
            args   = extractor(match)
            result, ok_f = handler(*args) if isinstance(args, tuple) else handler(args)
            tool_result_box(result, ok_f)
            return True, result

    # HTTP
    http_m = re.search(r'<http\s+url=["\']?([^"\'>\s]+)["\']?\s*/?>', text, re.IGNORECASE)
    if not http_m:
        http_m = re.search(r'<http>(.*?)</http>', text, re.IGNORECASE | re.DOTALL)
    if http_m:
        url = http_m.group(1).strip()
        tool_header("🌐", "http", url)
        result, ok_f = http_fetch(url)
        tool_result_box(result, ok_f)
        return True, result

    # edit
    edit_m = re.search(
        r'<edit\s+file=["\']([^"\']+)["\']\s+pattern=["\']([^"\']+)["\'][^>]*>(.*?)</edit>',
        text, re.IGNORECASE | re.DOTALL)
    if edit_m:
        filepath, pattern, replacement = edit_m.group(1), edit_m.group(2), edit_m.group(3)
        tool_header("✏️", "edit", filepath)
        result, ok_f = edit_file(filepath, pattern, replacement)
        tool_result_box(result, ok_f)
        return True, result

    # move / copy
    for tag in ['move', 'copy']:
        m = re.search(rf'<{tag}\s+([^>]+?)\s*/?>', text, re.IGNORECASE)
        if m:
            attrs = m.group(1)
            src   = re.search(r'src=["\']([^"\']+)["\']', attrs)
            dst   = re.search(r'dest=["\']([^"\']+)["\']', attrs)
            if src and dst:
                icon = "📦" if tag == "move" else "📋"
                tool_header(icon, tag, f"{src.group(1)} → {dst.group(1)}")
                fn = move_file if tag == "move" else copy_file
                result, ok_f = fn(src.group(1), dst.group(1))
                tool_result_box(result, ok_f)
                return True, result

    return False, ""

# ══════════════════════════════════════════════════════════════════════
#  INFERENCE
# ══════════════════════════════════════════════════════════════════════

def run_inference(messages):
    global _msg_count
    payload = {
        "model":   cfg["model"],
        "messages": messages,
        "stream":   cfg["stream"],
        "options": {
            "temperature": cfg["temperature"],
            "num_ctx":     cfg["ctx"],
            "top_p":       cfg["top_p"],
        }
    }
    if cfg["max_tokens"] > 0:
        payload["options"]["num_predict"] = cfg["max_tokens"]

    if cfg["stream"]:
        print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: ", end="", flush=True)
        full = ""
        t0   = time.time()
        try:
            resp = requests.post(CHAT_URL, json=payload, stream=True, timeout=120)
            tok_count = 0
            for line in resp.iter_lines():
                if line:
                    data  = json.loads(line)
                    chunk = data.get("message", {}).get("content", "")
                    full += chunk
                    tok_count += 1
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    if data.get("done"):
                        elapsed = time.time() - t0
                        # subtle stats after reply
                        print(f"\n  {c(C.GRAY+C.DIM, f'{elapsed:.1f}s')}")
                        break
            _msg_count += 1
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
                _msg_count += 1
                return reply
            else:
                err(f"Ollama error: {resp.status_code}")
                return None
        except Exception as e:
            spin.stop()
            err(f"Ollama error: {e}")
            return None

# ══════════════════════════════════════════════════════════════════════
#  KNOWN COMMANDS (for suggestions)
# ══════════════════════════════════════════════════════════════════════

KNOWN_COMMANDS = [
    '/exit', '/clear', '/model', '/allow', '/reconfigure',
    '/save', '/load', '/help', '/ls', '/pwd', '/status',
]

# ══════════════════════════════════════════════════════════════════════
#  COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════

def handle_command(cmd):
    parts = cmd.split()
    verb  = parts[0].lower()

    if verb == '/exit':
        show_exit_stats()
        save_readline_history()
        sys.exit(0)

    elif verb == '/clear':
        return "clear"

    elif verb == '/ls':
        path = parts[1] if len(parts) > 1 else "."
        result, ok_f = list_directory(path)
        print()
        tool_result_box(result, ok_f)

    elif verb == '/pwd':
        ok(os.getcwd())

    elif verb == '/status':
        show_status()

    elif verb == '/model':
        if len(parts) > 1:
            cfg['model'] = parts[1]
            save_config()
            ok(f"Model → {parts[1]}")
        else:
            warn("Usage: /model <name>")

    elif verb == '/allow':
        if len(parts) == 3:
            feature, state = parts[1].lower(), parts[2].lower()
            if feature in ('shell', 'network', 'write'):
                val = state in ('true', '1', 'yes', 'on')
                cfg[f'allow_{feature}'] = val
                save_config()
                label = c(C.GREEN if val else C.GRAY, "ON" if val else "OFF")
                ok(f"{feature} access {label}")
                if feature == 'shell' and val:
                    warn("Shell is enabled. Be careful.")
            else:
                warn("Features: shell  network  write")
        else:
            warn("Usage: /allow <shell|network|write> <true|false>")

    elif verb == '/reconfigure':
        first_time_setup()
        return "reload"

    elif verb == '/save':
        session_file = SESSION_DIR / f"session_{int(time.time())}.json"
        session_data = {
            "timestamp": time.time(),
            "messages":  messages_history,
            "cfg":       cfg,
            "cwd":       os.getcwd()
        }
        session_file.write_text(json.dumps(session_data, indent=2), encoding='utf-8')
        ok(f"Session saved → {session_file.name}")

    elif verb == '/load':
        if len(parts) < 2:
            warn("Usage: /load <file>"); return
        name = parts[1]
        sf   = Path(name)
        if not sf.exists():
            sf = SESSION_DIR / name
        if not sf.exists():
            err("Session file not found"); return
        try:
            data = json.loads(sf.read_text(encoding='utf-8'))
            messages_history[:] = data.get("messages", [])
            cfg.update(data.get("cfg", {}))
            saved_cwd = data.get("cwd")
            if saved_cwd and Path(saved_cwd).exists():
                os.chdir(saved_cwd)
            ok(f"Loaded session from {sf.name}")
            return "load"
        except Exception as e:
            err(f"Load failed: {e}")

    elif verb == '/help':
        show_help()

    else:
        suggestion = suggest_command(verb, KNOWN_COMMANDS)
        if suggestion:
            warn(f"Unknown command: {verb}  —  did you mean {c(C.CYAN, suggestion)}?")
        else:
            warn(f"Unknown command: {verb}  (try /help)")

    return None

# ══════════════════════════════════════════════════════════════════════
#  HELP & STATUS
# ══════════════════════════════════════════════════════════════════════

def show_help():
    rows = [
        ("/exit",                "Exit ARIA"),
        ("/clear",               "Clear conversation history"),
        ("/model <name>",        "Switch Ollama model"),
        ("/allow <feat> <bool>", "Toggle shell / network / write"),
        ("/reconfigure",         "Run first-time setup again"),
        ("/save",                "Save current session to file"),
        ("/load <file>",         "Load a saved session"),
        ("/ls [path]",           "List directory (shortcut)"),
        ("/pwd",                 "Print working directory"),
        ("/status",              "Show config & session stats"),
        ("/help",                "This help screen"),
    ]
    print()
    rule()
    print(f"  {c(C.CYAN+C.B, 'Commands')}")
    rule()
    for cmd, desc in rows:
        print(f"  {c(C.WHITE+C.B, cmd.ljust(26))} {c(C.GRAY, desc)}")
    rule()
    info("↑ / ↓ to navigate history   ·   Ctrl+C to interrupt   ·   Ctrl+D to exit")
    print()

def show_status():
    print()
    rule()
    print(f"  {c(C.CYAN+C.B, 'Session status')}")
    rule()
    print(f"  {c(C.GRAY,'model')}     {c(C.WHITE, cfg['model'])}")
    print(f"  {c(C.GRAY,'messages')}  {c(C.WHITE, str(_msg_count))}")
    print(f"  {c(C.GRAY,'uptime')}    {c(C.WHITE, uptime_str())}")
    print(f"  {c(C.GRAY,'cwd')}       {c(C.WHITE, os.getcwd())}")
    print(f"  {c(C.GRAY,'shell')}     {c(C.GREEN if cfg['allow_shell']   else C.GRAY, str(cfg['allow_shell']))}")
    print(f"  {c(C.GRAY,'network')}   {c(C.GREEN if cfg['allow_network'] else C.GRAY, str(cfg['allow_network']))}")
    print(f"  {c(C.GRAY,'write')}     {c(C.GREEN if cfg['allow_write']   else C.GRAY, str(cfg['allow_write']))}")
    rule()
    print()

def show_exit_stats():
    print()
    rule()
    print(f"  {c(C.CYAN, 'Session summary')}")
    print(f"  {c(C.GRAY, 'messages')}  {c(C.WHITE, str(_msg_count))}"
          f"   {c(C.GRAY, 'uptime')}  {c(C.WHITE, uptime_str())}"
          f"   {c(C.GRAY, 'model')}  {c(C.WHITE, cfg['model'])}")
    rule()
    print(f"  {c(C.GRAY+C.DIM, 'goodbye ✦')}")
    print()

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

messages_history = []

def main():
    global messages_history
    load_config()
    load_model_memory()
    setup_readline()

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
                    info("feeding result back…")
                    messages_history.append({
                        "role": "user",
                        "content": f"[Tool Result]\n{tool_result}\n\nProceed with the next step or provide final answer."
                    })
                else:
                    break

        except KeyboardInterrupt:
            print(f"\n  {c(C.GRAY+C.DIM, 'interrupted — /exit to quit')}")
        except EOFError:
            show_exit_stats()
            save_readline_history()
            break

if __name__ == "__main__":
    main()
