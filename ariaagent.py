#!/usr/bin/env python3
"""
ARIA — Autonomous Reasoning Intelligent Agent  ·  Build 6
Smarter · Faster · Cleaner · Code memory · 9b-tuned prompts
"""

import ast, datetime, difflib, hashlib, io, json, os, re, shutil
import signal, subprocess, sys, tempfile, textwrap, threading, time
import traceback, zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import readline
except ImportError:
    readline = None

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ── Constants ──────────────────────────────────────────────────────────
VERSION        = "Build 6"
DEFAULT_MODEL  = "qwen3.5:9b"
OLLAMA_BASE    = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
CHAT_URL       = f"{OLLAMA_BASE}/api/chat"
TAGS_URL       = f"{OLLAMA_BASE}/api/tags"
HOME           = Path.home()
SESSION_DIR    = HOME / ".aria_sessions"
CONFIG_FILE    = HOME / ".aria_config.json"
MEMORY_FILE    = HOME / ".aria_memory.json"
BACKUP_DIR     = HOME / ".aria_backups"
HISTORY_FILE   = HOME / ".aria_history"
NOTES_FILE     = HOME / ".aria_notes.json"
CODE_MEM_FILE  = HOME / ".aria_code_memory.json"   # NEW: /code mode memory
PLUGINS_DIR    = HOME / ".aria_plugins"
CRASH_FILE     = HOME / ".aria_crash.json"

MAX_TOOL_LOOPS  = 32
MAX_OUTPUT_LEN  = 14000
CODE_MAX_ITER   = 10
SANDBOX_TIMEOUT = 25
CTX_COMPRESS_AT = 24
CTX_KEEP_RECENT = 6
MAX_RETRIES     = 2

for _d in (SESSION_DIR, BACKUP_DIR, PLUGINS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Colors ─────────────────────────────────────────────────────────────
class C:
    R="\033[0m"; B="\033[1m"; DIM="\033[2m"; IT="\033[3m"
    GRAY="\033[90m"; RED="\033[91m"; GREEN="\033[92m"; YEL="\033[93m"
    BLUE="\033[94m"; MAG="\033[95m"; CYAN="\033[96m"; WHT="\033[97m"
    DGRN="\033[32m"; DGRAY="\033[2;37m"

def c(color: str, text: str) -> str: return f"{color}{text}{C.R}"
def strip_ansi(s: str) -> str: return re.sub(r'\033\[[0-9;]*m', '', s)

# ── Runtime state ──────────────────────────────────────────────────────
_start_time  = time.time()
_msg_count   = 0
_tool_counts: dict[str, int] = defaultdict(int)
_token_total = 0
_pystate: dict[str, Any] = {}
messages_history: list[dict] = []
_last_tool_result = ""

# ── Config ─────────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "model": DEFAULT_MODEL, "temperature": 0.3, "top_p": 0.95,
    "ctx": 65536, "max_tokens": -1, "stream": True,
    "search_n": 5, "allow_shell": False, "allow_network": True,
    "allow_write": True, "danger_confirm": True, "autonomous": True,
    "auto_compress": True, "show_diffs": True, "retry_on_fail": True,
    "browser_cmd": "firefox", "editor_cmd": "code",
    "projects_dir": str(HOME / "projects"), "fallback_model": "",
}
cfg: dict = dict(DEFAULTS)

def load_config():
    global cfg
    if CONFIG_FILE.exists():
        try: cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception: pass
    for k, v in DEFAULTS.items(): cfg.setdefault(k, v)

def save_config():
    try: CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception: pass

# ── Notes & memory ─────────────────────────────────────────────────────
_notes: list[dict] = []

def load_notes():
    global _notes
    try: _notes = json.loads(NOTES_FILE.read_text()) if NOTES_FILE.exists() else []
    except Exception: _notes = []

def save_notes():
    try: NOTES_FILE.write_text(json.dumps(_notes, indent=2))
    except Exception: pass

def load_memory() -> dict:
    try: return json.loads(MEMORY_FILE.read_text()) if MEMORY_FILE.exists() else {}
    except Exception: return {}

def save_memory(mem: dict):
    try: MEMORY_FILE.write_text(json.dumps(mem, indent=2))
    except Exception: pass

# ── Code memory (persists across /code sessions) ───────────────────────
def load_code_memory() -> dict:
    """Persistent knowledge base across all /code sessions."""
    try: return json.loads(CODE_MEM_FILE.read_text()) if CODE_MEM_FILE.exists() else {}
    except Exception: return {}

def save_code_memory(mem: dict):
    try: CODE_MEM_FILE.write_text(json.dumps(mem, indent=2))
    except Exception: pass

def update_code_memory(task: str, outcome: str, files: list[str]):
    """Record what was built, what worked, what failed."""
    mem = load_code_memory()
    entry = {
        "time": datetime.datetime.now().isoformat(timespec='seconds'),
        "task": task[:200],
        "outcome": outcome[:400],
        "files": files[:10],
    }
    mem.setdefault("sessions", []).append(entry)
    mem["sessions"] = mem["sessions"][-30:]  # keep last 30
    # Extract reusable patterns
    if "error" not in outcome.lower() and files:
        mem.setdefault("known_patterns", {})[task[:60]] = {
            "files": files, "summary": outcome[:200]
        }
    save_code_memory(mem)

# ── Readline ────────────────────────────────────────────────────────────
_CMDS = ['/exit','/clear','/model','/allow','/save','/load','/help','/ls',
         '/pwd','/status','/code','/auto','/note','/notes','/memory',
         '/diff','/undo','/plugins','/explain','/compress','/temp','/reconfigure']

def setup_readline():
    if not readline: return
    try:
        if HISTORY_FILE.exists(): readline.read_history_file(str(HISTORY_FILE))
        readline.set_history_length(2000)
        readline.parse_and_bind("tab: complete")
        readline.set_completer(
            lambda t, s: ([x for x in _CMDS if x.startswith(t)] + [None])[s])
    except Exception: pass

def save_readline_history():
    if not readline: return
    try: readline.write_history_file(str(HISTORY_FILE))
    except Exception: pass

# ── Utilities ───────────────────────────────────────────────────────────
def term_width() -> int:
    try: return min(os.get_terminal_size().columns, 120)
    except: return 80

def rule(char="─", color=C.GRAY):
    print(f"  {c(color, char * min(term_width()-4, 76))}")

def trunc(text: str, n: int = MAX_OUTPUT_LEN) -> str:
    if len(text) <= n: return text
    half = n // 2
    return (text[:half]
            + f"\n{c(C.YEL,'…')} {c(C.GRAY,f'[{len(text)-n:,} chars omitted]')} {c(C.YEL,'…')}\n"
            + text[-half:])

def uptime() -> str:
    s = int(time.time() - _start_time)
    return f"{s//3600}h {(s%3600)//60}m" if s >= 3600 else (f"{s//60}m {s%60}s" if s >= 60 else f"{s}s")

def udiff(orig: str, new: str, name: str = "file") -> str:
    return "".join(difflib.unified_diff(
        orig.splitlines(True), new.splitlines(True),
        fromfile=f"a/{name}", tofile=f"b/{name}")) or "(no changes)"

def workspace() -> str | None:
    parts = []
    try:
        root   = subprocess.check_output(['git','rev-parse','--show-toplevel'], stderr=subprocess.DEVNULL, text=True).strip()
        branch = subprocess.check_output(['git','branch','--show-current'], stderr=subprocess.DEVNULL, text=True).strip()
        parts.append(f"git:{Path(root).name}@{branch}")
    except Exception: pass
    venv = os.environ.get('VIRTUAL_ENV') or os.environ.get('CONDA_DEFAULT_ENV')
    if venv: parts.append(f"env:{Path(venv).name}")
    return ' · '.join(parts) if parts else None

def backup(p: Path) -> Path:
    bak = BACKUP_DIR / f"{p.name}_{int(time.time())}.bak"
    try: shutil.copy2(p, bak)
    except Exception: pass
    return bak

# ── Danger detection ────────────────────────────────────────────────────
_DANGER_RE = [
    r'\brm\s+(-[rRf]{1,3}|--recursive|--force)\s*/\S*',
    r'\bdd\b.*\bof=/dev/', r'\bmkfs\b', r'>\s*/dev/sd[a-z]',
    r':\(\)\s*\{.*\}', r'\bsudo\s+(rm|dd|mkfs)',
    r'curl[^|#\n]*\|\s*(ba)?sh', r'wget[^|#\n]*\|\s*(ba)?sh',
    r'\bshred\b', r'\bwipefs\b', r'nc\s+.*-[eEcC]',
    r'base64\s+-d\s*\|', r'python[23]?\s+-c\s+[\'"].*(?:import\s+os|subprocess)',
]
_danger_ok: set[str] = set()

def is_dangerous(cmd: str) -> bool:
    h = hashlib.sha1(cmd.encode()).hexdigest()
    if h in _danger_ok: return False
    return any(re.search(p, cmd, re.I) for p in _DANGER_RE)

def danger_confirm(label: str, detail: str = "") -> bool:
    print(); rule("─", C.RED)
    print(f"  {c(C.RED+C.B,'⚠  DANGEROUS')}  {c(C.GRAY, label)}")
    if detail:
        for ln in textwrap.wrap(detail[:300], 70): print(f"  {c(C.GRAY, ln)}")
    rule("─", C.RED)
    ans = input(f"  {c(C.RED+C.B,'Type YES to allow, ALWAYS to always allow:')} ").strip()
    if ans == "ALWAYS":
        _danger_ok.add(hashlib.sha1(detail.encode()).hexdigest())
        return True
    return ans == "YES"

# ── Spinner ─────────────────────────────────────────────────────────────
class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    MSGS = {
        "thinking":  ["thinking…","reasoning…","planning…"],
        "running":   ["running…","executing…"],
        "searching": ["searching…","fetching…"],
        "testing":   ["testing…","verifying…"],
        "coding":    ["coding…","drafting…"],
        "compress":  ["compressing…"],
    }
    def __init__(self, kind="thinking"):
        self.msgs  = self.MSGS.get(kind, self.MSGS["thinking"])
        self._stop = threading.Event()
        self._t    = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            msg = self.msgs[(i // 10) % len(self.msgs)]
            sys.stdout.write(f"\r  {c(C.CYAN, self.FRAMES[i%10])} {c(C.GRAY, msg)}   ")
            sys.stdout.flush(); time.sleep(0.09); i += 1

    def start(self): self._t.start(); return self

    def stop(self):
        self._stop.set(); self._t.join(0.3)
        sys.stdout.write(f"\r{' '*50}\r"); sys.stdout.flush()

# ── Output helpers ──────────────────────────────────────────────────────
def ok(m):   print(f"  {c(C.GREEN,'✓')}  {m}")
def warn(m): print(f"  {c(C.YEL,'⚠')}  {m}")
def err(m):  print(f"  {c(C.RED,'✗')}  {m}")
def info(m): print(f"  {c(C.BLUE,'·')}  {c(C.GRAY, m)}")

def tool_hdr(icon: str, tag: str, detail: str = ""):
    print(f"\n  {c(C.MAG,icon)} {c(C.GRAY+C.DIM,tag+':')} {c(C.WHT, detail[:80]+('…' if len(detail)>80 else ''))}")

def result_box(text: str, success: bool = True, max_lines: int = 35):
    col = C.DGRN if success else C.RED
    lines = text.splitlines()
    for ln in lines[:max_lines]:
        print(f"  {c(col,'│')} {c(C.GRAY, ln)}")
    if len(lines) > max_lines:
        print(f"  {c(col,'│')} {c(C.DGRAY, f'  … +{len(lines)-max_lines} lines')}")

def diff_box(text: str, max_lines: int = 50):
    print()
    for ln in text.splitlines()[:max_lines]:
        if ln.startswith('+') and not ln.startswith('+++'):   print(f"  {c(C.DGRN, ln)}")
        elif ln.startswith('-') and not ln.startswith('---'): print(f"  {c(C.RED,  ln)}")
        elif ln.startswith('@@'):                             print(f"  {c(C.CYAN, ln)}")
        else:                                                 print(f"  {c(C.GRAY, ln)}")

# ── Context compression ─────────────────────────────────────────────────
def compress_history(msgs: list[dict]) -> list[dict]:
    if len(msgs) <= CTX_COMPRESS_AT: return msgs
    sys_msgs   = [m for m in msgs if m['role'] == 'system']
    non_sys    = [m for m in msgs if m['role'] != 'system']
    if len(non_sys) <= CTX_KEEP_RECENT + 2: return msgs
    to_squash  = non_sys[:-CTX_KEEP_RECENT]
    keep       = non_sys[-CTX_KEEP_RECENT:]
    spin = Spinner("compress").start()
    try:
        blob = "\n".join(
            f"[{m['role'].upper()}]: {str(m['content'])[:300]}" for m in to_squash)
        payload = {
            "model": cfg["model"], "stream": False,
            "messages": [
                {"role":"system","content":"Summarise this conversation excerpt. Keep: file paths, decisions, errors, outcomes. Be concise."},
                {"role":"user","content": blob[:5000]},
            ],
            "options": {"temperature": 0.1, "num_ctx": 8192},
        }
        resp    = requests.post(CHAT_URL, json=payload, timeout=60)
        summary = resp.json().get("message",{}).get("content","").strip()
    except Exception as e:
        spin.stop(); warn(f"Compress failed: {e}"); return msgs
    finally: spin.stop()
    new = sys_msgs + [{"role":"user","content":f"[HISTORY SUMMARY]\n{summary}\n[/HISTORY]"}] + keep
    info(f"History compressed: {len(msgs)} → {len(new)} msgs")
    return new


def build_system_prompt() -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ws  = workspace()
    mem = load_memory()
    mem_str  = " | ".join(f"{k}={v}" for k, v in list(mem.items())[:5]) or "(empty)"
    note_str = " | ".join(n['text'][:40] for n in _notes[-2:]) or "(none)"

    tools = (
        # FS
        "<read>path</read>  <write file='path'>text</write>  <append file='path'>text</append>\n"
        "<patch file='path'><<<FIND\nold\n>>>REPLACE\nnew\n>>>END</patch>\n"
        "<delete file=p/>  <edit file=p pattern=re>repl</edit>\n"
        "<listdir>p</listdir>  <tree>p</tree>  <find>name||dir</find>  <glob>pat</glob>\n"
        "<grep>pat||file_or_dir</grep>  <cd>p</cd>  <mkdir>p</mkdir>\n"
        "<move src=a dest=b/>  <copy src=a dest=b/>  <zip src=p dest=z/>  <unzip src=z dest=p/>\n"
        "<diff file1=a file2=b/>  <summarize>p</summarize>  <stat>p</stat>  <wc>p</wc>\n"
        "<head n=20>p</head>  <tail n=20>p</tail>  <touch>p</touch>  <chmod>mode p</chmod>\n"
        # Exec
        "<execute>cmd</execute>  <pyeval>code</pyeval>  <test>file_or_cmd</test>  <which>prog</which>\n"
        # Web
        "<search>query</search>  <http url=URL/>  <http method=POST url=URL>body</http>\n"
        # Memory
        "<remember key=k>v</remember>  <forget key=k/>  <note>text</note>  <notes/>\n"
        # Util
        "<calc>expr</calc>  <env>VAR</env>  <uuid/>  <ts/>  <hash algo=sha256>text</hash>\n"
        "<b64>text</b64>  <unb64>text</unb64>  <urlencode>t</urlencode>  <urldecode>t</urldecode>\n"
        # JSON/CSV
        "<jsonq>expr||file</jsonq>  <csvhead>file</csvhead>  <csvq>col=val||file</csvq>\n"
        "<template file=p>vars_json</template>  <replace_all file=p>old|||new</replace_all>\n"
        # Code tools
        "<lint>file</lint>  <fmt>file</fmt>  <complexity>file</complexity>\n"
        "<imports>file</imports>  <tokcount>file</tokcount>  <todos>dir</todos>\n"
        "<pip>pkg</pip>  <pytest>path</pytest>  <black>file</black>\n"
        # Git
        "<git>args</git>  <git_log>n</git_log>  <git_diff/>  <git_status/>  <git_blame>f</git_blame>\n"
        # System
        "<ps/>  <df/>  <free/>  <du>path</du>  <uname/>  <kill>pid</kill>\n"
        # Fun
        "<weather>city</weather>  <define>word</define>  <translate lang=es>text</translate>\n"
        "<joke/>  <quote/>  <clip>text</clip>  <open>path_or_url</open>\n"
    )

    return (
        f"Your ARIA (Build 6) — a sharp, autonomous AI agent. "
        f"Solve tasks completely. Think step by step. Use tools strategically.\n\n"
        f"STATE: time={now}  cwd={os.getcwd()}  ws={ws or 'none'}\n"
        f"FLAGS: shell={'ON' if cfg['allow_shell'] else 'OFF'}  "
        f"net={'ON' if cfg['allow_network'] else 'OFF'}  "
        f"write={'ON' if cfg['allow_write'] else 'OFF'}\n"
        f"MEMORY: {mem_str}\nNOTES: {note_str}\n\n"
        f"TOOLS:\n{tools}\n"
        f"RULES:\n"
        f"1. ONE tool tag per reply — wait for result before using another.\n"
        f"2. EXACT syntax: <write file='name.ext'>content</write> — file= not path=, no semicolons, no extra attributes.\n"
        f"3. Always READ a file before patching it.\n"
        f"4. Prefer <patch> over full rewrites. Never rewrite a file that already exists unless architecture is broken.\n"
        f"5. On failure: read the exact error, diagnose, targeted fix only.\n"
        f"6. When done, say DONE and give a clear summary.\n"
        f"7. Be concise. No markdown tables in plans — just act."
    )

def code_agent_prompt(task: str, code_mem: dict) -> str:
    """Prompt tuned for 9b models: explicit, structured, memory-aware."""
    # Inject relevant past sessions
    past = ""
    sessions = code_mem.get("sessions", [])[-5:]
    if sessions:
        past = "PAST SESSIONS (learn from these):\n"
        for s in sessions:
            past += f"  [{s['time'][:10]}] {s['task'][:80]} → {s['outcome'][:100]}\n"
        past += "\n"

    patterns = code_mem.get("known_patterns", {})
    pat_str = ""
    if patterns:
        pat_str = "KNOWN PATTERNS:\n"
        for k, v in list(patterns.items())[-3:]:
            pat_str += f"  {k}: files={v['files']}\n"
        pat_str += "\n"

    return (
        f"Your ARIA Code Agent. You write and fix code. You are methodical.\n\n"
        f"{past}{pat_str}"
        f"TASK: {task}\n\n"
        f"WORKFLOW (follow this exactly):\n"
        f"STEP 1 — PLAN: List files to create/modify. State approach.\n"
        f"STEP 2 — BUILD: Write files using <write> or <patch> tools.\n"
        f"STEP 3 — TEST: Run with <test> or <execute>. Read ALL output.\n"
        f"STEP 4 — FIX: If failing, read the error, patch ONLY broken parts.\n"
        f"STEP 5 — REPEAT steps 3-4 until tests pass (max {CODE_MAX_ITER} tries).\n"
        f"STEP 6 — REPORT: State what was built, how to run it, any caveats.\n\n"
        f"ONE tool per message. Do not skip steps. Begin with STEP 1."
    )

# ═══════════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════

# ── File I/O ─────────────────────────────────────────────────────────────
_CODE_EXTS = {'.py','.js','.ts','.c','.cpp','.h','.rs','.go','.java',
              '.sh','.yaml','.json','.toml','.html','.css','.jsx','.tsx',
              '.vue','.rb','.php','.swift','.kt','.md','.sql','.xml'}

def tool_read(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    if p.is_dir():     return f"Is a directory — use <listdir>", False
    try:
        content = p.read_text(encoding='utf-8', errors='replace')
        if p.suffix.lower() in _CODE_EXTS:
            lines = content.splitlines()
            numbered = "\n".join(f"{i:>4} │ {l}" for i, l in enumerate(lines, 1))
            return f"[{path}] ({len(lines)} lines)\n{trunc(numbered)}", True
        return f"[{path}]\n{trunc(content)}", True
    except Exception as e: return f"Read error: {e}", False

def tool_write(path: str, content: str) -> tuple[str, bool]:
    if not cfg["allow_write"]: return "Write disabled", False
    p = Path(path).expanduser()
    try:
        bak = f"\n  backup: {backup(p).name}" if p.exists() else ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return f"Wrote {len(content):,} chars / {content.count(chr(10))+1} lines → {path}{bak}", True
    except Exception as e: return f"Write failed: {e}", False

def tool_append(path: str, content: str) -> tuple[str, bool]:
    if not cfg["allow_write"]: return "Write disabled", False
    try:
        p = Path(path).expanduser(); p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            f.write(content if content.endswith('\n') else content + '\n')
        return f"Appended {len(content):,} chars to {path}", True
    except Exception as e: return f"Append failed: {e}", False

def tool_delete(path: str) -> tuple[str, bool]:
    if not cfg["allow_write"]: return "Write disabled", False
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        bak = backup(p); p.unlink()
        return f"Deleted {path}  (backup: {bak.name})", True
    except Exception as e: return f"Delete failed: {e}", False

def tool_patch(path: str, body: str) -> tuple[str, bool]:
    if not cfg["allow_write"]: return "Write disabled", False
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path} — use <write> to create it first", False
    try: original = p.read_text(encoding='utf-8')
    except Exception as e: return f"Read error: {e}", False

    hunks = re.findall(r'<<<FIND\s*\n(.*?)>>>REPLACE\s*\n(.*?)>>>END', body, re.DOTALL)
    if not hunks:
        return ("Patch parse error. Required format:\n"
                "<<<FIND\nold text\n>>>REPLACE\nnew text\n>>>END"), False

    content, applied, report = original, 0, []
    for find_raw, repl_raw in hunks:
        find = find_raw.rstrip('\n')
        repl = repl_raw.rstrip('\n')
        if find in content:
            content = content.replace(find, repl, 1); applied += 1
            report.append(f"✓ ({len(find.splitlines())}→{len(repl.splitlines())} lines)")
        else:
            # Whitespace-normalised fallback
            sc = "\n".join(l.rstrip() for l in content.splitlines())
            sf = "\n".join(l.rstrip() for l in find.splitlines())
            if sf in sc:
                content = sc.replace(sf, repl, 1); applied += 1
                report.append(f"✓ (whitespace-normalised)")
            else:
                report.append(f"✗ NOT FOUND: {repr(find[:60])}")

    if not applied: return "No hunks applied — check that FIND text matches exactly.", False
    bak = backup(p)
    try: p.write_text(content, encoding='utf-8')
    except Exception as e: return f"Write error: {e}", False
    diff = udiff(original, content, p.name)
    return (f"Patched {path} [{applied}/{len(hunks)} hunks]  bak:{bak.name}\n"
            + " | ".join(report) + "\n\n" + diff), True

def tool_edit(path: str, pattern: str, replacement: str) -> tuple[str, bool]:
    if not cfg["allow_write"]: return "Write disabled", False
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        orig = p.read_text(encoding='utf-8')
        new, n = re.subn(pattern, replacement, orig)
        if not n: return f"Pattern not found: {pattern}", False
        backup(p); p.write_text(new, encoding='utf-8')
        return f"Replaced {n} occurrence(s)\n{udiff(orig, new, p.name)}", True
    except re.error as e: return f"Regex error: {e}", False
    except Exception as e: return f"Edit failed: {e}", False

# ── Navigation ────────────────────────────────────────────────────────
def tool_cd(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser().resolve()
    if not p.is_dir(): return f"Not a directory: {path}", False
    try: os.chdir(p); return f"Now in {p}", True
    except Exception as e: return f"cd failed: {e}", False

def tool_listdir(path: str) -> tuple[str, bool]:
    p = Path(path or ".").expanduser().resolve()
    if not p.is_dir(): return f"Not a directory: {path}", False
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = []
        for item in items[:150]:
            if item.is_file():
                sz = item.stat().st_size
                lines.append(f"  📄 {item.name}  {c(C.DGRAY, f'{sz//1024}KB' if sz>=1024 else f'{sz}B')}")
            else:
                lines.append(f"  📁 {c(C.CYAN, item.name)}/")
        if len(items) > 150: lines.append(f"  … +{len(items)-150} more")
        return "\n".join(lines) or "(empty)", True
    except Exception as e: return f"listdir error: {e}", False

def tool_tree(path: str, depth: int = 3) -> tuple[str, bool]:
    p = Path(path or ".").expanduser().resolve()
    if not p.is_dir(): return f"Not a directory: {path}", False
    lines = [f"{c(C.CYAN, p.name)}/"]
    def _walk(d: Path, prefix: str, level: int):
        if level > depth: return
        try: items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError: return
        for i, item in enumerate(items[:50]):
            conn = "└── " if i == len(items)-1 else "├── "
            ext  = "    " if i == len(items)-1 else "│   "
            if item.is_dir():
                lines.append(f"{prefix}{conn}{c(C.CYAN, item.name)}/")
                _walk(item, prefix+ext, level+1)
            else:
                lines.append(f"{prefix}{conn}{item.name}")
    _walk(p, "", 1)
    return "\n".join(lines[:200]), True

def tool_glob(pattern: str) -> tuple[str, bool]:
    try:
        results = sorted(Path.cwd().glob(pattern))[:200]
        return ("\n".join(str(r) for r in results) if results else "No matches"), bool(results)
    except Exception as e: return f"Glob error: {e}", False

def tool_grep(args: str) -> tuple[str, bool]:
    sep = '||'
    if sep in args: pattern, target = args.split(sep, 1)
    else:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2: return "Usage: <grep>pattern||file_or_dir</grep>", False
        pattern, target = parts
    pattern, target = pattern.strip(), target.strip()
    tp = Path(target).expanduser()
    if tp.is_file():   files = [tp]
    elif tp.is_dir():  files = [f for f in tp.rglob('*') if f.is_file() and f.stat().st_size < 1_000_000][:80]
    else: return f"Not found: {target}", False
    try: rx = re.compile(pattern, re.I)
    except re.error as e: return f"Regex error: {e}", False
    results, total = [], 0
    for fp in files:
        try:
            for i, line in enumerate(fp.read_text(errors='replace').splitlines(), 1):
                if rx.search(line):
                    rel = str(fp.relative_to(Path.cwd()) if fp.is_absolute() else fp)
                    results.append(f"{c(C.CYAN,rel)}:{c(C.YEL,str(i))}  {line.strip()[:160]}")
                    total += 1
                    if total >= 60: break
        except Exception: pass
        if total >= 60: break
    return ("\n".join(results) if results else "No matches"), bool(results)

def tool_find(args: str) -> tuple[str, bool]:
    if '||' in args: pat, d = args.split('||', 1)
    else:
        parts = args.strip().split(maxsplit=1)
        pat = parts[0]; d = parts[1] if len(parts) > 1 else "."
    try:
        rx   = re.compile(pat.strip(), re.I)
        hits = [str(p) for p in Path(d.strip()).expanduser().rglob('*') if rx.search(p.name)][:100]
        return ("\n".join(hits) if hits else "No matches"), bool(hits)
    except Exception as e: return f"Find error: {e}", False

def tool_mkdir(path: str) -> tuple[str, bool]:
    try: Path(path).expanduser().mkdir(parents=True, exist_ok=True); return f"Created {path}", True
    except Exception as e: return f"mkdir failed: {e}", False

def tool_move(src: str, dst: str) -> tuple[str, bool]:
    s, d = Path(src).expanduser(), Path(dst).expanduser()
    if not s.exists(): return f"Not found: {src}", False
    try: d.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(s), str(d)); return f"Moved {src} → {dst}", True
    except Exception as e: return f"Move failed: {e}", False

def tool_copy(src: str, dst: str) -> tuple[str, bool]:
    s, d = Path(src).expanduser(), Path(dst).expanduser()
    if not s.exists(): return f"Not found: {src}", False
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(s), str(d), dirs_exist_ok=True) if s.is_dir() else shutil.copy2(s, d)
        return f"Copied {src} → {dst}", True
    except Exception as e: return f"Copy failed: {e}", False

def tool_zip(src: str, dest: str) -> tuple[str, bool]:
    sp, dp = Path(src).expanduser(), Path(dest).expanduser()
    if not sp.exists(): return f"Not found: {src}", False
    try:
        with zipfile.ZipFile(dp, 'w', zipfile.ZIP_DEFLATED) as zf:
            if sp.is_dir():
                for f in sp.rglob('*'):
                    if f.is_file(): zf.write(f, f.relative_to(sp.parent))
            else: zf.write(sp, sp.name)
        return f"Created {dest} ({dp.stat().st_size:,} bytes)", True
    except Exception as e: return f"Zip failed: {e}", False

def tool_unzip(src: str, dest: str) -> tuple[str, bool]:
    sp, dp = Path(src).expanduser(), Path(dest).expanduser()
    if not sp.exists(): return f"Not found: {src}", False
    try:
        dp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(sp) as zf:
            zf.extractall(dp); n = len(zf.namelist())
        return f"Extracted {src} → {dest} ({n} files)", True
    except Exception as e: return f"Unzip failed: {e}", False

def tool_diff(f1: str, f2: str) -> tuple[str, bool]:
    p1, p2 = Path(f1).expanduser(), Path(f2).expanduser()
    if not p1.exists(): return f"Not found: {f1}", False
    if not p2.exists(): return f"Not found: {f2}", False
    try:
        t1 = p1.read_text(errors='replace'); t2 = p2.read_text(errors='replace')
        d = udiff(t1, t2, f1)
        return (d if d != "(no changes)" else "Files are identical."), True
    except Exception as e: return f"Diff failed: {e}", False

def tool_summarize(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    if p.is_dir():
        fs = list(p.rglob('*'))
        total = sum(f.stat().st_size for f in fs if f.is_file())
        return (f"Dir: {p.name}  files={sum(1 for f in fs if f.is_file())}  "
                f"dirs={sum(1 for f in fs if f.is_dir())}  size={total:,}B"), True
    try:
        text  = p.read_text(errors='replace'); lines = text.splitlines()
        words = sum(len(l.split()) for l in lines)
        preview = "\nFirst 10 lines:\n" + "\n".join(lines[:10])
        return f"File: {p.name}  ({p.stat().st_size:,}B  {len(lines):,} lines  {words:,} words){preview}", True
    except Exception as e: return str(e), False

# ── Execution ──────────────────────────────────────────────────────────
def tool_execute(cmd: str) -> tuple[str, bool]:
    if not cfg["allow_shell"]:
        return "Shell disabled. Enable with: /allow shell on", False
    if not cmd.strip(): return "Empty command", False
    if is_dangerous(cmd):
        if not (cfg["danger_confirm"] and danger_confirm("Shell command", cmd)):
            return "Blocked: dangerous pattern.", False
    spin = Spinner("running").start()
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=90, cwd=os.getcwd())
        spin.stop()
        out   = (res.stdout + res.stderr).strip() or "(no output)"
        label = c(C.GREEN if res.returncode==0 else C.RED, f"exit {res.returncode}")
        return f"{label}\n{trunc(out)}", res.returncode == 0
    except subprocess.TimeoutExpired: spin.stop(); return "Timed out (90s)", False
    except Exception as e: spin.stop(); return f"Execute failed: {e}", False

def tool_pyeval(code: str) -> tuple[str, bool]:
    global _pystate
    blocked = {'__import__', 'subprocess', 'os.system', '__builtins__', 'compile('}
    for tok in blocked:
        if tok in code: return f"Blocked: '{tok}' not allowed in pyeval", False
    old_out, old_err = sys.stdout, sys.stderr
    cap_out, cap_err = io.StringIO(), io.StringIO()
    try:
        sys.stdout, sys.stderr = cap_out, cap_err
        exec(compile(code, "<pyeval>", "exec"), _pystate)
        out = cap_out.getvalue(); err_s = cap_err.getvalue()
        result = out + (f"\n[stderr]:\n{err_s}" if err_s else "")
        return trunc(result.strip()) or "(no output)", True
    except Exception as e:
        return f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}", False
    finally:
        sys.stdout, sys.stderr = old_out, old_err

def tool_test(target: str) -> tuple[str, bool]:
    target = target.strip()
    if not target: return "No target", False
    p = Path(target)
    spin = Spinner("testing").start()
    try:
        if p.suffix == '.py' and p.exists():
            cmd = [sys.executable, str(p)]; is_list = True
        else:
            cmd = target; is_list = False
        res = subprocess.run(cmd if is_list else cmd, shell=not is_list,
                             capture_output=True, text=True,
                             timeout=SANDBOX_TIMEOUT, cwd=os.getcwd())
        spin.stop()
        passed = res.returncode == 0
        out    = (res.stdout + res.stderr).strip() or "(no output)"
        label  = c(C.GREEN if passed else C.RED, "PASS" if passed else "FAIL")
        return f"exit {res.returncode} — {label}\n{trunc(out)}", passed
    except subprocess.TimeoutExpired: spin.stop(); return f"Timed out ({SANDBOX_TIMEOUT}s)", False
    except Exception as e: spin.stop(); return f"Test error: {e}", False

def tool_which(prog: str) -> tuple[str, bool]:
    p = shutil.which(prog.strip())
    return (f"{prog} → {p}", True) if p else (f"{prog}: not found", False)

def tool_calc(expr: str) -> tuple[str, bool]:
    import math
    try:
        result = eval(re.sub(r'[^0-9+\-*/().%, a-zA-Z_]', '', expr),
                      {"__builtins__": {}}, vars(math))
        return str(result), True
    except Exception as e: return f"Calc error: {e}", False

def tool_env(var: str) -> tuple[str, bool]:
    val = os.environ.get(var.strip())
    return (f"{var}={val}", True) if val is not None else (f"{var} is not set", False)

# ── Web ────────────────────────────────────────────────────────────────
def tool_search(query: str) -> tuple[str, bool]:
    if not cfg["allow_network"]: return "Network disabled", False
    import urllib.parse, html as _html
    try:
        hdrs = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
        resp = requests.post("https://html.duckduckgo.com/html/",
                             data={"q": query}, headers=hdrs, timeout=12)
        resp.raise_for_status()
        results = []
        for m in re.finditer(r'<a class="result__url" href="([^"]+)"[^>]*>(.*?)</a>',
                             resp.text, re.I | re.S):
            link  = m.group(1)
            title = _html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
            if 'uddg=' in link:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse("http:"+link).query)
                link = qs.get('uddg', [link])[0]
            results.append(f"• {title}\n  {link}")
            if len(results) >= cfg.get("search_n", 5): break
        return "\n\n".join(results) or "No results.", bool(results)
    except Exception as e: return f"Search error: {e}", False

def tool_http(url: str, method: str = "GET", body: str = "") -> tuple[str, bool]:
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        hdrs = {"User-Agent": "ARIA/6.0"}
        resp = (requests.post(url, data=body, headers=hdrs, timeout=20)
                if method.upper() == "POST"
                else requests.get(url, headers=hdrs, timeout=20))
        resp.raise_for_status()
        ct = resp.headers.get('content-type', '')
        out = json.dumps(resp.json(), indent=2) if 'json' in ct else resp.text
        return f"HTTP {resp.status_code}\n{trunc(out, 8000)}", True
    except Exception as e: return f"HTTP error: {e}", False

# ── Memory & Notes ─────────────────────────────────────────────────────
def tool_remember(key: str, val: str) -> tuple[str, bool]:
    mem = load_memory(); mem[key] = val; save_memory(mem)
    return f"Remembered: {key} = {val}", True

def tool_forget(key: str) -> tuple[str, bool]:
    mem = load_memory()
    if key not in mem: return f"Key not found: {key}", False
    del mem[key]; save_memory(mem); return f"Forgotten: {key}", True

def tool_note(text: str) -> tuple[str, bool]:
    _notes.append({"text": text, "time": datetime.datetime.now().isoformat(timespec='seconds')})
    save_notes(); return f"Note saved: {text}", True

def tool_notes_list() -> tuple[str, bool]:
    if not _notes: return "No notes.", True
    return "\n".join(f"{n['time']}  {n['text']}" for n in _notes[-20:]), True

# ── Text utils ─────────────────────────────────────────────────────────
def _file_text(p: Path) -> tuple[str, bool]:
    if not p.exists(): return f"Not found: {p}", False
    try: return p.read_text(errors='replace'), True
    except Exception as e: return str(e), False

def tool_wc(path: str) -> tuple[str, bool]:
    t, ok_f = _file_text(Path(path).expanduser())
    if not ok_f: return t, False
    return f"lines={t.count(chr(10))}  words={len(t.split())}  chars={len(t)}  file={path}", True

def tool_head(path: str, n: int = 20) -> tuple[str, bool]:
    t, ok_f = _file_text(Path(path).expanduser())
    return ("\n".join(t.splitlines()[:n]), True) if ok_f else (t, False)

def tool_tail(path: str, n: int = 20) -> tuple[str, bool]:
    t, ok_f = _file_text(Path(path).expanduser())
    return ("\n".join(t.splitlines()[-n:]), True) if ok_f else (t, False)

def tool_touch(path: str) -> tuple[str, bool]:
    try: p = Path(path).expanduser(); p.parent.mkdir(parents=True, exist_ok=True); p.touch(); return f"Touched {p}", True
    except Exception as e: return str(e), False

def tool_stat(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    s = p.stat()
    mt = datetime.datetime.fromtimestamp(s.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    return (f"path={p}  size={s.st_size:,}B  modified={mt}  "
            f"mode={oct(s.st_mode)[-3:]}  type={'dir' if p.is_dir() else 'file'}"), True

def tool_chmod(args: str) -> tuple[str, bool]:
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2: return "Usage: <chmod>mode path</chmod>", False
    try: Path(parts[1]).expanduser().chmod(int(parts[0], 8)); return f"chmod {args}", True
    except Exception as e: return str(e), False

def tool_replace_all(path: str, args: str) -> tuple[str, bool]:
    if '|||' not in args: return "Usage: <replace_all file=p>old|||new</replace_all>", False
    old, new = args.split('|||', 1)
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        orig = p.read_text(encoding='utf-8'); n = orig.count(old)
        if not n: return f"'{old}' not found", False
        backup(p); p.write_text(orig.replace(old, new), encoding='utf-8')
        return f"Replaced {n} occurrence(s)", True
    except Exception as e: return str(e), False

def tool_template(path: str, vars_json: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        text = p.read_text(encoding='utf-8'); vals = json.loads(vars_json)
        for k, v in vals.items(): text = text.replace(f"{{{{{k}}}}}", str(v))
        return text, True
    except Exception as e: return str(e), False

# ── JSON / CSV ─────────────────────────────────────────────────────────
def tool_jsonq(args: str) -> tuple[str, bool]:
    if '||' not in args: return "Usage: <jsonq>expr||file</jsonq>", False
    expr, fpath = args.split('||', 1)
    p = Path(fpath.strip()).expanduser()
    if not p.exists(): return f"Not found: {fpath}", False
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        for part in re.split(r'\.(?![^[]*\])', expr.strip().lstrip('.')):
            if not part: continue
            m = re.match(r'^(\w+)?\[(\d+)\]$', part)
            if m:
                if m.group(1): data = data[m.group(1)]
                data = data[int(m.group(2))]
            else: data = data[part]
        return (json.dumps(data, indent=2) if not isinstance(data, str) else data), True
    except Exception as e: return f"jsonq error: {e}", False

def tool_csvhead(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        import csv
        with p.open(newline='', encoding='utf-8', errors='replace') as f:
            rows = list(csv.reader(f))[:5]
        if not rows: return "(empty)", True
        widths = [max(len(str(c)) for c in col) for col in zip(*rows)]
        return '\n'.join('  '.join(str(c).ljust(w) for c,w in zip(r,widths)) for r in rows), True
    except Exception as e: return str(e), False

def tool_csvq(args: str) -> tuple[str, bool]:
    if '||' not in args: return "Usage: <csvq>col=val||file</csvq>", False
    expr, fpath = args.split('||', 1)
    p = Path(fpath.strip()).expanduser()
    if not p.exists(): return f"Not found: {fpath}", False
    try:
        import csv
        with p.open(newline='', encoding='utf-8', errors='replace') as f:
            rows = list(csv.DictReader(f))
        if '=' in expr:
            col, val = expr.split('=', 1)
            rows = [r for r in rows if r.get(col.strip(),'').lower() == val.strip().lower()]
        return f"{len(rows)} rows\n" + '\n'.join(json.dumps(r) for r in rows[:50]), bool(rows)
    except Exception as e: return str(e), False

# ── Git ────────────────────────────────────────────────────────────────
def _git(args: str) -> tuple[str, bool]:
    if not cfg["allow_shell"]: return "Shell disabled", False
    try:
        res = subprocess.run(f"git {args}", shell=True, capture_output=True,
                             text=True, timeout=30, cwd=os.getcwd())
        return trunc((res.stdout + res.stderr).strip() or "(no output)"), res.returncode == 0
    except Exception as e: return str(e), False

def tool_git(args: str)     -> tuple[str, bool]: return _git(args)
def tool_git_log(n: str)    -> tuple[str, bool]: return _git(f"log --oneline -{n or 10}")
def tool_git_diff()         -> tuple[str, bool]: return _git("diff")
def tool_git_status()       -> tuple[str, bool]: return _git("status -s")
def tool_git_blame(f: str)  -> tuple[str, bool]: return _git(f"blame {f}")

# ── System ─────────────────────────────────────────────────────────────
def _ro(cmd: str) -> tuple[str, bool]:
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return (res.stdout + res.stderr).strip() or "(no output)", res.returncode == 0
    except Exception as e: return str(e), False

def tool_ps()             -> tuple[str, bool]: return _ro("ps aux | head -25")
def tool_df()             -> tuple[str, bool]: return _ro("df -h")
def tool_free()           -> tuple[str, bool]: return _ro("free -h")
def tool_du(path: str)    -> tuple[str, bool]: return _ro(f"du -sh {path}/*")
def tool_uname()          -> tuple[str, bool]: return _ro("uname -a")
def tool_kill(pid: str)   -> tuple[str, bool]:
    if not cfg["allow_shell"]: return "Shell disabled", False
    try: os.kill(int(pid), 15); return f"SIGTERM → {pid}", True
    except Exception as e: return str(e), False

# ── Network ────────────────────────────────────────────────────────────
def tool_ping(host: str)   -> tuple[str, bool]: return _ro(f"ping -c 3 -W 2 {host.strip()}")
def tool_dns(host: str)    -> tuple[str, bool]: return _ro(f"nslookup {host.strip()}")
def tool_whois(d: str)     -> tuple[str, bool]: return _ro(f"whois {d.strip()} 2>/dev/null | head -25")

# ── Python dev ─────────────────────────────────────────────────────────
def tool_pip(args: str) -> tuple[str, bool]:
    if not cfg["allow_shell"]: return "Shell disabled", False
    try:
        res = subprocess.run([sys.executable, "-m", "pip"] + args.split(),
                             capture_output=True, text=True, timeout=120)
        return (res.stdout + res.stderr).strip(), res.returncode == 0
    except Exception as e: return str(e), False

def tool_pytest(path: str) -> tuple[str, bool]:
    if not cfg["allow_shell"]: return "Shell disabled", False
    res = subprocess.run([sys.executable, "-m", "pytest", path.strip(), "-v", "--tb=short"],
                         capture_output=True, text=True, timeout=60, cwd=os.getcwd())
    return trunc((res.stdout + res.stderr).strip()), res.returncode == 0

def tool_black(path: str) -> tuple[str, bool]:
    res = subprocess.run([sys.executable, "-m", "black", path.strip()],
                         capture_output=True, text=True, timeout=30)
    return (res.stdout + res.stderr).strip(), res.returncode == 0

def tool_lint(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    for linter in ["flake8", "pylint", "pyflakes"]:
        if shutil.which(linter):
            res = subprocess.run([linter, str(p)], capture_output=True, text=True, timeout=30)
            return (res.stdout + res.stderr).strip() or "No issues.", res.returncode == 0
    try: ast.parse(p.read_text(encoding='utf-8')); return "Syntax OK (no linter installed)", True
    except SyntaxError as e: return f"SyntaxError: {e}", False

def tool_complexity(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        tree = ast.parse(p.read_text(encoding='utf-8'))
        scores = sorted(
            [(n.name, sum(1 for x in ast.walk(n)
                          if isinstance(x, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With))) + 1)
             for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
            key=lambda x: -x[1])
        return "Cyclomatic complexity:\n" + "\n".join(f"  {n}: {s}" for n, s in scores[:20]), True
    except Exception as e: return str(e), False

def tool_imports(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        tree = ast.parse(p.read_text(encoding='utf-8'))
        imps = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):           imps += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):     imps.append(f"from {n.module}")
        return "\n".join(sorted(set(imps))) or "(none)", True
    except Exception as e: return str(e), False

def tool_tokcount(path: str) -> tuple[str, bool]:
    p = Path(path).expanduser()
    if not p.exists(): return f"Not found: {path}", False
    try:
        text = p.read_text(errors='replace')
        return f"~{len(text)//4:,} tokens  ({len(text):,} chars)", True
    except Exception as e: return str(e), False

def tool_todos(dirpath: str) -> tuple[str, bool]:
    return tool_grep(f"TODO|FIXME|HACK||{dirpath}")

# ── Hashing / encoding ─────────────────────────────────────────────────
def tool_hash(text: str, algo: str = "sha256") -> tuple[str, bool]:
    h = hashlib.new(algo, text.encode(errors='replace'))
    return f"{algo}: {h.hexdigest()}", True

def tool_b64(text: str) -> tuple[str, bool]:
    import base64; return base64.b64encode(text.encode()).decode(), True

def tool_unb64(text: str) -> tuple[str, bool]:
    import base64
    try: return base64.b64decode(text.strip()).decode(errors='replace'), True
    except Exception as e: return str(e), False

def tool_urlencode(text: str) -> tuple[str, bool]:
    import urllib.parse; return urllib.parse.quote_plus(text), True

def tool_urldecode(text: str) -> tuple[str, bool]:
    import urllib.parse; return urllib.parse.unquote_plus(text), True

def tool_uuid() -> tuple[str, bool]:
    import uuid; return str(uuid.uuid4()), True

def tool_ts() -> tuple[str, bool]:
    now = datetime.datetime.now()
    return f"local={now.strftime('%Y-%m-%d %H:%M:%S')}  unix={int(now.timestamp())}  iso={now.isoformat()}", True

# ── Fun ────────────────────────────────────────────────────────────────
_JOKES = [
    "Why do programmers prefer dark mode? Light attracts bugs. 🐛",
    "A SQL query walks into a bar, walks up to two tables and asks 'Can I join you?'",
    "Why do Java devs wear glasses? Because they don't C#.",
    "There are 10 types of people: those who understand binary, and those who don't.",
    "Debugging: being the detective in a crime movie where you're also the murderer.",
]
_QUOTES = [
    '"Any fool can write code a computer understands. Good programmers write code humans understand." — Fowler',
    '"First, solve the problem. Then, write the code." — Johnson',
    '"Talk is cheap. Show me the code." — Torvalds',
    '"Make it work, make it right, make it fast." — Beck',
]

def tool_joke()  -> tuple[str, bool]: import random; return random.choice(_JOKES), True
def tool_quote() -> tuple[str, bool]: import random; return random.choice(_QUOTES), True

def tool_weather(city: str) -> tuple[str, bool]:
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        resp = requests.get(f"https://wttr.in/{city.strip()}?format=3",
                            timeout=8, headers={"User-Agent": "curl"})
        return resp.text.strip(), resp.ok
    except Exception as e: return str(e), False

def tool_define(word: str) -> tuple[str, bool]:
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        resp = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.strip()}", timeout=8)
        data = resp.json()
        if isinstance(data, list) and data:
            lines = []
            for m in data[0].get("meanings", [])[:2]:
                for d in m.get("definitions", [])[:1]:
                    lines.append(f"[{m.get('partOfSpeech','')}] {d.get('definition','')}")
            return "\n".join(lines) or "No definition found.", bool(lines)
        return str(data), False
    except Exception as e: return str(e), False

def tool_translate(text: str, lang: str = "es") -> tuple[str, bool]:
    if not cfg["allow_network"]: return "Network disabled", False
    try:
        import urllib.parse
        url = (f"https://translate.googleapis.com/translate_a/single?"
               f"client=gtx&sl=auto&tl={lang}&dt=t&q={urllib.parse.quote(text)}")
        data = requests.get(url, timeout=10).json()
        return "".join(p[0] for p in data[0] if p[0]), True
    except Exception as e: return f"Translate error: {e}", False

def tool_clip(text: str) -> tuple[str, bool]:
    for cmd in [["xclip","-selection","clipboard"],["xsel","--clipboard","--input"],["pbcopy"],["clip.exe"]]:
        if shutil.which(cmd[0]):
            try: subprocess.run(cmd, input=text.encode(), timeout=5); return f"Copied {len(text)} chars", True
            except Exception: pass
    return "No clipboard tool found", False

def tool_open(target: str) -> tuple[str, bool]:
    for opener in ["xdg-open","open","start"]:
        if shutil.which(opener):
            try: subprocess.Popen([opener, target.strip()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); return f"Opening {target}", True
            except Exception as e: return str(e), False
    return "No opener found", False

# ── Plugins ────────────────────────────────────────────────────────────
_plugins: dict[str, Any] = {}

def load_plugins():
    for fp in PLUGINS_DIR.glob("*.py"):
        try:
            ns: dict = {}
            exec(fp.read_text(encoding='utf-8'), ns)
            _plugins[fp.stem] = ns; info(f"Plugin: {fp.stem}")
        except Exception as e: warn(f"Plugin {fp.name}: {e}")

# ═══════════════════════════════════════════════════════════════════════
#  TOOL DISPATCHER
# ═══════════════════════════════════════════════════════════════════════

def dispatch(tag: str, fn, icon: str, detail: str, *args, **kwargs) -> tuple[bool, str, str]:
    global _last_tool_result
    tool_hdr(icon, tag, detail)
    result, success = fn(*args, **kwargs)
    _tool_counts[tag] += 1; _last_tool_result = result
    result_box(result, success)
    return True, result, tag

def process_tools(text: str) -> tuple[bool, str, str]:
    """Find and execute first tool tag. Returns (used, result, tag_name)."""

    # Structural tools (need special handling)
    m = re.search(r'<patch\s+(?:file|path)=["\']([^"\']+)["\'][^>]*>(.*?)</patch>', text, re.S|re.I)
    if m:
        fp, body = m.group(1).strip(), m.group(2)
        tool_hdr("🩹", "patch", fp)
        result, success = tool_patch(fp, body)
        _tool_counts["patch"] += 1; _last_tool_result = result
        result_box(result, success)
        if success and cfg["show_diffs"]:
            diff_lines = [l for l in result.splitlines() if l.startswith(('+','-','@@','---','+++'))]
            if diff_lines: diff_box("\n".join(diff_lines[:50]))
        return True, result, "patch"

    # write — accept file= or path= attribute
    m = re.search(r'<write\s+(?:file|path)=["\']([^"\']+)["\'][^>]*>(.*?)</write>', text, re.S|re.I)
    if not m:
        m2 = re.search(r'<write\s+([^>]+)>(.*?)</write>', text, re.S|re.I)
        if m2:
            fp_m = re.search(r'(?:file|path)=["\']?([^\s"\'>;&]+)', m2.group(1))
            if fp_m:
                return dispatch("write", tool_write, "💾", fp_m.group(1), fp_m.group(1).strip(), m2.group(2))
    if m: return dispatch("write", tool_write, "💾", m.group(1), m.group(1).strip(), m.group(2))

    # append — accept file= or path= attribute
    m = re.search(r'<append\s+(?:file|path)=["\']([^"\']+)["\'][^>]*>(.*?)</append>', text, re.S|re.I)
    if not m:
        m2 = re.search(r'<append\s+([^>]+)>(.*?)</append>', text, re.S|re.I)
        if m2:
            fp_m = re.search(r'(?:file|path)=["\']?([^\s"\'>;&]+)', m2.group(1))
            if fp_m:
                return dispatch("append", tool_append, "➕", fp_m.group(1), fp_m.group(1).strip(), m2.group(2))
    if m: return dispatch("append", tool_append, "➕", m.group(1), m.group(1).strip(), m.group(2))

    m = re.search(r'<delete\s+(?:file=["\']([^"\']+)["\'][^>]*/?|([^>]*))>', text, re.I)
    if not m: m = re.search(r'<delete>(.*?)</delete>', text, re.S|re.I)
    if m:
        fp = (m.group(1) or m.group(2) or "").strip()
        return dispatch("delete", tool_delete, "🗑", fp, fp)

    m = re.search(r'<diff\s+file1=["\']([^"\']+)["\']\s+file2=["\']([^"\']+)["\'][^>]*/?>',text,re.I)
    if m:
        f1, f2 = m.group(1), m.group(2)
        tool_hdr("🔀","diff",f"{f1} ↔ {f2}")
        result, success = tool_diff(f1, f2)
        _tool_counts["diff"] += 1; _last_tool_result = result
        result_box(result, success)
        if success: diff_box(result)
        return True, result, "diff"

    for tag, fn, icon in [("move", tool_move, "📦"), ("copy", tool_copy, "📋")]:
        m = re.search(rf'<{tag}\s+([^>]+)/>', text, re.I)
        if m:
            a = m.group(1)
            src = re.search(r'src=["\']([^"\']+)["\']', a)
            dst = re.search(r'dest=["\']([^"\']+)["\']', a)
            if src and dst: return dispatch(tag, fn, icon, f"{src.group(1)}→{dst.group(1)}", src.group(1), dst.group(1))

    m = re.search(r'<zip\s+src=["\']([^"\']+)["\']\s+dest=["\']([^"\']+)["\'][^>]*/?>',text,re.I)
    if m: return dispatch("zip", tool_zip, "📦", m.group(1), m.group(1), m.group(2))

    m = re.search(r'<unzip\s+src=["\']([^"\']+)["\']\s+dest=["\']([^"\']+)["\'][^>]*/?>',text,re.I)
    if m: return dispatch("unzip", tool_unzip, "📤", m.group(1), m.group(1), m.group(2))

    m = re.search(r'<remember\s+key=["\']([^"\']+)["\'][^>]*>(.*?)</remember>',text,re.S|re.I)
    if m: return dispatch("remember", tool_remember, "🧠", m.group(1), m.group(1).strip(), m.group(2).strip())

    m = re.search(r'<forget\s+key=["\']([^"\']+)["\'][^>]*/?>',text,re.I)
    if m: return dispatch("forget", tool_forget, "🗑", m.group(1), m.group(1).strip())

    m = re.search(r'<edit\s+file=["\']([^"\']+)["\']\s+pattern=["\']([^"\']+)["\'][^>]*>(.*?)</edit>',text,re.S|re.I)
    if m: return dispatch("edit", tool_edit, "✏️", m.group(1), m.group(1), m.group(2), m.group(3))

    m = re.search(r'<http\s+method=["\'](\w+)["\'][^>]*url=["\']([^"\']+)["\'][^>]*>(.*?)</http>',text,re.S|re.I)
    if m: return dispatch("http", tool_http, "🌐", f"{m.group(1)} {m.group(2)}", m.group(2), m.group(1), m.group(3))

    m = re.search(r'<http\s+url=["\']?([^"\'>\s]+)["\']?\s*/?>', text, re.I)
    if not m: m = re.search(r'<http>(.*?)</http>', text, re.S|re.I)
    if m: return dispatch("http", tool_http, "🌐", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<hash\s+algo=["\']([^"\']+)["\'][^>]*>(.*?)</hash>',text,re.S|re.I)
    if m: return dispatch("hash", tool_hash, "🔑", f"{m.group(1)}", m.group(2).strip(), m.group(1))

    m = re.search(r'<head\s+n=(\d+)>(.*?)</head>',text,re.S|re.I)
    if m: return dispatch("head", tool_head, "📄", m.group(2), m.group(2).strip(), int(m.group(1)))

    m = re.search(r'<tail\s+n=(\d+)>(.*?)</tail>',text,re.S|re.I)
    if m: return dispatch("tail", tool_tail, "📄", m.group(2), m.group(2).strip(), int(m.group(1)))

    m = re.search(r'<replace_all\s+file=["\']([^"\']+)["\'][^>]*>(.*?)</replace_all>',text,re.S|re.I)
    if m: return dispatch("replace_all", tool_replace_all, "🔄", m.group(1), m.group(1), m.group(2))

    m = re.search(r'<template\s+file=["\']([^"\']+)["\'][^>]*>(.*?)</template>',text,re.S|re.I)
    if m: return dispatch("template", tool_template, "📝", m.group(1), m.group(1), m.group(2).strip())

    m = re.search(r'<translate\s+lang=["\']([^"\']+)["\'][^>]*>(.*?)</translate>',text,re.S|re.I)
    if m: return dispatch("translate", tool_translate, "🌍", m.group(2)[:30], m.group(2).strip(), m.group(1))

    m = re.search(r'<cd>(.*?)</cd>',text,re.S|re.I)
    if m: return dispatch("cd", tool_cd, "📂", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<mkdir>(.*?)</mkdir>',text,re.S|re.I)
    if m: return dispatch("mkdir", tool_mkdir, "📁", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<chmod>(.*?)</chmod>',text,re.S|re.I)
    if m: return dispatch("chmod", tool_chmod, "🔒", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<calc>(.*?)</calc>',text,re.S|re.I)
    if m: return dispatch("calc", tool_calc, "🔢", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<env>(.*?)</env>',text,re.S|re.I)
    if m: return dispatch("env", tool_env, "🌍", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<note>(.*?)</note>',text,re.S|re.I)
    if m: return dispatch("note", tool_note, "📝", m.group(1).strip()[:60], m.group(1).strip())

    m = re.search(r'<notes\s*/>',text,re.I)
    if m: return dispatch("notes", tool_notes_list, "📝", "list")

    m = re.search(r'<git_log>(.*?)</git_log>',text,re.S|re.I)
    if m: return dispatch("git_log", tool_git_log, "📜", m.group(1).strip(), m.group(1).strip())

    m = re.search(r'<git_diff\s*/>',text,re.I)
    if m: return dispatch("git_diff", tool_git_diff, "🔀", "diff")

    m = re.search(r'<git_status\s*/>',text,re.I)
    if m: return dispatch("git_status", tool_git_status, "📊", "status")

    m = re.search(r'<git_blame>(.*?)</git_blame>',text,re.S|re.I)
    if m: return dispatch("git_blame", tool_git_blame, "📜", m.group(1), m.group(1).strip())

    m = re.search(r'<dps\s*/>',text,re.I)
    if m: return dispatch("dps", lambda: _ro("docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'"), "🐳", "docker ps")

    m = re.search(r'<ps\s*/>',text,re.I)
    if m: return dispatch("ps", tool_ps, "⚙️", "process list")

    m = re.search(r'<df\s*/>',text,re.I)
    if m: return dispatch("df", tool_df, "💿", "disk free")

    m = re.search(r'<free\s*/>',text,re.I)
    if m: return dispatch("free", tool_free, "💾", "memory free")

    m = re.search(r'<uname\s*/>',text,re.I)
    if m: return dispatch("uname", tool_uname, "🖥️", "uname")

    m = re.search(r'<uuid\s*/>',text,re.I)
    if m: return dispatch("uuid", tool_uuid, "🆔", "generate uuid")

    m = re.search(r'<ts\s*/>',text,re.I)
    if m: return dispatch("ts", tool_ts, "🕐", "timestamp")

    m = re.search(r'<joke\s*/>',text,re.I)
    if m: return dispatch("joke", tool_joke, "😄", "random joke")

    m = re.search(r'<quote\s*/>',text,re.I)
    if m: return dispatch("quote", tool_quote, "💬", "random quote")

    # Simple single-arg tools
    SIMPLE = [
        ("execute",    tool_execute,    r'<execute>(.*?)</execute>',    "⚡"),
        ("pyeval",     tool_pyeval,     r'<pyeval>(.*?)</pyeval>',      "🐍"),
        ("read",       tool_read,       r'<read>(.*?)</read>',          "📖"),
        ("test",       tool_test,       r'<test>(.*?)</test>',          "🧪"),
        ("which",      tool_which,      r'<which>(.*?)</which>',        "🔍"),
        ("summarize",  tool_summarize,  r'<summarize>(.*?)</summarize>',"📋"),
        ("search",     tool_search,     r'<search>(.*?)</search>',      "🔍"),
        ("listdir",    tool_listdir,    r'<listdir>(.*?)</listdir>',    "📁"),
        ("tree",       tool_tree,       r'<tree>(.*?)</tree>',          "🌲"),
        ("find",       tool_find,       r'<find>(.*?)</find>',          "🔎"),
        ("glob",       tool_glob,       r'<glob>(.*?)</glob>',          "🔎"),
        ("grep",       tool_grep,       r'<grep>(.*?)</grep>',          "🔬"),
        ("stat",       tool_stat,       r'<stat>(.*?)</stat>',          "ℹ️"),
        ("wc",         tool_wc,         r'<wc>(.*?)</wc>',              "📊"),
        ("head",       tool_head,       r'<head>(.*?)</head>',          "📄"),
        ("tail",       tool_tail,       r'<tail>(.*?)</tail>',          "📄"),
        ("touch",      tool_touch,      r'<touch>(.*?)</touch>',        "✨"),
        ("jsonq",      tool_jsonq,      r'<jsonq>(.*?)</jsonq>',        "{}"),
        ("csvhead",    tool_csvhead,    r'<csvhead>(.*?)</csvhead>',    "📊"),
        ("csvq",       tool_csvq,       r'<csvq>(.*?)</csvq>',          "📊"),
        ("git",        tool_git,        r'<git>(.*?)</git>',            "🔧"),
        ("du",         tool_du,         r'<du>(.*?)</du>',              "💿"),
        ("kill",       tool_kill,       r'<kill>(.*?)</kill>',          "🔴"),
        ("pip",        tool_pip,        r'<pip>(.*?)</pip>',            "📦"),
        ("pytest",     tool_pytest,     r'<pytest>(.*?)</pytest>',      "🧪"),
        ("black",      tool_black,      r'<black>(.*?)</black>',        "⬛"),
        ("lint",       tool_lint,       r'<lint>(.*?)</lint>',          "🔍"),
        ("fmt",        tool_black,      r'<fmt>(.*?)</fmt>',            "✨"),
        ("complexity", tool_complexity, r'<complexity>(.*?)</complexity>',"📊"),
        ("imports",    tool_imports,    r'<imports>(.*?)</imports>',    "📦"),
        ("tokcount",   tool_tokcount,   r'<tokcount>(.*?)</tokcount>',  "🔢"),
        ("todos",      tool_todos,      r'<todos>(.*?)</todos>',        "📝"),
        ("ping",       tool_ping,       r'<ping>(.*?)</ping>',          "🌐"),
        ("dns",        tool_dns,        r'<dns>(.*?)</dns>',            "🌐"),
        ("whois",      tool_whois,      r'<whois>(.*?)</whois>',        "🌐"),
        ("weather",    tool_weather,    r'<weather>(.*?)</weather>',    "🌤"),
        ("define",     tool_define,     r'<define>(.*?)</define>',      "📚"),
        ("b64",        tool_b64,        r'<b64>(.*?)</b64>',            "🔤"),
        ("unb64",      tool_unb64,      r'<unb64>(.*?)</unb64>',        "🔤"),
        ("urlencode",  tool_urlencode,  r'<urlencode>(.*?)</urlencode>',"🔗"),
        ("urldecode",  tool_urldecode,  r'<urldecode>(.*?)</urldecode>',"🔗"),
        ("clip",       tool_clip,       r'<clip>(.*?)</clip>',          "📋"),
        ("open",       tool_open,       r'<open>(.*?)</open>',          "🔗"),
    ]
    for tag, fn, pat, icon in SIMPLE:
        m = re.search(pat, text, re.S|re.I)
        if m: return dispatch(tag, fn, icon, m.group(1).strip()[:80], m.group(1).strip())

    return False, "", ""

# ═══════════════════════════════════════════════════════════════════════
#  INFERENCE
# ═══════════════════════════════════════════════════════════════════════

def run_inference(messages: list[dict], override_sys: str = "") -> str | None:
    global _msg_count, _token_total

    if cfg.get("auto_compress") and len(messages) > CTX_COMPRESS_AT:
        messages[:] = compress_history(messages)

    payload: dict = {
        "model": cfg["model"],
        "messages": messages,
        "stream":   cfg["stream"],
        "options": {
            "temperature": cfg["temperature"],
            "num_ctx":     cfg["ctx"],
            "top_p":       cfg["top_p"],
        },
    }
    if cfg["max_tokens"] > 0:
        payload["options"]["num_predict"] = cfg["max_tokens"]

    try:
        CRASH_FILE.write_text(json.dumps({"messages": messages[-8:], "cfg": cfg}, indent=2))
    except Exception: pass

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            fallback = cfg.get("fallback_model", "")
            if fallback and attempt == 1:
                warn(f"Retrying with fallback: {fallback}")
                payload["model"] = fallback
            else:
                time.sleep(1.5 * attempt)

        if cfg["stream"]:
            print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: ", end="", flush=True)
            full = ""; t0 = time.time()
            try:
                resp = requests.post(CHAT_URL, json=payload, stream=True, timeout=240)
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line: continue
                    data  = json.loads(line)
                    chunk = data.get("message",{}).get("content","")
                    full += chunk; sys.stdout.write(chunk); sys.stdout.flush()
                    if data.get("done"):
                        elapsed = time.time() - t0
                        tps     = data.get("eval_count",0) / max(elapsed, 0.01)
                        _token_total += data.get("eval_count", 0)
                        print(f"\n  {c(C.DGRAY, f'{elapsed:.1f}s · {tps:.0f}t/s')}")
                        break
                _msg_count += 1; return full
            except requests.HTTPError as e:
                err(f"HTTP {e.response.status_code}")
                if attempt >= MAX_RETRIES: return None
            except Exception as e:
                err(f"Inference error (attempt {attempt+1}): {e}")
                if attempt >= MAX_RETRIES: return None
        else:
            spin = Spinner("thinking").start()
            try:
                resp = requests.post(CHAT_URL, json=payload, timeout=240)
                spin.stop(); resp.raise_for_status()
                reply = resp.json().get("message",{}).get("content","")
                print(f"\n  {c(C.CYAN+C.B,'◈ ARIA')}: {reply}\n")
                _msg_count += 1; return reply
            except Exception as e:
                spin.stop(); err(f"Inference error: {e}")
                if attempt >= MAX_RETRIES: return None
    return None

# ═══════════════════════════════════════════════════════════════════════
#  AUTONOMOUS LOOP
# ═══════════════════════════════════════════════════════════════════════

def autonomous_loop(messages: list[dict]) -> str:
    final_reply = ""; consecutive_plain = 0
    for loop in range(MAX_TOOL_LOOPS):
        reply = run_inference(messages)
        if not reply: break
        messages.append({"role": "assistant", "content": reply})
        final_reply = reply
        tool_used, tool_result, tool_name = process_tools(reply)
        if tool_used:
            consecutive_plain = 0
            info(f"loop {loop+1}  tool={c(C.CYAN, tool_name)}")
            messages.append({"role": "user", "content":
                f"[{tool_name} result]\n{tool_result}\n\n"
                "Continue with next tool if needed, or give your final answer if done."})
        else:
            consecutive_plain += 1
            if consecutive_plain >= 2: break
            messages.append({"role": "user", "content":
                "Task complete? If yes, give final summary. If not, use a tool."})
    else:
        warn(f"Tool loop limit reached ({MAX_TOOL_LOOPS})")
    return final_reply

# ═══════════════════════════════════════════════════════════════════════
#  /CODE MODE  — with persistent memory
# ═══════════════════════════════════════════════════════════════════════

def code_mode(task: str):
    print(); rule("═", C.CYAN)
    print(f"  {c(C.CYAN+C.B,'⌨  CODE MODE')}  {c(C.WHT, task)}")
    rule("═", C.CYAN)
    info(f"max iters={CODE_MAX_ITER}  timeout={SANDBOX_TIMEOUT}s  memory=ON")
    print()

    code_mem = load_code_memory()
    messages: list[dict] = [
        {"role": "system", "content": code_agent_prompt(task, code_mem)},
        {"role": "user",   "content": task},
    ]

    last_pass   = False
    files_used: list[str] = []
    outcome     = "incomplete"

    for iteration in range(CODE_MAX_ITER * MAX_TOOL_LOOPS):
        reply = run_inference(messages)
        if not reply: break
        messages.append({"role": "assistant", "content": reply})
        tool_used, tool_result, tool_name = process_tools(reply)

        if tool_used:
            # Track files modified
            if tool_name in ("write","patch","edit","append"):
                m = re.search(r'(?:Wrote|Patched|Appended|Replaced)\s+\S+\s+.*?→\s+(\S+)', tool_result)
                if m: files_used.append(m.group(1))

            if tool_name == "test":
                passed = "PASS" in tool_result or "exit 0" in tool_result
                last_pass = passed
                label = c(C.GREEN if passed else C.RED, "PASS" if passed else "FAIL")
                print(f"\n  {c(C.CYAN+C.DIM, f'iter {iteration+1}')} test {label}")
                if passed:
                    outcome = "success"
                    messages.append({"role": "user", "content":
                        f"[Test PASS]\n{tool_result}\n\n"
                        "Write REPORT: what was built, how to use it, any caveats."})
                    report = run_inference(messages)
                    if report:
                        messages.append({"role": "assistant", "content": report})
                        outcome = report[:300]
                    break
                else:
                    messages.append({"role": "user", "content":
                        f"[Test FAIL — iter {iteration+1}/{CODE_MAX_ITER}]\n{tool_result}\n\n"
                        "Read the error carefully. Diagnose. Patch ONLY the broken part. "
                        "Do not rewrite the whole file unless the architecture is wrong."})
            else:
                messages.append({"role": "user", "content":
                    f"[{tool_name}]\n{tool_result}\n\nContinue."})
        else:
            done_words = {"complete","done","finished","report","built","summary","usage"}
            if any(w in reply.lower() for w in done_words):
                outcome = reply[:300]; break
            messages.append({"role": "user", "content":
                "Use a tool for the next step. Do not describe — act."})

    # Persist session to code memory
    update_code_memory(task, outcome, files_used)

    print(); rule("─", C.CYAN)
    s = c(C.GREEN,"✓ tests passed") if last_pass else c(C.YEL,"⚠ done (no passing test)")
    print(f"  {c(C.CYAN+C.B,'⌨  CODE DONE')}  {s}")
    rule("─", C.CYAN); print()

# ═══════════════════════════════════════════════════════════════════════
#  BANNER & PROMPT
# ═══════════════════════════════════════════════════════════════════════

def check_ollama() -> tuple[bool | None, list[str]]:
    try:
        r = requests.get(TAGS_URL, timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        ok_f = (cfg["model"] in models or
                any(cfg["model"].split(":")[0] in m for m in models))
        return ok_f, models
    except Exception: return None, []

def banner():
    print()
    print(f"  {c(C.CYAN+C.B,'ARIA')} {c(C.GRAY, VERSION)}  {c(C.DGRAY,'·  Autonomous Reasoning Intelligent Agent')}")
    rule()
    online, models = check_ollama()
    if online is None:   ms = c(C.RED,'✗ ollama offline')
    elif not online:
        avail = ", ".join(models[:4]) or "none"
        ms = c(C.YEL, f'⚠ model not found  (available: {avail})')
    else: ms = c(C.GREEN,'✓ ready')
    dot = lambda f: c(C.GREEN if f else C.GRAY, '●')
    print(f"  {c(C.GRAY,'model')}   {c(C.WHT, cfg['model'])}  {ms}")
    print(f"  {c(C.GRAY,'access')}  shell {dot(cfg['allow_shell'])}  net {dot(cfg['allow_network'])}  write {dot(cfg['allow_write'])}  auto {dot(cfg['autonomous'])}")
    code_mem = load_code_memory()
    if sessions := code_mem.get("sessions", []):
        print(f"  {c(C.GRAY,'code mem')} {c(C.DGRAY, f'{len(sessions)} past sessions')}")
    if _plugins:
        print(f"  {c(C.GRAY,'plugins')} {c(C.CYAN, '  '.join(_plugins.keys()))}")
    rule()
    info("type /help  ·  /code <task> for code agent  ·  ↑↓ history")
    print()

def prompt_line() -> str:
    ws  = workspace()
    ws_s = f" {c(C.DGRN, ws)}" if ws else ""
    return (f"\n  {c(C.GRAY+C.DIM, f'#{_msg_count+1}')} {c(C.GRAY+C.DIM, Path.cwd().name)}{ws_s}\n"
            f"  {c(C.GREEN+C.B,'▶')} {c(C.WHT+C.B,'you')}: ")

# ═══════════════════════════════════════════════════════════════════════
#  STATUS & HELP
# ═══════════════════════════════════════════════════════════════════════

def show_status():
    _, models = check_ollama()
    code_mem  = load_code_memory()
    print(); rule()
    print(f"  {c(C.CYAN+C.B,'ARIA')} {c(C.GRAY, VERSION)}  ·  {uptime()} uptime")
    rule()
    rows = [
        ("model",        cfg["model"]),
        ("messages",     str(_msg_count)),
        ("tokens",       f"{_token_total:,}"),
        ("cwd",          os.getcwd()),
        ("shell",        str(cfg["allow_shell"])),
        ("network",      str(cfg["allow_network"])),
        ("write",        str(cfg["allow_write"])),
        ("auto",         str(cfg["autonomous"])),
        ("compress",     str(cfg["auto_compress"])),
        ("notes",        str(len(_notes))),
        ("code sessions",str(len(code_mem.get("sessions",[])))),
        ("plugins",      ", ".join(_plugins.keys()) or "none"),
    ]
    for k, v in rows:
        vc = C.GREEN if v.lower()=='true' else (C.GRAY if v.lower()=='false' else C.WHT)
        print(f"  {c(C.GRAY, k.ljust(14))} {c(vc, v)}")
    if _tool_counts:
        print(f"\n  {c(C.GRAY,'tools used:')}")
        for t, n in sorted(_tool_counts.items(), key=lambda x: -x[1]):
            print(f"  {c(C.CYAN, t.ljust(14))} {c(C.WHT, str(n))}")
    if models:
        print(f"\n  {c(C.GRAY,'available models:')}")
        for m in models[:8]:
            mark = c(C.GREEN," ←") if cfg["model"].split(":")[0] in m else ""
            print(f"  {c(C.GRAY,'  '+m)}{mark}")
    rule(); print()

def show_help():
    cmds = [
        ("/exit",              "Exit ARIA"),
        ("/clear",             "Clear conversation history"),
        ("/code [task]",       "Coding agent with memory"),
        ("/auto [on|off]",     "Toggle autonomous loop"),
        ("/model <name>",      "Switch model"),
        ("/allow <feat> <on>", "Toggle shell/network/write"),
        ("/temp <0.0-1.0>",    "Set temperature"),
        ("/compress",          "Compress history now"),
        ("/status",            "Session stats"),
        ("/ls [path]",         "List directory"),
        ("/tree [path]",       "Directory tree"),
        ("/pwd",               "Working directory"),
        ("/note <text>",       "Add a note"),
        ("/notes",             "List notes"),
        ("/memory",            "Show persistent memory"),
        ("/diff <f1> <f2>",    "Diff two files"),
        ("/undo",              "List backups"),
        ("/codemem",           "Show code mode memory"),
        ("/explain",           "Explain last tool result"),
        ("/save",              "Save session"),
        ("/load [file]",       "Load session"),
        ("/reconfigure",       "Re-run setup"),
        ("/plugins",           "List plugins"),
        ("/help",              "This screen"),
    ]
    print(); rule()
    print(f"  {c(C.CYAN+C.B,'Commands')}")
    rule()
    for cmd, desc in cmds:
        print(f"  {c(C.WHT+C.B, cmd.ljust(24))} {c(C.GRAY, desc)}")
    rule()
    info("↑↓ history  ·  Tab completion  ·  Ctrl+C interrupt  ·  Ctrl+D exit")
    print()

def show_exit_stats():
    print(); rule()
    print(f"  {c(C.CYAN,'Session')}  msgs={_msg_count}  tokens={_token_total:,}  uptime={uptime()}  tools={sum(_tool_counts.values())}")
    rule(); print(f"  {c(C.GRAY+C.DIM,'goodbye ✦')}"); print()

# ═══════════════════════════════════════════════════════════════════════
#  COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════════════

def handle_command(line: str) -> str | None:
    parts = line.split(); verb = parts[0].lower()

    if verb == '/exit':
        show_exit_stats(); save_readline_history(); save_notes(); sys.exit(0)

    elif verb == '/clear': return "clear"

    elif verb == '/compress':
        messages_history[:] = compress_history(messages_history)
        ok(f"History: {len(messages_history)} messages")

    elif verb == '/code':
        task = " ".join(parts[1:]) or input(f"  {c(C.YEL,'Task:')} ").strip()
        if task: code_mode(task)
        else: warn("No task specified.")

    elif verb == '/auto':
        if len(parts) > 1:
            cfg['autonomous'] = parts[1].lower() in ('true','1','yes','on')
            save_config(); ok(f"Autonomous: {'ON' if cfg['autonomous'] else 'OFF'}")
        else: ok(f"Autonomous: {'ON' if cfg['autonomous'] else 'OFF'}")

    elif verb == '/temp':
        if len(parts) > 1:
            try:
                cfg['temperature'] = max(0.0, min(1.0, float(parts[1])))
                save_config(); ok(f"Temperature → {cfg['temperature']}")
            except ValueError: warn("Usage: /temp 0.3")
        else: ok(f"Temperature: {cfg['temperature']}")

    elif verb == '/ls':
        result, ok_f = tool_listdir(parts[1] if len(parts) > 1 else ".")
        print(); result_box(result, ok_f)

    elif verb == '/tree':
        result, ok_f = tool_tree(parts[1] if len(parts) > 1 else ".")
        print(); result_box(result, ok_f)

    elif verb == '/pwd': ok(os.getcwd())

    elif verb == '/status': show_status()

    elif verb == '/model':
        if len(parts) > 1: cfg['model'] = parts[1]; save_config(); ok(f"Model → {parts[1]}")
        else:
            _, models = check_ollama()
            for m in models:
                mark = c(C.GREEN," ←") if cfg["model"].split(":")[0] in m else ""
                print(f"  {c(C.GRAY, m)}{mark}")

    elif verb == '/allow':
        if len(parts) == 3 and parts[1].lower() in ('shell','network','write'):
            feat = parts[1].lower(); val = parts[2].lower() in ('true','1','yes','on')
            cfg[f'allow_{feat}'] = val; save_config()
            ok(f"{feat}: {'ON' if val else 'OFF'}")
            if feat == 'shell' and val: warn("Shell enabled — dangerous patterns still blocked.")
        else: warn("Usage: /allow <shell|network|write> <on|off>")

    elif verb == '/reconfigure':
        first_time_setup()
        if messages_history and messages_history[0]["role"] == "system":
            messages_history[0]["content"] = build_system_prompt()
        ok("System prompt refreshed.")

    elif verb == '/save':
        sf = SESSION_DIR / f"session_{int(time.time())}.json"
        try:
            sf.write_text(json.dumps({"ts": time.time(), "messages": messages_history,
                                       "cfg": cfg, "cwd": os.getcwd()}, indent=2))
            ok(f"Saved → {sf.name}")
        except Exception as e: err(f"Save failed: {e}")

    elif verb == '/load':
        if len(parts) < 2:
            sessions = sorted(SESSION_DIR.glob("*.json"), reverse=True)[:10]
            if not sessions: warn("No saved sessions.")
            else:
                for s in sessions:
                    ts = s.stem.replace("session_","")
                    print(f"  {c(C.CYAN, s.name)}  {c(C.GRAY, datetime.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M'))}")
            return None
        sf = Path(parts[1]); sf = sf if sf.exists() else SESSION_DIR / parts[1]
        if not sf.exists(): err("Session not found."); return None
        try:
            data = json.loads(sf.read_text())
            messages_history[:] = data.get("messages", []); cfg.update(data.get("cfg", {}))
            if (cwd := data.get("cwd")) and Path(cwd).exists(): os.chdir(cwd)
            ok(f"Loaded: {sf.name}"); return "load"
        except Exception as e: err(f"Load failed: {e}")

    elif verb == '/note':
        text = " ".join(parts[1:])
        if text: tool_note(text)
        else: warn("Usage: /note <text>")

    elif verb == '/notes':
        result, _ = tool_notes_list(); print(); result_box(result, True)

    elif verb == '/memory':
        mem = load_memory()
        if mem:
            print()
            for k, v in mem.items(): print(f"  {c(C.CYAN, k)}  {c(C.GRAY, str(v))}")
        else: info("Memory is empty.")

    elif verb == '/codemem':
        mem = load_code_memory()
        sessions = mem.get("sessions", [])
        if not sessions: info("No code sessions yet.")
        else:
            print()
            for s in sessions[-10:]:
                print(f"  {c(C.CYAN, s['time'][:10])}  {c(C.WHT, s['task'][:60])}")
                print(f"    {c(C.GRAY, s['outcome'][:80])}")
                if s.get('files'): print(f"    {c(C.DGRAY, 'files: '+', '.join(s['files'][:4]))}")

    elif verb == '/diff':
        if len(parts) < 3: warn("Usage: /diff <file1> <file2>")
        else:
            result, ok_f = tool_diff(parts[1], parts[2])
            print(); result_box(result, ok_f)
            if ok_f: diff_box(result)

    elif verb == '/undo':
        baks = sorted(BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)[:15]
        if not baks: info("No backups found.")
        else:
            for b in baks:
                ts = datetime.datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                print(f"  {c(C.CYAN, b.name)}  {c(C.GRAY, ts)}  {c(C.DGRAY, f'{b.stat().st_size:,}B')}")
            info("Restore: cp ~/.aria_backups/<file> <destination>")

    elif verb == '/plugins':
        if _plugins:
            for name, ns in _plugins.items():
                desc = ns.get("__doc__") or ns.get("DESCRIPTION","(no description)")
                print(f"  {c(C.CYAN, name)}  {c(C.GRAY, str(desc)[:60])}")
        else: info(f"No plugins. Add .py files to {PLUGINS_DIR}")

    elif verb == '/explain':
        if _last_tool_result:
            run_inference([
                {"role":"system","content":"You are ARIA. Explain this tool output clearly and concisely. What does it mean? Any issues to note?"},
                {"role":"user","content": _last_tool_result[:3000]},
            ])
        else: info("No tool result to explain.")

    elif verb == '/help': show_help()

    else:
        suggestion = difflib.get_close_matches(verb, _CMDS, n=1, cutoff=0.55)
        if suggestion: warn(f"Unknown: {verb} — did you mean {c(C.CYAN, suggestion[0])}?")
        else: warn(f"Unknown command: {verb}  (try /help)")

    return None

# ═══════════════════════════════════════════════════════════════════════
#  FIRST-TIME SETUP & CRASH RECOVERY
# ═══════════════════════════════════════════════════════════════════════

def first_time_setup():
    print(f"\n  {c(C.CYAN+C.B,'✨ First-time setup')}"); rule()
    cfg["browser_cmd"] = input(f"  {c(C.YEL,'Browser')} {c(C.GRAY,'[firefox]:')} ").strip() or "firefox"
    cfg["editor_cmd"]  = input(f"  {c(C.YEL,'Editor')}  {c(C.GRAY,'[code]:')} ").strip() or "code"
    cfg["allow_shell"] = input(f"  {c(C.YEL,'Enable shell?')} {c(C.GRAY,'(yes/no) [no]:')} ").strip().lower() in ('yes','y')
    cfg["fallback_model"] = input(f"  {c(C.YEL,'Fallback model')} {c(C.GRAY,'(blank=none):')} ").strip()
    Path(cfg["projects_dir"]).mkdir(parents=True, exist_ok=True)
    save_config(); rule()
    ok(f"Saved.  Shell: {'ON' if cfg['allow_shell'] else 'OFF'}")
    time.sleep(0.3)

def check_crash_recovery():
    if not CRASH_FILE.exists(): return
    try:
        data = json.loads(CRASH_FILE.read_text())
        msgs = data.get("messages", [])
        age  = time.time() - CRASH_FILE.stat().st_mtime
        if not msgs or age > 21600: CRASH_FILE.unlink(missing_ok=True); return
        dt_str = datetime.datetime.fromtimestamp(CRASH_FILE.stat().st_mtime).strftime("%H:%M:%S")
        warn(f"Crash recovery available from {dt_str} ({len(msgs)} messages)")
        if input(f"  {c(C.YEL,'Restore? (y/N):')} ").strip().lower() in ('y','yes'):
            messages_history[:] = msgs; cfg.update(data.get("cfg", {})); ok("Restored.")
        CRASH_FILE.unlink(missing_ok=True)
    except Exception: pass

# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    load_config(); load_notes(); load_plugins(); setup_readline()
    if not CONFIG_FILE.exists(): first_time_setup()
    banner(); check_crash_recovery()
    messages_history.append({"role": "system", "content": build_system_prompt()})

    while True:
        try:
            user_input = input(prompt_line()).strip()
            if not user_input: continue

            if user_input.startswith('/'):
                result = handle_command(user_input)
                if result == "clear":
                    messages_history.clear()
                    messages_history.append({"role": "system", "content": build_system_prompt()})
                    ok("History cleared.")
                elif result == "load":
                    if messages_history and messages_history[0]["role"] == "system":
                        messages_history[0]["content"] = build_system_prompt()
                continue

            messages_history.append({"role": "user", "content": user_input})

            if cfg.get("autonomous", True):
                autonomous_loop(messages_history)
            else:
                reply = run_inference(messages_history)
                if reply:
                    messages_history.append({"role": "assistant", "content": reply})
                    tool_used, tool_result, tool_name = process_tools(reply)
                    if tool_used:
                        messages_history.append({"role": "user", "content":
                            f"[{tool_name} result]\n{tool_result}\n\nContinue or give final answer."})

        except KeyboardInterrupt:
            print(f"\n  {c(C.GRAY+C.DIM,'interrupted  ·  /exit to quit')}")
        except EOFError:
            show_exit_stats(); save_readline_history(); save_notes(); break


if __name__ == "__main__":
    main()
