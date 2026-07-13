# CandelaAI CLI

An agentic AI CLI for LANforge network test automation. Supports Google Gemini and any OpenAI-compatible provider (OpenRouter, local Ollama, etc.) with full LANforge MCP tool integration.

---

## Requirements

- Python 3.9+
- Git (optional, for cloning)
- A LANforge system reachable over the network
- A Google Gemini API key **or** an OpenRouter/OpenAI API key

---

## Installation (Any Machine)

### 1. Copy or clone the project

```bash
# Option A: clone from your repo
git clone https://github.com/sunil1306-ps/candelaAI-CLI.git
cd candelaAI

# Option B: copy the folder to the new machine
```

### 2. Create a virtual environment and install dependencies

**Windows:**
```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Run the interactive setup — it will ask for your API key, model, and LANforge paths:

```bash
python candela.py configure
```

**What it asks:**
| Prompt | Default / Example |
|--------|-------------------|
| LLM Provider | `gemini` or `openai` |
| Gemini API Key | `AIza...` |
| Gemini Model | `gemini-3.1-flash-lite` |
| Configure LANforge MCP? | `y` |
| Path to `server.py` | `<project_dir>/lanforge_lanforge_mcp/server.py` (Default) |
| Path to `lanforge_mcp_dataset.json` | `<project_dir>/lanforge_lanforge_mcp/lanforge_mcp_dataset.json` (Default) |
| Path to `lanforge-scripts` dir | `<project_dir>/lanforge_lanforge_mcp/lanforge-scripts` (Default) |
| LANforge Manager URL | `http://192.168.215.183:8080` |

Config is saved to `~/.candela/config.json` (your home directory).

### 4. Start chatting

```bash
python candela.py chat
```

---

## Deployment Scenarios

### Scenario A — CLI + MCP on Windows laptop, controlling remote LANforge via SSH (Remote Mode)

Run in **remote mode**. The CLI automatically connects to the remote machine via SSH and runs the MCP server on the remote LANforge system:

```bash
python candela.py chat --mode remote --ssh-host 192.168.215.183 --ssh-user root
```

> **Requirements for remote mode:**
> - SSH key-based auth set up to the LANforge machine (no password prompts).
> - LANforge scripts (`/home/lanforge/lanforge-scripts`) must exist on the remote LANforge system.
> - Python 3.9+ installed on the remote machine.
> - No manual path translation is needed: the client automatically maps your local project's `lanforge_mcp/` folder path to the corresponding remote path on the LANforge host (e.g., `/home/lanforge/Desktop/sunil/candela_cli/lanforge`).

### Scenario B — CLI + MCP server both on the same Linux machine (Local Mode)

If you are running directly on the LANforge machine (or a Linux system with LANforge scripts cloned locally), you can configure local paths:

```bash
python3 candela.py configure
```
Enter the local paths when prompted (e.g., path to `server.py`, `dataset`, and the local `lanforge-scripts` directory).

### Scenario C — Environment variables (headless / CI)

Set these before running — no interactive prompts needed:

```bash
export GEMINI_API_KEY=AIza...
export LANFORGE_SERVER_PY=/path/to/candelaAI/lanforge_lanforge_mcp/server.py
export LANFORGE_DATASET=/path/to/candelaAI/lanforge_lanforge_mcp/lanforge_mcp_dataset.json
export LANFORGE_SCRIPTS=/path/to/lanforge-scripts
export LANFORGE_BASE_URL=http://192.168.215.183:8080

python3 candela.py chat --mode local
```

---

## Configuration File

Located at `~/.candela/config.json`. You can edit it directly:

```json
{
  "provider": "gemini",
  "gemini_api_key": "YOUR_KEY",
  "gemini_model": "gemini-3.1-flash-lite",
  "mcp_servers": {
    "lanforge": {
      "command": "python",
      "args": [
        "D:/Projects/candelaAI/lanforge_lanforge_mcp/server.py",
        "--dataset", "D:/Projects/candelaAI/lanforge_lanforge_mcp/lanforge_mcp_dataset.json",
        "--scripts", "D:/Projects/candelaAI/lanforge_lanforge_mcp/lanforge-scripts"
      ],
      "env": {
        "LANFORGE_BASE_URL": "http://192.168.215.183:8080",
        "LANFORGE_TIMEOUT": "30",
        "LANFORGE_SSH_ENABLED": "true",
        "LANFORGE_SSH_USER": "root"
      }
    }
  }
}
```

---

## SSH Key Setup (for remote mode)

On the machine running the CLI:
```bash
ssh-keygen -t ed25519 -C "candelaai"
ssh-copy-id root@192.168.215.183
# Test: should connect without a password prompt
ssh root@192.168.215.183 echo ok
```

---

## What Files Do You Need to Copy?

The entire `candelaAI` project directory is self-contained. Copy it to any machine:
- `candela.py`, `client.py`, `mcp_client.py`, `config.py` (CLI client files)
- `lanforge_mcp/server.py`, `lanforge_mcp/lanforge_mcp_dataset.json`, `lanforge_mcp/lanforge_scripts_index.json` (MCP server files)
- `requirements.txt` (dependencies)

The remote LANforge machine only needs the automation scripts repository folder (`/home/lanforge/lanforge-scripts`) which is automatically invoked via SSH during remote execution.

