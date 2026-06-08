#!/usr/bin/env python3
"""
ARIA Full Bootstrap Installer
- installs python deps
- installs Ollama (if missing)
- starts Ollama
- pulls qwen3.5:9b
"""

import os
import sys
import subprocess
import platform
import shutil
import time

REQUIRED_PY = ["requests"]

OLLAMA_MODEL = "qwen3.5:9b"
OLLAMA_URL = "http://localhost:11434"


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────

def run(cmd):
    print(f"\n$ {cmd}")
    return subprocess.call(cmd, shell=True)

def check_python(pkg):
    try:
        __import__(pkg)
        return True
    except:
        return False

def pip_install(pkg):
    run(f"{sys.executable} -m pip install {pkg}")

def has_cmd(cmd):
    return shutil.which(cmd) is not None


# ─────────────────────────────────────────────
# install ollama
# ─────────────────────────────────────────────

def install_ollama():
    print("\n🔥 Installing Ollama...")

    if has_cmd("ollama"):
        print("✔ Ollama already installed")
        return

    system = platform.system().lower()

    if system == "linux":
        run("curl -fsSL https://ollama.com/install.sh | sh")
    elif system == "darwin":
        run("brew install ollama")
    else:
        print("❌ Unsupported OS for auto install")
        sys.exit(1)


# ─────────────────────────────────────────────
# start ollama
# ─────────────────────────────────────────────

def start_ollama():
    print("\n🧠 Starting Ollama server...")

    if has_cmd("ollama"):
        # try background start
        subprocess.Popen(["ollama", "serve"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(3)
        print("✔ Ollama launch attempted")


# ─────────────────────────────────────────────
# pull model
# ─────────────────────────────────────────────

def pull_model():
    print(f"\n📦 Pulling model: {OLLAMA_MODEL}")

    result = run(f"ollama pull {OLLAMA_MODEL}")

    if result != 0:
        print("❌ Model pull failed")
        sys.exit(1)

    print("✔ Model ready")


# ─────────────────────────────────────────────
# verify ollama
# ─────────────────────────────────────────────

def check_ollama_alive():
    try:
        import requests
        r = requests.get(OLLAMA_URL, timeout=3)
        return r.status_code < 500
    except:
        return False


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    print("\n🚀 ARIA FULL SYSTEM BOOTSTRAP 🚀\n")

    # python deps
    print("📦 Checking Python dependencies...")
    for pkg in REQUIRED_PY:
        if not check_python(pkg):
            print(f"Missing: {pkg}")
            pip_install(pkg)
        else:
            print(f"✔ {pkg}")

    # ollama install
    install_ollama()

    # start ollama
    start_ollama()

    # wait for server
    print("\n⏳ Waiting for Ollama...")
    for _ in range(10):
        if check_ollama_alive():
            print("✔ Ollama is running")
            break
        time.sleep(1)
    else:
        print("⚠ Ollama not responding but continuing...")

    # pull model
    pull_model()
print("Qwen3.5 9B succesfully pulled. Run ariaagent.py using python3 ariaagent.py")


if __name__ == "__main__":
    main()