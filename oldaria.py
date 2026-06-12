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
import ast
import traceback
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

# ══════════════════════════════════════════════════════════════════════
#  VERSION & CONSTANTS
# ══════════════════════════════════════════════════════════════════════

VERSION        = "Build 3 Beta"
DEFAULT_MODEL  = "qwen3.5:9b"
OLLAMA_BASE    = "http://localhost:11434"
CHAT_URL       = f"{OLLAMA_BASE}/api/chat"
SESSION_DIR    = Path.home() / ".aria_sessions"
CONFIG_FILE    = Path.home() / ".aria_config.json"
MODEL_MEMORY_FILE = Path.home() / ".aria_model_config.json"
BACKUP_DIR     = Path.home() / ".aria_backups"
HISTORY_FILE   = Path.home() / ".aria_history"
MAX_TOOL_LOOPS = 30          # autonomous loop ceiling
MAX_OUTPUT_LEN = 12000
CODE_MAX_ITER  = 6           # max draft/test iterations in /code mode
SANDBOX_TIMEOUT = 15         # seconds for sandboxed test runs

SESSION_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════
#  COLORS
# ══════════════════════════════════════════════════════════════════════

class C:
    R       = "\033[0m";  B      = "\033[1m";  DIM    = "\033[2m"
    ITALIC  = "\033[3m";  GRAY   = "\033[90m"; RED    = "\033[91m"
    GREEN   = "\033[92m"; YELLOW = "\033[93m"; BLUE   = "\033[94m"
    MAGENTA = "\033[95m"; CYAN   = "\033[96m"; WHITE  = "\033[97m"
    DGREEN  = "\033[32m"; BG_RED = "\033[41m"; BG_YELLOW = "\033[43m"
    BG_GREEN = "\033[42m"; BG_BLUE = "\033[44m"

def c(color: str, text: str) -> str:
    return f"{color}{text}{C.R}"

# ══════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════

_start_time = time.time()
_msg_count  = 0

def get_terminal_width() -> int:
    try:    return os.get_terminal_size().columns
    except: return 80

def rule(char: str = "─", color: str = C.GRAY):
    w = min(get_terminal_width() - 4, 72)
    print(f"  {c(color, char * w)}")

def detect_workspace() -> str | None:
    info = []
    try:
        root   = subprocess.check_output(
            ['git','rev-parse','--show-toplevel'],
            stderr=subprocess.DEVNULL, text=True).strip()
        branch = subprocess.check_output(
            ['git','branch','--show-current'],
            stderr=subprocess.DEVNULL, text=True).strip()
        info.append(f"git:{Path(root).name}@{branch}")
    except Exception:
        pass
    if os.environ.get('VIRTUAL_ENV'):
        info.append(f"venv:{Path(os.environ['VIRTUAL_ENV']).name}")
    return ' · '.join(info) if info else None

def truncate_output(text: str, max_len: int = MAX_OUTPUT_LEN) -> str:
    if len(text) <= max_len:
        return text
    omitted = len(text) - max_len
    return text[:max_len] + f"\n{c(C.YELLOW,'…')} {c(C.GRAY, f'{omitted:,} chars omitted')}"

def uptime_str() -> str:
    s = int(time.time() - _start_time)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def suggest_command(bad_cmd: str, known_cmds: list[str]) -> str | None:
    matches = difflib.get_close_matches(bad_cmd, known_cmds, n=1, cutoff=0.6)
    return matches[0] if matches else None

def unified_diff(original: str, modified: str, filename: str = "file") -> str:
    """Return a compact unified diff string."""
    a = original.splitlines(keepends=True)
    b = modified.splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, fromfile=f"a/{filename}", tofile=f"b/{filename}"))
    return "".join(diff) if diff else "(no changes)"

# ══════════════════════════════════════════════════════════════════════
#  READLINE
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
    r'rm\s+(-rf?|--recursive)?\s*/',
    r'sudo\s+',
    r'\bdd\b',
    r'mkfs',
    r'>\s*/dev/sd[a-z]',
    r':\(\)\s*\{\s*:\s*;\s*\}\s*;',   # fork bomb
    r'chmod\s+777\s+/',
    r'curl[^|]*\|\s*(ba)?sh',
    r'wget[^|]*\|\s*(ba)?sh',
    r'\bkillall\b',
    r'\bpkill\b',
    r'kill\s+-9',
    r'nc\s+.*-[eCe]',
    r'python[23]?\s+-c\s+[\'"].*import\s+os',
    r'shred\b',
    r'wipefs\b',
]

def is_dangerous_command(cmd: str) -> bool:
    return any(re.search(p, cmd, re.IGNORECASE) for p in DANGEROUS_PATTERNS)

def danger_confirm(action_desc: str, details: str = "") -> bool:
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

DEFAULT_CFG: dict = {
    "model": DEFAULT_MODEL, "temperature": 0.4, "top_p": 0.95,
    "ctx": 64000, "max_tokens": -1, "stream": True, "search_n": 5,
    "allow_shell": False, "allow_network": True, "allow_write": True,
    "danger_confirm": True, "autonomous": True,
}
cfg: dict = dict(DEFAULT_CFG)

def load_config():
    global cfg
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
        except Exception:
            pass
    for k, v in DEFAULT_CFG.items():
        if k not in cfg:
            cfg[k] = v

def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    except Exception:
        pass

DEFAULT_MEMORY: dict = {
    "browser_cmd": "firefox", "editor_cmd": "code",
    "default_project": str(Path.home() / "projects"),
    "shell_enabled": False,
}
model_memory: dict = dict(DEFAULT_MEMORY)

def load_model_memory():
    global model_memory
    if MODEL_MEMORY_FILE.exists():
        try:
            model_memory.update(json.loads(MODEL_MEMORY_FILE.read_text(encoding='utf-8')))
        except Exception:
            pass
    for k, v in DEFAULT_MEMORY.items():
        if k not in model_memory:
            model_memory[k] = v

def save_model_memory():
    try:
        MODEL_MEMORY_FILE.write_text(json.dumps(model_memory, indent=2), encoding='utf-8')
    except Exception:
        pass

def first_time_setup():
    print(f"\n  {c(C.CYAN+C.B, '✨ First-time setup')}")
    rule()
    browser = input(f"  {c(C.YELLOW, 'Default browser')}  {c(C.GRAY,'[firefox]:')} ").strip()
    model_memory["browser_cmd"] = browser or "firefox"
    editor  = input(f"  {c(C.YELLOW, 'Default editor')}   {c(C.GRAY,'[code]:')} ").strip()
    model_memory["editor_cmd"]  = editor or "code"
    default_proj = DEFAULT_MEMORY["default_project"]
    proj    = input(f"  {c(C.YELLOW, 'Projects folder')} {c(C.GRAY, '['+default_proj+']:')}" " ").strip()
    model_memory["default_project"] = proj or DEFAULT_MEMORY["default_project"]
    Path(model_memory["default_project"]).mkdir(parents=True, exist_ok=True)
    enable  = input(f"  {c(C.YELLOW, 'Enable shell?')}    {c(C.GRAY,'(yes/no) [yes]:')} ").strip().lower()
    model_memory["shell_enabled"] = enable in ('yes', 'y', 'true', '')
    save_model_memory()
    cfg["allow_shell"] = model_memory["shell_enabled"]
    save_config()
    rule()
    ok(f"Saved. Shell: {'ON' if cfg['allow_shell'] else 'OFF'}")
    time.sleep(0.6)

# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════════

def system_prompt() -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cwd = os.getcwd()
    ws  = detect_workspace()
    shell_status = "ENABLED (dangerous blocked)" if cfg["allow_shell"] else "DISABLED"
    return f"""Your ARIA — Autonomous Reasoning Intelligent Agent. Be direct, precise, and efficient.

[ENVIRONMENT]
Time: {now} | CWD: {cwd} | Workspace: {ws or 'none'}
Security: Shell={shell_status} | Network={cfg['allow_network']} | Write={cfg['allow_write']}

[USER PREFERENCES]
Browser: `{model_memory['browser_cmd']}` | Editor: `{model_memory['editor_cmd']}` | Projects: `{model_memory['default_project']}`

[AUTONOMOUS MODE]
You operate in an autonomous loop. Use ONE tool tag per response, wait for its result,
then decide your next action. Chain as many steps as needed to fully complete the task.
Think step-by-step. Verify your work before declaring it done.

[DIRECT FILE EDITING]
Prefer surgical edits over full rewrites:
  <patch file="path">
  <<<FIND
  exact lines to find
  >>>REPLACE
  replacement lines
  >>>END
  </patch>

For new files or complete rewrites: <write file="path">content</write>
For regex substitution: <edit file="path" pattern="regex">replacement</edit>
For inspecting before editing: <read>file</read> or <grep>pattern file</grep>

[ALL TOOLS]
<search>query</search>
<read>file</read>
<write file="path">content</write>
<patch file="path"><<<FIND\nold\n>>>REPLACE\nnew\n>>>END</patch>
<summarize>file</summarize>
<http url="URL"/>
<execute>command</execute>
<pyeval>python code</pyeval>
<listdir>path</listdir>
<glob>pattern</glob>
<grep>pattern||file</grep>
<edit file="path" pattern="regex">replacement</edit>
<move src="from" dest="to"/>
<copy src="from" dest="to"/>
<cd>path</cd>
<test>python_file_or_command</test>

[RULES]
- ONE tool tag per message. Never nest tags.
- Always read a file before patching if you haven't seen it yet.
- After writing/patching code, run <test> to verify it works.
- If a test fails, diagnose and fix — do NOT give up before {CODE_MAX_ITER} attempts.
- When task is complete, summarise what was done clearly.
"""

def code_mode_prompt(task: str) -> str:
    return f"""You are ARIA in /code mode — focused software engineering agent.

TASK: {task}

WORKFLOW (follow this exactly):
1. PLAN   — think through the approach, list files to create/modify
2. DRAFT  — write or patch the code using tools
3. TEST   — run <test> on the result; analyse output carefully
4. FIX    — if tests fail, patch only the broken parts (not full rewrite)
5. REPEAT steps 3-4 until all tests pass (max {CODE_MAX_ITER} iterations)
6. REPORT — summarise what was built, how to use it, any caveats

[CONSTRAINTS]
- Prefer <patch> over <write> for existing files
- One tool per message; wait for result
- Write real, production-quality code with error handling
- Include docstrings, type hints where sensible
- After success, show a brief usage example

Begin with your PLAN.
"""

# ══════════════════════════════════════════════════════════════════════
#  SPINNER
# ══════════════════════════════════════════════════════════════════════

SPINNER_MSGS = {
    "thinking":  ["thinking…", "reasoning…", "planning…", "computing…"],
    "searching": ["searching…", "fetching results…", "looking it up…"],
    "running":   ["running…", "executing…", "spawning process…"],
    "reading":   ["reading…", "loading…", "parsing…"],
    "fetching":  ["fetching URL…", "downloading…", "requesting…"],
    "patching":  ["patching file…", "applying edit…", "modifying…"],
    "testing":   ["running tests…", "checking output…", "verifying…"],
    "coding":    ["drafting code…", "writing…", "generating…"],
}

class Spinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, kind: str = "thinking"):
        self.msgs  = SPINNER_MSGS.get(kind, SPINNER_MSGS["thinking"])
        self._stop = threading.Event()
        self._t    = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            msg = self.msgs[(i // 12) % len(self.msgs)]
            sys.stdout.write(f"\r  {c(C.CYAN, self.FRAMES[i % 10])} {c(C.GRAY, msg)}   ")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join(timeout=0.5)
        sys.stdout.write(f"\r{' ' * 64}\r")
        sys.stdout.flush()

# ══════════════════════════════════════════════════════════════════════
#  STATUS HELPERS
# ══════════════════════════════════════════════════════════════════════

def ok(m: str):   print(f"  {c(C.GREEN,  '✓')}  {m}")
def warn(m: str): print(f"  {c(C.YELLOW, '⚠')}  {m}")
def err(m: str):  print(f"  {c(C.RED,    '✗')}  {m}")
def info(m: str): print(f"  {c(C.BLUE,   '·')}  {c(C.GRAY, m)}")

def tool_header(icon: str, tag: str, detail: str = ""):
    trunc = detail[:72] + ("…" if len(detail) > 72 else "")
    print(f"\n  {c(C.MAGENTA, icon)} {c(C.GRAY+C.DIM, tag+':')} {c(C.WHITE, trunc)}")

def tool_result_box(result: str, success: bool = True, max_lines: int = 35):
    color  = C.DGREEN if success else C.RED
    lines  = result.splitlines()
    prefix = f"  {c(color, '│')} "
    for ln in lines[:max_lines]:
        print(f"{prefix}{c(C.GRAY, ln)}")
    if len(lines) > max_lines:
        print(f"{prefix}{c(C.GRAY+C.DIM, f'  … {len(lines)-max_lines} more lines')}")

def diff_box(diff_text: str):
    """Pretty-print a unified diff."""
    print()
    for line in diff_text.splitlines()[:60]:
        if line.startswith('+') and not line.startswith('+++'):
            print(f"  {c(C.DGREEN, line)}")
        elif line.startswith('-') and not line.startswith('---'):
            print(f"  {c(C.RED, line)}")
        elif line.startswith('@@'):
            print(f"  {c(C.CYAN, line)}")
        else:
            print(f"  {c(C.GRAY, line)}")

# ══════════════════════════════════════════════════════════════════════
#  BANNER & PROMPT
# ══════════════════════════════════════════════════════════════════════

def check_ollama() -> tuple[bool | None, list]:
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        model_ok = (cfg["model"] in models or
                    any(cfg["model"].split(":")[0] in m for m in models))
        return model_ok, models
    except Exception:
        return None, []

def banner():
    print()
    print(f"  {c(C.CYAN+C.B, 'ARIA')} {c(C.GRAY, VERSION)}  "
          f"{c(C.DIM+C.GRAY, '·  Autonomous Reasoning Agent')}")
    rule()

    online, models = check_ollama()
    if online is None:
        model_status = c(C.RED, '✗ ollama offline')
    elif not online:
        model_status = c(C.YELLOW,
                         f'⚠ model not found  (available: {", ".join(models[:3])})')
    else:
        model_status = c(C.GREEN, '✓ ready')

    shell_col = C.GREEN if cfg['allow_shell']   else C.GRAY
    net_col   = C.GREEN if cfg['allow_network'] else C.GRAY
    write_col = C.GREEN if cfg['allow_write']   else C.GRAY
    auto_col  = C.GREEN if cfg['autonomous']    else C.GRAY

    print(f"  {c(C.GRAY,'model')}    {c(C.WHITE, cfg['model'])}  {model_status}")
    print(f"  {c(C.GRAY,'access')}   shell {c(shell_col,'●')}  network {c(net_col,'●')}  "
          f"write {c(write_col,'●')}  auto {c(auto_col,'●')}")
    rule()
    info("type /help for commands  ·  /code <task> for coding mode  ·  ↑↓ history")
    print()

def prompt_line() -> str:
    ws     = detect_workspace()
    ws_str = f" {c(C.DGREEN, ws)}" if ws else ""
    cwd    = c(C.GRAY+C.DIM, Path.cwd().name)
    count  = c(C.GRAY+C.DIM, f"#{_msg_count+1}")
    return (f"\n  {count} {cwd}{ws_str}\n"
            f"  {c(C.GREEN+C.B,'▶')} {c(C.WHITE+C.B,'you')}: ")

# ══════════════════════════════════════════════════════════════════════
#  TOOL: PATCH (surgical multi-line find/replace)
# ══════════════════════════════════════════════════════════════════════

def apply_patch(filepath: str, patch_body: str) -> tuple[str, bool]:
    """
    Parse a patch block:
        <<<FIND
        ...exact lines...
        >>>REPLACE
        ...new lines...
        >>>END
    Multiple hunks separated by a blank line are supported.
    """
    if not cfg["allow_write"]:
        return "File write disabled", False

    p = Path(filepath).expanduser()
    if not p.exists():
        return f"Error: {filepath} not found", False

    try:
        original = p.read_text(encoding='utf-8')
    except Exception as e:
        return f"Read error: {e}", False

    # Split into hunks (separated by optional blank lines between >>>END and <<<FIND)
    hunk_pattern = re.compile(
        r'<<<FIND\s*\n(.*?)>>>REPLACE\s*\n(.*?)>>>END',
        re.DOTALL
    )
    hunks = hunk_pattern.findall(patch_body)

    if not hunks:
        return ("Patch parse error: expected <<<FIND … >>>REPLACE … >>>END blocks.\n"
                f"Got:\n{patch_body[:300]}"), False

    content = original
    applied = 0
    report  = []

    for find_text, replace_text in hunks:
        # Strip one trailing newline that the tag format adds
        find_text    = find_text.rstrip('\n')
        replace_text = replace_text.rstrip('\n')

        if find_text not in content:
            # Try stripping trailing whitespace per line (common editor artefact)
            stripped_content = "\n".join(l.rstrip() for l in content.splitlines())
            stripped_find    = "\n".join(l.rstrip() for l in find_text.splitlines())
            if stripped_find in stripped_content:
                # Rebuild with stripped version
                content = stripped_content
                find_text = stripped_find
            else:
                report.append(f"⚠ hunk not found:\n  {repr(find_text[:80])}")
                continue

        content  = content.replace(find_text, replace_text, 1)
        applied += 1
        report.append(f"✓ applied hunk ({len(find_text.splitlines())} → "
                       f"{len(replace_text.splitlines())} lines)")

    if applied == 0:
        return "No hunks applied — find text not found in file.", False

    # Backup & write
    backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
    try:
        shutil.copy(p, backup)
        p.write_text(content, encoding='utf-8')
    except Exception as e:
        return f"Write error: {e}", False

    diff = unified_diff(original, content, p.name)
    return (f"Patched {filepath} — {applied}/{len(hunks)} hunk(s)\n"
            f"Backup: {backup.name}\n\n{diff}"), True

# ══════════════════════════════════════════════════════════════════════
#  TOOL: TEST (run a file or command and capture result)
# ══════════════════════════════════════════════════════════════════════

def run_test(target: str) -> tuple[str, bool]:
    """
    Run a test target:
      - if it's a .py file → python <file>
      - if it's a command  → run as shell command (no dangerous check for test runner)
    Captures stdout + stderr, returns (output, passed).
    """
    target = target.strip()
    if not target:
        return "No test target specified", False

    p = Path(target)
    if p.suffix == '.py' and p.exists():
        cmd = [sys.executable, str(p)]
    else:
        # treat as shell snippet
        cmd = target

    spin = Spinner("testing").start()
    try:
        if isinstance(cmd, list):
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=SANDBOX_TIMEOUT, cwd=os.getcwd())
        else:
            res = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=SANDBOX_TIMEOUT, cwd=os.getcwd())
        spin.stop()
        passed  = res.returncode == 0
        output  = (res.stdout + res.stderr).strip() or "(no output)"
        prefix  = c(C.GREEN if passed else C.RED,
                    f"exit {res.returncode} — {'PASS' if passed else 'FAIL'}")
        return f"{prefix}\n{truncate_output(output)}", passed
    except subprocess.TimeoutExpired:
        spin.stop()
        return f"Test timed out after {SANDBOX_TIMEOUT}s", False
    except Exception as e:
        spin.stop()
        return f"Test error: {e}", False

# ══════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS  (unchanged from Build 2 + improvements)
# ══════════════════════════════════════════════════════════════════════

def change_directory(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser().resolve()
    if not p.exists():  return f"Error: {path} does not exist", False
    if not p.is_dir():  return f"Error: {path} is not a directory", False
    try:
        os.chdir(p)
        return f"Changed directory to {p}", True
    except Exception as e:
        return f"cd failed: {e}", False

def search_web(query: str) -> tuple[str, bool]:
    if not cfg["allow_network"]:
        return "Network disabled", False
    try:
        import urllib.parse, html as _html
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.post(url, data={"q": query}, timeout=15, headers=headers)
        resp.raise_for_status()

        results = []
        pattern = r'<a class="result__url" href="([^"]+)"[^>]*>(.*?)</a>'
        for m in re.finditer(pattern, resp.text, re.IGNORECASE | re.DOTALL):
            link  = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            title = _html.unescape(title)
            if 'uddg=' in link:
                qs   = urllib.parse.parse_qs(
                    urllib.parse.urlparse("http:" + link).query)
                link = qs.get('uddg', [link])[0]
            results.append(f"{title}\n  {link}")
            if len(results) >= cfg.get("search_n", 5):
                break

        out = "\n\n".join(results) if results else "No results found."
        return out, bool(results)
    except Exception as e:
        return f"Search error: {e}", False

def http_fetch(url: str) -> tuple[str, bool]:
    if not cfg["allow_network"]:
        return "Network disabled", False
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ARIA/3.0"})
        resp.raise_for_status()
        ct = resp.headers.get('content-type', '')
        if 'application/json' in ct:
            try:
                out = json.dumps(resp.json(), indent=2)
            except ValueError:
                out = resp.text
        else:
            out = resp.text
        return truncate_output(out), True
    except Exception as e:
        return f"HTTP error: {e}", False

def summarize_file(filepath: str) -> tuple[str, bool]:
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    if p.is_dir():     return f"Error: {filepath} is a directory", False
    stat = p.stat()
    try:
        text  = p.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        words = sum(len(l.split()) for l in lines)
    except Exception:
        text, lines, words = "", [], -1
    preview = ("\n\nFirst 25 lines:\n" + "\n".join(lines[:25])) if lines else ""
    out = (f"File: {p.name}\nSize: {stat.st_size:,} bytes\n"
           f"Lines: {len(lines):,}\nWords: {words:,}{preview}")
    return out, True

def python_eval(code: str) -> tuple[str, bool]:
    # Basic safety: reject obvious escape attempts
    forbidden = ['__import__', 'open(', 'subprocess', 'os.system', 'eval(', 'exec(']
    for token in forbidden:
        if token in code:
            return f"Blocked: forbidden token '{token}' in pyeval", False
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        globs: dict = {"__name__": "__aria_pyeval__"}
        exec(compile(code, "<pyeval>", "exec"), globs)  # noqa: S102
        out     = sys.stdout.getvalue()
        err_out = sys.stderr.getvalue()
        result  = truncate_output(
            out + (f"\n[stderr]:\n{err_out}" if err_out else "")
        ) or "(no output)"
        return result, True
    except Exception as e:
        return f"Python error: {type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}", False
    finally:
        sys.stdout, sys.stderr = old_out, old_err

def list_directory(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser().resolve()
    if not p.exists(): return f"Error: {path} does not exist", False
    if not p.is_dir(): return f"Error: {path} is not a directory", False
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = []
        for i in items[:120]:
            if i.is_file():
                s    = i.stat().st_size
                size = f"  {c(C.GRAY+C.DIM, f'{s:,}B')}" if s < 1024 else \
                       f"  {c(C.GRAY+C.DIM, f'{s//1024}KB')}"
                icon = "📄"
            else:
                size = ""
                icon = "📁"
            lines.append(f"{icon} {i.name}{size}")
        if len(items) > 120:
            lines.append(f"… and {len(items)-120} more")
        return "\n".join(lines) if lines else "(empty)", True
    except Exception as e:
        return f"Listdir error: {e}", False

def glob_files(pattern: str) -> tuple[str, bool]:
    try:
        matches = sorted(Path.cwd().glob(pattern))
        out = "\n".join(str(p) for p in matches[:200]) or "No matches"
        return out, bool(matches)
    except Exception as e:
        return f"Glob error: {e}", False

def grep_file(args_str: str) -> tuple[str, bool]:
    """Accept 'pattern||file' or 'pattern file' splitting."""
    if '||' in args_str:
        parts = args_str.split('||', 1)
    else:
        parts = args_str.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: <grep>pattern||file</grep>", False
    pattern, filepath = parts[0].strip(), parts[1].strip()
    p = Path(filepath).expanduser()
    if not p.exists():
        return f"Error: {filepath} not found", False
    try:
        lines   = p.read_text(encoding='utf-8', errors='replace').splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append(f"{c(C.GRAY+C.DIM, str(i).rjust(4))}  {line.strip()[:200]}")
                if len(results) >= 60:
                    break
        out = "\n".join(results) if results else "No matches"
        return out, bool(results)
    except Exception as e:
        return f"Grep error: {e}", False

def edit_file(filepath: str, pattern: str, replacement: str) -> tuple[str, bool]:
    if not cfg["allow_write"]:
        return "File write disabled", False
    p = Path(filepath).expanduser()
    if not p.exists():
        return f"Error: {filepath} not found", False
    try:
        original = p.read_text(encoding='utf-8')
        new_content, count = re.subn(pattern, replacement, original)
        if count == 0:
            return "No matches found for pattern", False
        backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
        shutil.copy(p, backup)
        p.write_text(new_content, encoding='utf-8')
        diff = unified_diff(original, new_content, p.name)
        return (f"Replaced {count} occurrence(s) in {filepath}\n"
                f"Backup: {backup.name}\n\n{diff}"), True
    except re.error as e:
        return f"Regex error: {e}", False
    except Exception as e:
        return f"Edit failed: {e}", False

def move_file(src: str, dst: str) -> tuple[str, bool]:
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists():
        return f"Error: source {src} not found", False
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return f"Moved {src} → {dst}", True
    except Exception as e:
        return f"Move failed: {e}", False

def copy_file(src: str, dst: str) -> tuple[str, bool]:
    src_p, dst_p = Path(src).expanduser(), Path(dst).expanduser()
    if not src_p.exists():
        return f"Error: source {src} not found", False
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)
        return f"Copied {src} → {dst}", True
    except Exception as e:
        return f"Copy failed: {e}", False

def run_shell_command(cmd: str) -> tuple[str, bool]:
    if not cfg["allow_shell"]:
        return "Shell disabled — enable with /allow shell true", False
    if not cmd.strip():
        return "Empty command", False
    if is_dangerous_command(cmd):
        if cfg["danger_confirm"]:
            if not danger_confirm("Dangerous command detected", cmd):
                return "User denied execution", False
        else:
            return "Command blocked (dangerous pattern matched)", False
    spin = Spinner("running").start()
    try:
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=os.getcwd())
        spin.stop()
        out     = (res.stdout + res.stderr).strip() or "(no output)"
        ok_flag = res.returncode == 0
        prefix  = c(C.GREEN if ok_flag else C.RED, f"exit {res.returncode}")
        return f"{prefix}\n{truncate_output(out)}", ok_flag
    except subprocess.TimeoutExpired:
        spin.stop()
        return "Command timed out after 60s", False
    except Exception as e:
        spin.stop()
        return f"Execution failed: {e}", False

def write_file(filepath: str, content: str) -> tuple[str, bool]:
    if not cfg["allow_write"]:
        return "File write disabled", False
    p = Path(filepath).expanduser()
    try:
        if p.exists():
            backup = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
            shutil.copy(p, backup)
            backup_msg = f"\nBackup: {backup.name}"
        else:
            backup_msg = ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return f"Wrote {len(content):,} chars ({content.count(chr(10))+1} lines) to {filepath}{backup_msg}", True
    except Exception as e:
        return f"Write failed: {e}", False

def read_file(filepath: str) -> tuple[str, bool]:
    p = Path(filepath).expanduser()
    if not p.exists(): return f"Error: {filepath} not found", False
    if p.is_dir():     return f"Error: {filepath} is a directory", False
    try:
        content = p.read_text(encoding='utf-8', errors='replace')
        # Show line numbers for code files
        suffix = p.suffix.lower()
        code_exts = {'.py','.js','.ts','.c','.cpp','.h','.rs','.go',
                     '.java','.sh','.yaml','.json','.toml','.html','.css'}
        if suffix in code_exts:
            numbered = "\n".join(
                f"{str(i).rjust(4)} │ {l}"
                for i, l in enumerate(content.splitlines(), 1)
            )
            return f"[{filepath}]\n{truncate_output(numbered)}", True
        return f"[{filepath}]\n{truncate_output(content)}", True
    except Exception as e:
        return f"Read error: {e}", False

# ══════════════════════════════════════════════════════════════════════
#  TOOL PROCESSOR
# ══════════════════════════════════════════════════════════════════════

def process_tools(text: str) -> tuple[bool, str, str]:
    """
    Returns (tool_used, result_text, tool_name).
    """
    # ── cd ──────────────────────────────────────────────────────────
    m = re.search(r'<cd>(.*?)</cd>', text, re.IGNORECASE | re.DOTALL)
    if m:
        path = m.group(1).strip()
        tool_header("📂", "cd", path)
        result, ok_f = change_directory(path)
        tool_result_box(result, ok_f)
        return True, result, "cd"

    # ── patch ────────────────────────────────────────────────────────
    m = re.search(r'<patch\s+file=["\']([^"\']+)["\'][^>]*>(.*?)</patch>',
                  text, re.IGNORECASE | re.DOTALL)
    if m:
        filepath, patch_body = m.group(1).strip(), m.group(2)
        tool_header("🩹", "patch", filepath)
        result, ok_f = apply_patch(filepath, patch_body)
        tool_result_box(result, ok_f)
        if ok_f:
            # Show diff inline
            diff_lines = [l for l in result.splitlines()
                          if l.startswith(('+', '-', '@@', '---', '+++'))]
            if diff_lines:
                diff_box("\n".join(diff_lines[:40]))
        return True, result, "patch"

    # ── test ─────────────────────────────────────────────────────────
    m = re.search(r'<test>(.*?)</test>', text, re.IGNORECASE | re.DOTALL)
    if m:
        target = m.group(1).strip()
        tool_header("🧪", "test", target)
        result, ok_f = run_test(target)
        tool_result_box(result, ok_f)
        return True, result, "test"

    # ── standard tools ───────────────────────────────────────────────
    TOOLS = [
        ('execute',   run_shell_command,
         r'<execute>(.*?)</execute>',
         lambda m: m.group(1).strip(), "⚡", "running"),

        ('write',     write_file,
         r'<write\s+file=["\']([^"\']+)["\'][^>]*>(.*?)</write>',
         lambda m: (m.group(1), m.group(2)), "💾", "reading"),

        ('read',      read_file,
         r'<read>(.*?)</read>',
         lambda m: m.group(1).strip(), "📖", "reading"),

        ('summarize', summarize_file,
         r'<summarize>(.*?)</summarize>',
         lambda m: m.group(1).strip(), "📋", "reading"),

        ('search',    search_web,
         r'<search>(.*?)</search>',
         lambda m: m.group(1).strip(), "🔍", "searching"),

        ('listdir',   list_directory,
         r'<listdir>(.*?)</listdir>',
         lambda m: m.group(1).strip(), "📁", "reading"),

        ('glob',      glob_files,
         r'<glob>(.*?)</glob>',
         lambda m: m.group(1).strip(), "🔎", "reading"),

        ('grep',      grep_file,
         r'<grep>(.*?)</grep>',
         lambda m: m.group(1).strip(), "🔬", "reading"),

        ('pyeval',    python_eval,
         r'<pyeval>(.*?)</pyeval>',
         lambda m: m.group(1).strip(), "🐍", "thinking"),
    ]

    for tag, handler, pattern, extractor, icon, spin_kind in TOOLS:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            detail = (m.group(1) or "").strip()[:80]
            tool_header(icon, tag, detail)
            args   = extractor(m)
            result, ok_f = (handler(*args) if isinstance(args, tuple)
                             else handler(args))
            tool_result_box(result, ok_f)
            return True, result, tag

    # ── http ─────────────────────────────────────────────────────────
    m = re.search(r'<http\s+url=["\']?([^"\'>\s]+)["\']?\s*/?>', text, re.IGNORECASE)
    if not m:
        m = re.search(r'<http>(.*?)</http>', text, re.IGNORECASE | re.DOTALL)
    if m:
        url = m.group(1).strip()
        tool_header("🌐", "http", url)
        result, ok_f = http_fetch(url)
        tool_result_box(result, ok_f)
        return True, result, "http"

    # ── edit ─────────────────────────────────────────────────────────
    m = re.search(
        r'<edit\s+file=["\']([^"\']+)["\']\s+pattern=["\']([^"\']+)["\'][^>]*>'
        r'(.*?)</edit>',
        text, re.IGNORECASE | re.DOTALL)
    if m:
        filepath, pattern, replacement = m.group(1), m.group(2), m.group(3)
        tool_header("✏️", "edit", filepath)
        result, ok_f = edit_file(filepath, pattern, replacement)
        tool_result_box(result, ok_f)
        return True, result, "edit"

    # ── move / copy ───────────────────────────────────────────────────
    for tag in ('move', 'copy'):
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
                return True, result, tag

    return False, "", ""

# ══════════════════════════════════════════════════════════════════════
#  INFERENCE
# ══════════════════════════════════════════════════════════════════════

def run_inference(messages: list[dict]) -> str | None:
    global _msg_count
    payload: dict = {
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
            resp = requests.post(CHAT_URL, json=payload, stream=True, timeout=180)
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line:
                    data  = json.loads(line)
                    chunk = data.get("message", {}).get("content", "")
                    full += chunk
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    if data.get("done"):
                        elapsed = time.time() - t0
                        print(f"\n  {c(C.GRAY+C.DIM, f'{elapsed:.1f}s')}")
                        break
            _msg_count += 1
            return full
        except requests.HTTPError as e:
            err(f"Ollama HTTP error: {e}")
            return None
        except Exception as e:
            err(f"Ollama error: {e}")
            return None
    else:
        spin = Spinner("thinking").start()
        try:
            resp = requests.post(CHAT_URL, json=payload, timeout=180)
            spin.stop()
            resp.raise_for_status()
            reply = resp.json().get("message", {}).get("content", "")
            print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: {reply}\n")
            _msg_count += 1
            return reply
        except Exception as e:
            spin.stop()
            err(f"Ollama error: {e}")
            return None

# ══════════════════════════════════════════════════════════════════════
#  AUTONOMOUS LOOP
# ══════════════════════════════════════════════════════════════════════

def autonomous_loop(messages: list[dict], max_loops: int = MAX_TOOL_LOOPS) -> str:
    """
    Run the inference + tool loop until the model stops using tools
    or max_loops is reached.  Returns the final reply text.
    """
    final_reply = ""
    consecutive_no_tool = 0

    for loop_idx in range(max_loops):
        reply = run_inference(messages)
        if not reply:
            break

        messages.append({"role": "assistant", "content": reply})
        final_reply = reply

        tool_used, tool_result, tool_name = process_tools(reply)

        if tool_used:
            consecutive_no_tool = 0
            info(f"loop {loop_idx+1}/{max_loops} · tool={tool_name} · feeding result…")
            messages.append({
                "role": "user",
                "content": (
                    f"[Tool: {tool_name}]\n{tool_result}\n\n"
                    "Continue. Use the next tool if needed, "
                    "or give your final answer if the task is complete."
                )
            })
        else:
            consecutive_no_tool += 1
            # Model gave two plain responses in a row → done
            if consecutive_no_tool >= 2:
                break
            # One plain response: ask it to confirm it's done
            messages.append({
                "role": "user",
                "content": "Is the task fully complete? If yes, confirm. If not, continue."
            })

    else:
        warn(f"Reached tool-loop limit ({max_loops}). Stopping.")

    return final_reply

# ══════════════════════════════════════════════════════════════════════
#  /CODE MODE
# ══════════════════════════════════════════════════════════════════════

def code_mode(task: str):
    """
    Dedicated coding agent loop with draft→test→fix cycles.
    Tracks test results and iteration count; stops early on success.
    """
    print()
    rule("═", C.CYAN)
    print(f"  {c(C.CYAN+C.B, '⌨  CODE MODE')}  {c(C.WHITE, task)}")
    rule("═", C.CYAN)
    info(f"max iterations: {CODE_MAX_ITER}  ·  test timeout: {SANDBOX_TIMEOUT}s")
    print()

    messages: list[dict] = [
        {"role": "system",  "content": code_mode_prompt(task)},
        {"role": "user",    "content": task},
    ]

    iteration      = 0
    last_test_pass = False
    final_reply    = ""

    for iteration in range(CODE_MAX_ITER * MAX_TOOL_LOOPS):
        reply = run_inference(messages)
        if not reply:
            break

        messages.append({"role": "assistant", "content": reply})
        final_reply = reply

        tool_used, tool_result, tool_name = process_tools(reply)

        if tool_used:
            # Track test outcomes specially
            if tool_name == "test":
                passed = "PASS" in tool_result or "exit 0" in tool_result
                last_test_pass = passed
                iter_label = c(C.GREEN if passed else C.RED,
                               "PASS" if passed else "FAIL")
                print(f"\n  {c(C.CYAN+C.DIM, f'[iteration {iteration+1}]')} "
                      f"test {iter_label}")

                if passed:
                    # Give the model one more turn to wrap up
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[Test Result — PASS]\n{tool_result}\n\n"
                            "All tests pass. Please provide your REPORT: "
                            "what was built, how to use it, any caveats."
                        )
                    })
                    # Get the report
                    report = run_inference(messages)
                    if report:
                        messages.append({"role": "assistant", "content": report})
                        final_reply = report
                    break
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[Test Result — FAIL]\n{tool_result}\n\n"
                            f"Tests failed (iteration {iteration+1}/{CODE_MAX_ITER}). "
                            "Diagnose the error, then patch ONLY the broken part. "
                            "Do NOT rewrite the whole file unless absolutely necessary."
                        )
                    })
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Tool: {tool_name}]\n{tool_result}\n\n"
                        "Continue with the next step."
                    )
                })
        else:
            # Model produced a plain response — check if it's done
            done_signals = ["report", "complete", "done", "finished", "summary",
                            "usage", "example"]
            if any(sig in reply.lower() for sig in done_signals):
                break
            # Otherwise nudge it to continue
            messages.append({
                "role": "user",
                "content": "Continue. Use a tool for the next step."
            })

    # Summary
    print()
    rule("─", C.CYAN)
    status_str = c(C.GREEN, "✓ tests passed") if last_test_pass \
                 else c(C.YELLOW, f"⚠ completed after {iteration+1} iterations")
    print(f"  {c(C.CYAN+C.B, '⌨  CODE MODE DONE')}  {status_str}")
    rule("─", C.CYAN)
    print()

# ══════════════════════════════════════════════════════════════════════
#  KNOWN COMMANDS
# ══════════════════════════════════════════════════════════════════════

KNOWN_COMMANDS = [
    '/exit', '/clear', '/model', '/allow', '/reconfigure',
    '/save', '/load', '/help', '/ls', '/pwd', '/status',
    '/code', '/auto',
]

# ══════════════════════════════════════════════════════════════════════
#  COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════

def handle_command(cmd: str) -> str | None:
    parts = cmd.split()
    verb  = parts[0].lower()

    if verb == '/exit':
        show_exit_stats()
        save_readline_history()
        sys.exit(0)

    elif verb == '/clear':
        return "clear"

    elif verb == '/code':
        task = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not task:
            task = input(f"  {c(C.YELLOW, 'Task description:')} ").strip()
        if task:
            code_mode(task)
        else:
            warn("No task provided.")

    elif verb == '/auto':
        if len(parts) > 1:
            val = parts[1].lower() in ('true', '1', 'yes', 'on')
            cfg['autonomous'] = val
            save_config()
            ok(f"Autonomous mode {'ON' if val else 'OFF'}")
        else:
            ok(f"Autonomous mode: {'ON' if cfg['autonomous'] else 'OFF'}")

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
                    warn("Shell enabled — dangerous patterns are still blocked.")
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
            "cwd":       os.getcwd(),
        }
        try:
            session_file.write_text(json.dumps(session_data, indent=2), encoding='utf-8')
            ok(f"Session saved → {session_file.name}")
        except Exception as e:
            err(f"Save failed: {e}")

    elif verb == '/load':
        if len(parts) < 2:
            warn("Usage: /load <file>")
            return None
        sf = Path(parts[1])
        if not sf.exists():
            sf = SESSION_DIR / parts[1]
        if not sf.exists():
            err("Session file not found")
            return None
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
            warn(f"Unknown: {verb}  — did you mean {c(C.CYAN, suggestion)}?")
        else:
            warn(f"Unknown command: {verb}  (try /help)")

    return None

# ══════════════════════════════════════════════════════════════════════
#  HELP & STATUS
# ══════════════════════════════════════════════════════════════════════

def show_help():
    rows = [
        ("/exit",                  "Exit ARIA"),
        ("/clear",                 "Clear conversation history"),
        ("/code [task]",           "Enter focused coding mode"),
        ("/auto [true|false]",     "Toggle autonomous loop"),
        ("/model <name>",          "Switch Ollama model"),
        ("/allow <feat> <bool>",   "Toggle shell / network / write"),
        ("/reconfigure",           "Run first-time setup again"),
        ("/save",                  "Save current session"),
        ("/load <file>",           "Load a saved session"),
        ("/ls [path]",             "List directory"),
        ("/pwd",                   "Print working directory"),
        ("/status",                "Config & session stats"),
        ("/help",                  "This screen"),
    ]
    print()
    rule()
    print(f"  {c(C.CYAN+C.B, 'Commands')}")
    rule()
    for cmd, desc in rows:
        print(f"  {c(C.WHITE+C.B, cmd.ljust(28))} {c(C.GRAY, desc)}")
    rule()
    print(f"  {c(C.GRAY+C.DIM, 'Tools available to ARIA:')}")
    tools = ["search","read","write","patch","edit","execute","pyeval",
             "listdir","glob","grep","http","move","copy","cd","test"]
    print(f"  {c(C.GRAY, '  '.join(tools))}")
    rule()
    info("↑/↓ history  ·  Ctrl+C interrupt  ·  Ctrl+D exit")
    print()

def show_status():
    print()
    rule()
    print(f"  {c(C.CYAN+C.B, 'Session status')}")
    rule()
    flags = {
        'model':     cfg['model'],
        'messages':  str(_msg_count),
        'uptime':    uptime_str(),
        'cwd':       os.getcwd(),
        'shell':     str(cfg['allow_shell']),
        'network':   str(cfg['allow_network']),
        'write':     str(cfg['allow_write']),
        'auto loop': str(cfg['autonomous']),
    }
    for k, v in flags.items():
        is_true = v.lower() in ('true',)
        vc = C.GREEN if is_true else (C.GRAY if v.lower() == 'false' else C.WHITE)
        print(f"  {c(C.GRAY, k.ljust(12))} {c(vc, v)}")
    rule()
    print()

def show_exit_stats():
    print()
    rule()
    print(f"  {c(C.CYAN, 'Session summary')}")
    print(f"  {c(C.GRAY,'messages')} {c(C.WHITE, str(_msg_count))}"
          f"   {c(C.GRAY,'uptime')} {c(C.WHITE, uptime_str())}"
          f"   {c(C.GRAY,'model')} {c(C.WHITE, cfg['model'])}")
    rule()
    print(f"  {c(C.GRAY+C.DIM, 'goodbye ✦')}")
    print()

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

messages_history: list[dict] = []

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

            # ── slash commands ─────────────────────────────────────
            if user_input.startswith('/'):
                cmd_res = handle_command(user_input)
                if cmd_res == "clear":
                    messages_history = [
                        {"role": "system", "content": system_prompt()}
                    ]
                    ok("Conversation cleared.")
                elif cmd_res == "reload":
                    messages_history[0] = {
                        "role": "system", "content": system_prompt()
                    }
                    ok("Preferences reloaded.")
                continue

            # ── normal chat / autonomous loop ──────────────────────
            messages_history.append({"role": "user", "content": user_input})

            if cfg.get("autonomous", True):
                autonomous_loop(messages_history)
            else:
                # single-shot (old behaviour)
                reply = run_inference(messages_history)
                if reply:
                    messages_history.append(
                        {"role": "assistant", "content": reply}
                    )
                    tool_used, tool_result, tool_name = process_tools(reply)
                    if tool_used:
                        info("feeding result back…")
                        messages_history.append({
                            "role": "user",
                            "content": (
                                f"[Tool: {tool_name}]\n{tool_result}\n\n"
                                "Proceed or give final answer."
                            )
                        })

        except KeyboardInterrupt:
            print(f"\n  {c(C.GRAY+C.DIM, 'interrupted — /exit to quit')}")
        except EOFError:
            show_exit_stats()
            save_readline_history()
            break

if __name__ == "__main__":
    main()
