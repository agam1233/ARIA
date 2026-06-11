#!/usr/bin/env python3
"""
ARIA Full Bootstrap Installer (Cross-Platform)
- installs python deps
- installs Ollama (Windows/Linux/macOS safe)
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
    except ImportError:
        return False


def pip_install(pkg):
    run(f"{sys.executable} -m pip install {pkg}")


def has_cmd(cmd):
    return shutil.which(cmd) is not None


# ─────────────────────────────────────────────
# install ollama (CROSS PLATFORM FIXED)
# ─────────────────────────────────────────────

def install_ollama():
    print("\n Checking if you have ollama.")

    if has_cmd("ollama"):
        print("✔ Ollama already installed")
        return

    system = platform.system().lower()

    if system == "linux":
        print("🐧 Linux detected")
        run("curl -fsSL https://ollama.com/install.sh | sh")

    elif system == "darwin":
        print("🍎 macOS detected")
        run("brew install ollama")

    elif system == "windows":
        print("🪟 Windows detected")
        print("\n⚠ Ollama must be installed manually on Windows:")
        print("👉 Option 1: Download ollama manually.")
        input("\nPress ENTER after installing Ollama...")

    else:
        print("❌ Unsupported OS")
        sys.exit(1)


# ─────────────────────────────────────────────
# start ollama
# ─────────────────────────────────────────────

def start_ollama():
    print("\n🧠 Starting Ollama server...")

    if not has_cmd("ollama"):
        print("❌ Ollama not found in PATH")
        return

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(3)
        print("✔ Ollama launch attempted")
    except Exception as e:
        print(f"⚠ Failed to start Ollama: {e}")


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
# check ollama alive
# ─────────────────────────────────────────────

def check_ollama_alive():
    try:
        import requests
        r = requests.get(OLLAMA_URL, timeout=3)
        return r.status_code < 500
    except Exception:
        return False


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    print("\nBootstrapper\n")

    # Python deps
    print(" Checking Python deps")
    for pkg in REQUIRED_PY:
        if not check_python(pkg):
            print(f"Missing: {pkg}")
            pip_install(pkg)
        else:
            print(f"✔ {pkg}")

    # Ollama install
    install_ollama()

    # Start Ollama
    start_ollama()

    # Wait for server
    print("\n Waiting for Ollama...")
    for _ in range(10):
        if check_ollama_alive():
            print("✔ Ollama is running")
            break
        time.sleep(1)
    else:
        print("⚠ Ollama not responding but continuing...")

    # Pull model
    pull_model()

    print("\n🎉 SETUP COMPLETE 🎉")
    print("Run: python3 ariaagent.py")


if __name__ == "__main__":
    main()
