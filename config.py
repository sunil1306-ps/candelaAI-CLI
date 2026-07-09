import os
import json
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~/.candela"))
CONFIG_FILE = CONFIG_DIR / "config.json"

PROJECT_DIR = Path(__file__).parent.resolve()
server_py_default = str((PROJECT_DIR / "mcp" / "server.py").as_posix())
dataset_default = str((PROJECT_DIR / "mcp" / "lanforge_mcp_dataset.json").as_posix())
scripts_default = str((PROJECT_DIR / "mcp" / "lanforge-scripts").as_posix())

DEFAULT_CONFIG = {
    "provider": "gemini",
    "gemini_api_key": "",
    "openai_api_key": "",
    "openai_base_url": "https://openrouter.ai/api/v1",
    "openai_model": "qwen/qwen3-coder:free",
    "gemini_model": "gemini-3.1-flash-lite",
    "mcp_servers": {
        "lanforge": {
            "command": "python",
            "args": [
                server_py_default,
                "--dataset", dataset_default,
                "--scripts", scripts_default
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

def get_default_mcp_config(server_py: str, dataset: str, scripts: str, manager_url: str = "http://localhost:8080") -> dict:
    """Build an MCP server config block from given paths."""
    return {
        "lanforge": {
            "command": "python",
            "args": [
                server_py,
                "--dataset", dataset,
                "--scripts", scripts
            ],
            "env": {
                "LANFORGE_BASE_URL": manager_url,
                "LANFORGE_TIMEOUT": "30",
                "LANFORGE_SSH_ENABLED": "true",
                "LANFORGE_SSH_USER": "root"
            }
        }
    }

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config = DEFAULT_CONFIG.copy()
        config["mcp_servers"] = {}

        # Pick up API keys from environment
        if os.environ.get("GEMINI_API_KEY"):
            config["gemini_api_key"] = os.environ["GEMINI_API_KEY"]
            config["provider"] = "gemini"
        if os.environ.get("OPENAI_API_KEY"):
            config["openai_api_key"] = os.environ["OPENAI_API_KEY"]
        if os.environ.get("OPENAI_BASE_URL"):
            config["openai_base_url"] = os.environ["OPENAI_BASE_URL"]
        if os.environ.get("OPENAI_MODEL"):
            config["openai_model"] = os.environ["OPENAI_MODEL"]

        # Pick up MCP config from environment variables if set
        server_py  = os.environ.get("LANFORGE_SERVER_PY", "")
        dataset    = os.environ.get("LANFORGE_DATASET", "")
        scripts    = os.environ.get("LANFORGE_SCRIPTS", "")
        mgr_url    = os.environ.get("LANFORGE_BASE_URL", "http://localhost:8080")
        if server_py and dataset and scripts:
            config["mcp_servers"] = get_default_mcp_config(server_py, dataset, scripts, mgr_url)

        save_config(config)
        return config

    try:
        with open(CONFIG_FILE, "r") as f:
            user_config = json.load(f)
        config = DEFAULT_CONFIG.copy()
        for k, v in user_config.items():
            if k == "mcp_servers" and isinstance(v, dict):
                config["mcp_servers"].update(v)
            else:
                config[k] = v
        return config
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
