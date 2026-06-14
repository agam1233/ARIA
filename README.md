<img width="2560" height="1299" alt="image" src="https://github.com/user-attachments/assets/a2f44d0e-6365-45de-9cae-23be3f67b4aa" /># ✦ Aria — Autonomous Reasoning Intelligent Agent

A local AI agent with a clean web UI.

---

## What is Aria?

Aria is a local AI agent that connects to your own model runtime (Ollama, LM Studio, or any OpenAI-compatible server) and gives it real tools — file read/write, shell commands, web search, and browsing — all from a polished chat interface in your browser.

The model is given a system prompt that teaches it how to use its tools. Remove the system prompt and you basically lobotomize it — but because this is open source, free as in speech and free as in beer, you can fork it and do whatever you want.

---

## Features

- **Real tools** — read files, write files, edit files, run shell commands, search the web, browse URLs
- **Zero dependencies** — stdlib only, no pip installs required for the agent itself
- **Streaming UI** — responses stream token by token with live tool call indicators
- **Deep research mode** — multi-step search and browse investigations
- **LAN access control** — other devices on your network can request access; the host approves or denies them
- **Confirmation prompts** — shell commands and file writes ask before running (configurable)
- **Chat history** — conversations are saved locally in your browser
- **Fully customisable** — system prompt, temperature, theme, accent colour, and more in Settings

---

## Supported Runtimes

- :llama: [Ollama](https://ollama.com)
- :test_tube: [LM Studio](https://lmstudio.ai)
- ⌘ [OpenCode](https://opencode.ai)
- :gear: Any OpenAI-compatible endpoint

---

## Quickstart

```bash
git clone https://github.com/agam1233/ARIA
cd ARIA
python3 requirements.py
python3 ariaagent.py
```

Then open [http://127.0.0.1:8400](http://127.0.0.1:8400) in your browser.

On first run, select your runtime and endpoint. Aria will detect available models automatically.

---

## Adding Tools

The tool system is designed to be extended. To add a new tool:

1. Add a handler function in the tools section of `ariaagent.py`
2. Register it in the `TOOLS` dict
3. Update the system prompt (`TOOL_SPEC`) so the model knows the tool exists and how to call it

---

## License

Free as in speech. Free as in beer. Do whatever you want with it.
<img width="2560" height="1299" alt="image" src="https://github.com/user-attachments/assets/1a6bd67b-c301-499e-a904-a89317cf6edb" />

