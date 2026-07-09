#!/usr/bin/env python3
"""LANforge MCP server.

This server speaks MCP over stdio and proxies LANforge CLI commands and queries
to the LANforge HTTP REST/JSON API, falling back to SSH telnet if needed.
Supports dynamic switching and targeting of different LANforge Manager IPs.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SERVER_NAME = "lanforge-mcp"
SERVER_VERSION = "1.1.0"

DEFAULT_BASE_URL = "http://host.docker.internal:8080"
DATASET_PATH = Path(__file__).parent / "lanforge_mcp_dataset.json"


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        dataset = json.load(handle)
    tools = dataset.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError(f"Dataset {path} does not contain a tools list")
    return dataset


def compact_text(value: Any, limit: int = 8000) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, indent=2, sort_keys=True)
    if len(text) > limit:
        return text[:limit] + "\n...<truncated>"
    return text


def json_content(value: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": compact_text(value)}]


def safe_tool_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in name)
    return cleaned[:128]


def cli_quote(value: Any) -> str:
    text = str(value)
    if text == "":
        return "''"
    if any(ch.isspace() for ch in text) or "'" in text:
        return "'" + text.replace("'", "''") + "'"
    return text


def decode_response(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def parse_ip_port(base_url: str) -> tuple[str, int]:
    if "://" not in base_url:
        if ":" in base_url:
            host, port_str = base_url.split(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                return host, 8080
        return base_url, 8080
    else:
        parsed = urlparse(base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8080
        return host, port


def generate_scripts_index(scripts_dir: Path) -> dict[str, Any]:
    index = {}
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        return index

    for path in scripts_dir.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix not in (".py", ".pl", ".sh", ".bash"):
            continue

        parts = path.relative_to(scripts_dir).parts
        if any(p.startswith(".") or p == "__pycache__" or p == "deprecated" or p == "sandbox" or p == "scripts_deprecated" or p == "archive" for p in parts):
            continue

        rel_path = "/".join(parts)
        description = ""
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                lines = [f.readline() for _ in range(25)]
                lines = [line.strip() for line in lines if line]

                comment_lines = []
                in_docstring = False
                docstring_delim = None

                for line in lines:
                    if line.startswith("#!"):
                        continue

                    if '"""' in line or "'''" in line:
                        delim = '"""' if '"""' in line else "'''"
                        if not in_docstring:
                            in_docstring = True
                            docstring_delim = delim
                            content = line.split(delim, 1)[1].strip()
                            if content:
                                comment_lines.append(content)
                            if line.count(delim) == 2:
                                content = content.split(delim, 1)[0].strip()
                                comment_lines = [content] if content else []
                                in_docstring = False
                                break
                        else:
                            content = line.split(delim, 1)[0].strip()
                            if content:
                                comment_lines.append(content)
                            in_docstring = False
                            break
                        continue

                    if in_docstring:
                        comment_lines.append(line)
                        continue

                    if line.startswith("#"):
                        content = line.lstrip("#").strip()
                        if content:
                            comment_lines.append(content)
                    elif line == "" and comment_lines:
                        comment_lines.append("")
                    elif line != "":
                        break

                description = "\n".join(comment_lines).strip()
        except Exception:
            pass

        index[rel_path] = {
            "name": path.name,
            "path": rel_path,
            "description": description[:1200] if description else "No description available."
        }
    return index


def run_script_help(scripts_dir: Path, script_path: str) -> str:
    """Run a script with --help and return the output. Used to extract argument info."""
    full_path = (scripts_dir / script_path).resolve()
    if not full_path.is_file():
        return "Script not found."

    suffix = full_path.suffix.lower()
    if suffix == ".py":
        cmd = [sys.executable, str(full_path), "--help"]
    elif suffix == ".pl":
        cmd = ["perl", str(full_path), "--help"]
    elif suffix in (".sh", ".bash"):
        cmd = ["bash", str(full_path), "--help"]
    else:
        return "Cannot run --help on this script type."

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([
        str(scripts_dir),
        str(scripts_dir / "py-json"),
        env.get("PYTHONPATH", "")
    ])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=15
        )
        out = result.stdout or result.stderr or "No help output."
        return out[:6000]  # Cap at 6000 chars
    except subprocess.TimeoutExpired:
        return "Script --help timed out."
    except Exception as e:
        return f"Error running --help: {e}"


class LanforgeClient:
    def __init__(self, host: str, user: str, timeout: float) -> None:
        self.host = host
        self.user = user
        self.timeout = timeout

    def call(self, cli_command: str, host_override: str | None = None) -> dict[str, Any]:
        if not cli_command:
            return {"ok": False, "error": "Empty command"}

        target_host = host_override or self.host
        # Escape single quotes for the shell command
        safe_cmd = cli_command.replace("'", "'\"'\"'")
        # Construct the telnet wrapper
        remote_wrapper = f"(echo '{safe_cmd}'; sleep 2; echo 'exit') | telnet localhost 4001"
        
        full_ssh_cmd = ["ssh", "-o", "BatchMode=yes", f"{self.user}@{target_host}", remote_wrapper]
        
        started = time.monotonic()
        try:
            result = subprocess.run(
                full_ssh_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            output = result.stdout if result.stdout else result.stderr
            return {
                "ok": result.returncode == 0,
                "command": cli_command,
                "duration_ms": duration_ms,
                "status": result.returncode,
                "response": output,
                "source": "SSH_Telnet_Fallback"
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "command": cli_command,
                "status": -1,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "response": f"Error: SSH command timed out after {self.timeout}s",
                "source": "SSH_Telnet_Fallback"
            }
        except Exception as e:
            return {
                "ok": False,
                "command": cli_command,
                "status": -1,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "response": f"Error executing SSH command: {str(e)}",
                "source": "SSH_Telnet_Fallback"
            }


class LanforgeMcpServer:
    def __init__(self, dataset: dict[str, Any], scripts_dir: Path, client: LanforgeClient, expose_commands: bool) -> None:
        self.dataset = dataset
        self.scripts_dir = scripts_dir
        self.client = client
        self.expose_commands = expose_commands
        
        self.commands: dict[str, dict[str, Any]] = {
            item["name"]: item for item in dataset.get("tools", []) if isinstance(item, dict) and "name" in item
        }
        
        self.direct_tool_prefix = os.getenv("LANFORGE_TOOL_PREFIX", "lf_")
        self.direct_tools: dict[str, str] = {}
        used_direct_names: set[str] = set()
        for command_name in self.commands:
            direct_name = self.direct_tool_prefix + safe_tool_name(command_name)
            if direct_name in used_direct_names:
                direct_name = f"{direct_name}_{len(used_direct_names)}"
            self.direct_tools[direct_name] = command_name
            used_direct_names.add(direct_name)

        # Connection configs
        self.host = client.host
        self.port = int(os.getenv("LANFORGE_HTTP_PORT", "8080"))
        self.timeout = client.timeout
        self.ssh_enabled = os.getenv("LANFORGE_SSH_ENABLED", "true").lower() in ("1", "true", "yes")

        # Load or generate scripts index
        self.scripts_index_path = DATASET_PATH.parent / "lanforge_scripts_index.json"
        self.scripts_index = {}
        self.load_or_generate_scripts_index()

    def load_or_generate_scripts_index(self):
        if self.scripts_index_path.is_file():
            try:
                sys.stderr.write(f"Loading scripts index from {self.scripts_index_path}...\n")
                with self.scripts_index_path.open("r", encoding="utf-8") as f:
                    self.scripts_index = json.load(f)
                return
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to load scripts index: {e}. Regenerating...\n")
        
        if self.scripts_dir.exists() and self.scripts_dir.is_dir():
            sys.stderr.write("Generating scripts index (first time setup)...\n")
            self.scripts_index = generate_scripts_index(self.scripts_dir)
            try:
                with self.scripts_index_path.open("w", encoding="utf-8") as f:
                    json.dump(self.scripts_index, f, indent=2)
                sys.stderr.write(f"Saved scripts index to {self.scripts_index_path}.\n")
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to save scripts index: {e}\n")
        else:
            sys.stderr.write(f"Warning: Scripts directory not found at {self.scripts_dir}. No scripts will be available.\n")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if req_id is None:
            return None

        try:
            if method == "initialize":
                result = self.initialize()
            elif method == "tools/list":
                result = self.tools_list()
            elif method == "tools/call":
                result = self.tools_call(params)
            elif method == "resources/list":
                result = self.resources_list()
            elif method == "resources/read":
                result = self.resources_read(params)
            elif method == "prompts/list":
                result = self.prompts_list()
            elif method == "prompts/get":
                result = self.prompts_get(params)
            else:
                raise McpError(-32601, f"Unsupported method: {method}")
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except McpError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": req_id, "error": error}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": str(exc),
                    "data": traceback.format_exc(),
                },
            }

    def initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {}
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "active_manager": f"http://{self.host}:{self.port}",
                "manager_ip": self.host,
                "manager_port": self.port,
                "ssh_enabled": self.ssh_enabled,
                "instructions": (
                    f"Active LANforge Manager: {self.host}:{self.port}. "
                    "Always pass --mgr {manager_ip} to any py-script run via lanforge_run_script. "
                    "Use lanforge_script_info to get the exact arguments for any script before running it. "
                    "Use lanforge_list_scripts to discover available scripts. "
                    "Use lanforge_get_request to query live system state."
                ).format(manager_ip=self.host)
            },
        }

    def tools_list(self) -> dict[str, Any]:
        tools = [
            {
                "name": "lanforge_list_commands",
                "description": "List LANforge CLI commands, optionally filtered by search text or category.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Optional search text filter (name, description, category)."},
                        "category": {"type": "string", "description": "Optional exact category name filter."},
                        "limit": {"type": "integer", "description": "Maximum commands to return.", "default": 50},
                    },
                    "required": [],
                },
            },
            {
                "name": "lanforge_command_info",
                "description": "Get detailed schema, syntax, category, and documentation for one LANforge CLI command.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "LANforge command name, for example adb."}
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "lanforge_call",
                "description": "Execute any LANforge CLI JSON command through POST /cli-json/<command> with automated SSH-telnet fallback.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "LANforge command name."},
                        "parameters": {
                            "type": "object",
                            "description": "JSON body to send to LANforge.",
                            "additionalProperties": True,
                        },
                        "base_url": {
                            "type": "string",
                            "description": "Optional specific LANforge Manager base URL (e.g. http://192.168.1.101:8080) to target for this call. Overrides default session IP."
                        }
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "lanforge_get_request",
                "description": "Perform an HTTP GET request to a LANforge REST JSON API endpoint (e.g. /ports, /cx, /endp, /wps) to query current system state.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "endpoint": {"type": "string", "description": "The endpoint path (e.g. '/ports' or '/ports/1/1/wlan0')."},
                        "query_params": {
                            "type": "object",
                            "description": "Optional key-value parameters to pass in the query string.",
                            "additionalProperties": True
                        },
                        "base_url": {
                            "type": "string",
                            "description": "Optional specific LANforge Manager base URL (e.g. http://192.168.1.101:8080) to target for this query. Overrides default session IP."
                        }
                    },
                    "required": ["endpoint"]
                }
            },
            {
                "name": "lanforge_set_active_manager",
                "description": "Switch the active default LANforge Manager base URL in memory for all subsequent operations in this session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "base_url": {
                            "type": "string",
                            "description": "The new default base URL (e.g. 'http://192.168.1.105:8080' or just IP '192.168.1.105')."
                        }
                    },
                    "required": ["base_url"]
                }
            },
            {
                "name": "lanforge_get_active_manager",
                "description": "Get details about the active default LANforge Manager (IP, port, SSH status) targeted by this session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "lanforge_render_cli",
                "description": "Render a LANforge command and parameters as a positional CLI string for review.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "LANforge command name."},
                        "parameters": {
                            "type": "object",
                            "description": "Command parameters keyed by argument name.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "lanforge_list_scripts",
                "description": "List and search available automation scripts in the lanforge-scripts directory without traversing the filesystem.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Optional search term to filter scripts by path, name, or description."}
                    },
                    "required": []
                }
            },
            {
                "name": "lanforge_run_script",
                "description": (
                    "Execute an automation script (Python, Perl, or Bash) from the lanforge-scripts folder. "
                    "IMPORTANT: Before calling this tool, ALWAYS call lanforge_script_info first to get the "
                    "exact --help output for the script so you know what arguments to pass. "
                    "All py-scripts require --mgr <manager_ip> as the first argument. "
                    "Never guess argument names - use lanforge_script_info to verify them."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "script_path": {
                            "type": "string",
                            "description": "Relative path of the script under lanforge-scripts (e.g. 'py-scripts/create_station.py'). Use lanforge_list_scripts to find available scripts."
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command-line arguments as a flat list of strings. Use lanforge_script_info to learn valid arguments. Example: ['--mgr', '192.168.1.1', '--radio', 'wiphy0', '--ssid', 'MyNet', '--passwd', 'pass123', '--security', 'wpa2']"
                        }
                    },
                    "required": ["script_path"]
                }
            },
            {
                "name": "lanforge_script_info",
                "description": (
                    "Get the full --help output (argument names, types, descriptions, and examples) for any "
                    "script in the lanforge-scripts folder. Call this BEFORE lanforge_run_script to know "
                    "exactly what arguments a script accepts. Returns the argparse --help text which lists "
                    "every supported flag with its description and default value."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "script_path": {
                            "type": "string",
                            "description": "Relative path of the script (e.g. 'py-scripts/create_station.py')."
                        }
                    },
                    "required": ["script_path"]
                }
            }
        ]

        if self.expose_commands:
            for command in self.commands.values():
                schema = command.get("inputSchema") or {"type": "object", "properties": {}, "required": []}
                direct_name = next(name for name, original in self.direct_tools.items() if original == command["name"])
                tools.append(
                    {
                        "name": direct_name,
                        "description": command.get("description") or f"Execute LANforge command {command['name']}.",
                        "inputSchema": schema,
                    }
                )

        return {"tools": tools}

    def tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpError(-32602, "Tool arguments must be an object")

        if name == "lanforge_list_commands":
            return {"content": json_content(self.list_commands(arguments))}
        if name == "lanforge_command_info":
            return {"content": json_content(self.command_info(require_str(arguments, "command")))}
        if name == "lanforge_call":
            command = require_str(arguments, "command")
            payload = arguments.get("parameters") or {}
            base_url = arguments.get("base_url")
            if not isinstance(payload, dict):
                raise McpError(-32602, "parameters must be an object")
            return {"content": json_content(self.execute_command(command, payload, base_url))}
        if name == "lanforge_get_request":
            endpoint = require_str(arguments, "endpoint")
            query_params = arguments.get("query_params")
            base_url = arguments.get("base_url")
            if query_params is not None and not isinstance(query_params, dict):
                raise McpError(-32602, "query_params must be an object")
            return {"content": json_content(self.get_request(endpoint, query_params, base_url))}
        if name == "lanforge_set_active_manager":
            base_url = require_str(arguments, "base_url")
            return {"content": json_content(self.set_active_manager(base_url))}
        if name == "lanforge_get_active_manager":
            return {"content": json_content(self.get_active_manager())}
        if name == "lanforge_render_cli":
            command = require_str(arguments, "command")
            payload = arguments.get("parameters") or {}
            if not isinstance(payload, dict):
                raise McpError(-32602, "parameters must be an object")
            return {"content": json_content(self.render_cli(command, payload))}
        if name == "lanforge_list_scripts":
            query = arguments.get("query", "")
            return {"content": json_content(self.list_scripts(query))}
        if name == "lanforge_run_script":
            script_path = require_str(arguments, "script_path")
            args = arguments.get("args") or []
            if not isinstance(args, list):
                raise McpError(-32602, "args must be a list of strings")
            return {"content": json_content(self.run_script(script_path, args))}
        if name == "lanforge_script_info":
            script_path = require_str(arguments, "script_path")
            return {"content": json_content(self.script_info(script_path))}

        if self.expose_commands and isinstance(name, str) and name in self.direct_tools:
            command = self.direct_tools[name]
            if command not in self.commands:
                raise McpError(-32601, f"Unknown LANforge direct tool: {name}")
            return {"content": json_content(self.execute_command(command, arguments))}

        raise McpError(-32601, f"Unknown tool: {name}")

    def list_commands(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip().lower()
        category = str(arguments.get("category", "")).strip()
        limit = int(arguments.get("limit") or 50)
        limit = max(1, min(limit, 261))

        matches = []
        for command in self.commands.values():
            haystack = " ".join(
                [
                    command.get("name", ""),
                    command.get("description", ""),
                    command.get("category", ""),
                    command.get("metadata", {}).get("syntax", ""),
                ]
            ).lower()
            if query and query not in haystack:
                continue
            if category and command.get("category") != category:
                continue
            matches.append(
                {
                    "name": command.get("name"),
                    "description": command.get("description"),
                    "category": command.get("category"),
                    "syntax": command.get("metadata", {}).get("syntax"),
                }
            )
            if len(matches) >= limit:
                break

        return {
            "total_available": len(self.commands),
            "returned": len(matches),
            "commands": matches,
            "categories": self.dataset.get("metadata", {}).get("categories", []),
        }

    def command_info(self, command: str) -> dict[str, Any]:
        info = self.commands.get(command)
        if not info:
            raise McpError(-32602, f"Unknown LANforge command: {command}")
        return info

    def set_active_manager(self, base_url: str) -> dict[str, Any]:
        host, port = parse_ip_port(base_url)
        self.host = host
        self.port = port
        self.client.host = host
        sys.stderr.write(f"Switched default LANforge Manager to http://{host}:{port}\n")
        return {
            "ok": True,
            "message": f"Successfully switched default LANforge Manager target to http://{host}:{port}",
            "active_manager": {
                "base_url": f"http://{host}:{port}",
                "host": host,
                "port": port
            }
        }

    def get_active_manager(self) -> dict[str, Any]:
        return {
            "base_url": f"http://{self.host}:{self.port}",
            "host": self.host,
            "port": self.port,
            "ssh_user": self.client.user,
            "ssh_enabled": self.ssh_enabled
        }

    def execute_command(self, command: str, parameters: dict[str, Any], base_url_override: str | None = None) -> dict[str, Any]:
        # Resolve host and port
        host = self.host
        port = self.port
        if base_url_override:
            host, port = parse_ip_port(base_url_override)

        # Attempt direct HTTP call first
        sys.stderr.write(f"Executing '{command}' via LANforge HTTP API on http://{host}:{port}...\n")
        http_res = self.call_http_api(command, parameters, host, port)
        if http_res["ok"]:
            return http_res

        sys.stderr.write(f"HTTP Call failed (status={http_res.get('status')}). Reason: {http_res.get('response')}\n")
        if self.ssh_enabled and http_res.get("status") == -1:
            sys.stderr.write(f"Falling back to Telnet over SSH to {host}...\n")
            cli_info = self.render_cli(command, parameters)
            ssh_res = self.client.call(cli_info["cli"], host_override=host)
            return ssh_res

        return http_res

    def call_http_api(self, command: str, parameters: dict[str, Any], host: str, port: int) -> dict[str, Any]:
        url = f"http://{host}:{port}/cli-json/{command}"
        data = json.dumps(parameters).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status_code = resp.status
                content_type = resp.headers.get("Content-Type", "")
                raw_body = resp.read()
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                body = decode_response(raw_body, content_type)
                return {
                    "ok": status_code in (200, 201, 202),
                    "command": command,
                    "duration_ms": duration_ms,
                    "status": status_code,
                    "response": body,
                    "source": "HTTP_API"
                }
        except urllib.error.HTTPError as err:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            try:
                raw_body = err.read()
                body = decode_response(raw_body, err.headers.get("Content-Type", ""))
            except Exception:
                body = str(err)
            return {
                "ok": False,
                "command": command,
                "duration_ms": duration_ms,
                "status": err.code,
                "response": body,
                "source": "HTTP_API"
            }
        except Exception as e:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": False,
                "command": command,
                "duration_ms": duration_ms,
                "status": -1,
                "response": f"HTTP request failed: {str(e)}",
                "source": "HTTP_API"
            }

    def get_request(self, endpoint: str, query_params: dict[str, Any] = None, base_url_override: str | None = None) -> dict[str, Any]:
        host = self.host
        port = self.port
        if base_url_override:
            host, port = parse_ip_port(base_url_override)

        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = f"http://{host}:{port}{endpoint}"
        if query_params:
            url += "?" + urllib.parse.urlencode(query_params, doseq=True)

        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET"
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status_code = resp.status
                content_type = resp.headers.get("Content-Type", "")
                raw_body = resp.read()
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                body = decode_response(raw_body, content_type)
                return {
                    "ok": status_code == 200,
                    "endpoint": endpoint,
                    "duration_ms": duration_ms,
                    "status": status_code,
                    "response": body
                }
        except Exception as e:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": False,
                "endpoint": endpoint,
                "duration_ms": duration_ms,
                "status": -1,
                "response": f"HTTP GET failed: {str(e)}"
            }

    def list_scripts(self, query: str) -> dict[str, Any]:
        query = query.strip().lower()
        matches = []
        for rel_path, info in self.scripts_index.items():
            if not query or query in rel_path.lower() or query in info.get("description", "").lower():
                matches.append(info)
        return {
            "total_scripts": len(self.scripts_index),
            "returned": len(matches),
            "scripts": matches
        }

    def script_info(self, script_path: str) -> dict[str, Any]:
        """Get --help output for a script to inform the AI about valid arguments."""
        # Smart search: resolve partial name to full path
        target_path = Path(script_path)
        if not (self.scripts_dir / target_path).is_file():
            for rel_path in self.scripts_index:
                if rel_path.endswith(script_path) or Path(rel_path).name == target_path.name:
                    script_path = rel_path
                    break

        full_path = (self.scripts_dir / script_path).resolve()
        if not full_path.is_file():
            return {
                "ok": False,
                "error": f"Script not found: {script_path}",
                "available_scripts_hint": "Use lanforge_list_scripts to find available scripts."
            }

        index_entry = self.scripts_index.get(script_path, {})
        help_text = run_script_help(self.scripts_dir, script_path)

        return {
            "ok": True,
            "script_path": script_path,
            "description": index_entry.get("description", ""),
            "manager_ip": self.host,
            "usage_note": f"Always include '--mgr {self.host}' as the first argument when running this script.",
            "help_output": help_text
        }

    def run_script(self, script_path: str, args: list[str]) -> dict[str, Any]:
        # Smart search fallback for simple filenames (e.g., 'create_station.py' -> 'py-scripts/create_station.py')
        target_path = Path(script_path)
        if not (self.scripts_dir / target_path).is_file():
            found = False
            # Check script index
            for rel_path in self.scripts_index:
                if rel_path.endswith(script_path) or Path(rel_path).name == target_path.name:
                    script_path = rel_path
                    found = True
                    break
            if not found:
                # Direct filesystem search fallback
                try:
                    for p in self.scripts_dir.rglob(target_path.name):
                        if p.is_file():
                            script_path = str(p.relative_to(self.scripts_dir))
                            found = True
                            break
                except Exception:
                    pass

        full_path = (self.scripts_dir / script_path).resolve()
        if not full_path.is_file() or not full_path.is_relative_to(self.scripts_dir.resolve()):
            return {
                "ok": False,
                "error": f"Invalid script path: {script_path}. Must be relative to lanforge-scripts directory."
            }

        suffix = full_path.suffix.lower()
        if suffix == ".py":
            cmd = [sys.executable, str(full_path)]
        elif suffix == ".pl":
            cmd = ["perl", str(full_path)]
        elif suffix in (".sh", ".bash"):
            cmd = ["bash", str(full_path)]
        else:
            cmd = [str(full_path)]

        cmd.extend(args)

        # Build environments
        env = os.environ.copy()
        env["LF_SCRIPTS"] = str(self.scripts_dir)
        
        # Add to pythonpath
        pythonpath = [str(self.scripts_dir)]
        py_json_dir = self.scripts_dir / "py-json"
        if py_json_dir.exists():
            pythonpath.append(str(py_json_dir))
        lanforge_client_dir = self.scripts_dir / "lanforge_client"
        if lanforge_client_dir.exists():
            pythonpath.append(str(lanforge_client_dir))

        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            pythonpath.append(existing_pythonpath)

        env["PYTHONPATH"] = os.pathsep.join(pythonpath)

        sys.stderr.write(f"Executing: {' '.join(cmd)}\n")
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=120
            )
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "duration_ms": duration_ms,
                "stdout": result.stdout,
                "stderr": result.stderr
            }
        except subprocess.TimeoutExpired as err:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": False,
                "error": "Script timed out after 120 seconds",
                "duration_ms": duration_ms,
                "stdout": err.stdout or "",
                "stderr": err.stderr or ""
            }
        except Exception as e:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": False,
                "error": f"Failed to execute script: {str(e)}",
                "duration_ms": duration_ms
            }

    def render_cli(self, command: str, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            info = self.command_info(command)
        except McpError:
            info = {}

        syntax = info.get("metadata", {}).get("syntax") or command
        parts = syntax.split()
        arg_names = parts[1:]
        rendered = [command]
        
        last_index = -1
        for i, arg_name in enumerate(arg_names):
            if arg_name in parameters and parameters[arg_name] is not None:
                last_index = i
        
        for i in range(last_index + 1):
            arg_name = arg_names[i]
            val = parameters.get(arg_name)
            if val is not None:
                rendered.append(cli_quote(val))
            else:
                rendered.append("NA")

        return {
            "command": command,
            "syntax": syntax,
            "cli": " ".join(rendered),
            "note": "Intermediate missing arguments were filled with 'NA'. Trailing arguments omitted.",
        }

    # Resources Methods
    def resources_list(self) -> dict[str, Any]:
        return {
            "resources": [
                {
                    "uri": "lanforge://docs/cli",
                    "name": "LANforge CLI Reference Documentation",
                    "description": "Comprehensive reference guide for LANforge CLI syntax, positioning, and connection concepts.",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "lanforge://docs/api_guide",
                    "name": "LANforge HTTP JSON API Guide",
                    "description": "Detailed guide on using HTTP GET/POST endpoints, EIDs, and REST APIs.",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "lanforge://docs/python_sdk",
                    "name": "LANforge Python SDK Scripting Guide",
                    "description": "Guide on writing custom automation scripts using LANforge python libraries.",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "lanforge://docs/scripts",
                    "name": "LANforge Automation Script Index",
                    "description": "Dynamic index of all available python, perl, and bash scripts inside the lanforge-scripts folder.",
                    "mimeType": "text/markdown"
                }
            ]
        }

    def resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = require_str(params, "uri")
        
        if uri == "lanforge://docs/cli":
            text = self.get_cli_docs()
        elif uri == "lanforge://docs/api_guide":
            text = self.get_api_docs()
        elif uri == "lanforge://docs/python_sdk":
            text = self.get_python_sdk_docs()
        elif uri == "lanforge://docs/scripts":
            text = self.get_scripts_docs()
        elif uri.startswith("lanforge://scripts/view/"):
            rel_path = uri[len("lanforge://scripts/view/"):]
            full_path = (self.scripts_dir / rel_path).resolve()
            if full_path.is_file() and full_path.is_relative_to(self.scripts_dir.resolve()):
                try:
                    with full_path.open("r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except Exception as e:
                    raise McpError(-32602, f"Failed to read script file: {str(e)}")
            else:
                raise McpError(-32602, f"Invalid script file path or file not found: {rel_path}")
        else:
            raise McpError(-32602, f"Unknown resource URI: {uri}")
            
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": text
                }
            ]
        }

    def get_cli_docs(self) -> str:
        return """# LANforge CLI Reference Documentation

## Overview
LANforge systems can be controlled via a command-line interface (CLI) using a text-based TCP socket connection (usually on port 4001).

## Telnet Connection
To connect directly to the CLI interface:
```bash
telnet <lanforge_ip> 4001
```

## CLI Syntax and Command Rules
1. **Positional Arguments**: Arguments are strictly sensitive to position. You cannot skip arguments.
2. **NA (Not Applicable)**: For optional or unused intermediate parameters, use `NA` to instruct LANforge to ignore them.
3. **Trailing Arguments**: You can omit trailing arguments; they will default to `NA`.
4. **Quoting**:
   - Single-word arguments do not require quotes.
   - Multi-word arguments or values containing spaces or single quotes must be surrounded by single quotes.
   - To include a single quote inside a single-quoted argument, use double single quotes (`''`).
5. **Number Formats**: Numbers can be entered in decimal or HEX (prefixed with `0x`). Do not prepend `0` to decimals or they might be parsed as octals.

## Entity Identifiers (EID)
LANforge identifies entities (like ports, cross-connects, and attenuators) using EIDs:
- **Port EID**: `shelf.resource.port_name` (e.g., `1.1.wlan0` or `1.wlan0` if shelf is 1).
- **Attenuator EID**: `shelf.resource.attenuator_id` (e.g., `1.1.4314`).
"""

    def get_api_docs(self) -> str:
        return """# LANforge HTTP JSON API Guide

## Overview
LANforge provides an HTTP REST API (usually on port 8080) served by the LANforge GUI or manager daemon. This API supports both querying system state (GET) and configuring system state (POST).

## Querying System State (HTTP GET)
- **Base URL**: `http://<lanforge_ip>:8080`
- **Endpoints**: correspond directly to LANforge GUI tabs:
  - `/ports`: Returns a list of physical and virtual ports.
  - `/cx`: Returns cross-connect information.
  - `/endp`: Returns endpoint details.
  - `/wps`: Returns WanPath emulations.
- **Example GET request**:
  ```http
  GET /ports/1/1/wlan0?fields=alias,ip,mac
  Accept: application/json
  ```

## Configuring System State (HTTP POST)
- **Endpoint**: `http://<lanforge_ip>:8080/cli-json/<command>`
- **Payload**: JSON object representing parameters. Key names correspond to the arguments of the command.
- **Example POST request**:
  ```http
  POST /cli-json/add_sta
  Content-Type: application/json
  
  {
    "shelf": 1,
    "resource": 1,
    "radio": "wiphy0",
    "sta_name": "sta000",
    "flags": 0
  }
  ```
"""

    def get_python_sdk_docs(self) -> str:
        return """# LANforge Python SDK Scripting Guide

## Overview
The LANforge Python SDK provides a library for automating testbeds using the HTTP/JSON API. It consists of the following key modules:
1. `lanforge_client.lanforge_api`: Automatically generated wrappers for all CLI commands.
2. `py-json.realm`: High-level automation classes (like `StationProfile`, `L3CXProfile`) for creating collections of stations or traffic endpoints.
3. `py-json.LANforge.lfcli_base`: Base class `LFCliBase` for command-line script structures.

## Writing a Script with `LFSession`
The recommended modern approach uses `LFSession` and the autogenerated methods.

```python
from lanforge_client import lanforge_api

# Initialize the session
session = lanforge_api.LFSession(lfclient_url="http://localhost:8080")

# Get command and query interfaces
command = session.get_command()
query = session.get_query()

# 1. Query ports
ports = query.get_port(eid_list=["1.1.wlan0"])
print("Port Details:", ports)

# 2. Call a configuration command (e.g., add_sta)
# This corresponds to POST /cli-json/add_sta
response = command.post_add_sta(
    shelf=1,
    resource=1,
    radio="wiphy0",
    sta_name="sta001",
    flags=0
)
print("Command Response:", response)
```

## Writing a Script with `LFCliBase` and `Realm`
If you need high-level profile management, use `Realm` from `py-json/realm.py`.

```python
import sys
from py_json.realm import Realm

class MyTest(Realm):
    def __init__(self, host, port):
        super().__init__(host, port)
        # Now you can use high-level helpers
        # self.station_profile, self.l3_cx_profile, etc.
```
"""

    def get_scripts_docs(self) -> str:
        lines = [
            "# LANforge Script Automation Index",
            "",
            "This is a live index of scripts available in the `lanforge-scripts` directory.",
            "To view the code of any script, load `lanforge://scripts/view/<relative_path>`.",
            "",
            "| Script Path | Description |",
            "| --- | --- |"
        ]
        for rel_path, info in sorted(self.scripts_index.items()):
            desc = info.get("description", "").replace("\n", " ").strip()
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(f"| [{rel_path}](lanforge://scripts/view/{rel_path}) | {desc} |")
        return "\n".join(lines)

    # Prompts Methods
    def prompts_list(self) -> dict[str, Any]:
        return {
            "prompts": [
                {
                    "name": "lanforge_expert",
                    "description": "System instructions to act as an expert LANforge test automation engineer. Includes the active manager IP and tool usage workflow.",
                    "arguments": []
                },
                {
                    "name": "lanforge_generate_script",
                    "description": "Template to generate a custom Python automation script using lanforge_api.",
                    "arguments": [
                        {
                            "name": "task_description",
                            "description": "A description of what the automation script should do.",
                            "required": True
                        }
                    ]
                },
                {
                    "name": "lanforge_troubleshoot",
                    "description": "Template to diagnose a failed command or script run in LANforge.",
                    "arguments": [
                        {
                            "name": "error_message",
                            "description": "The console error output or exception details.",
                            "required": True
                        }
                    ]
                }
            ]
        }

    def prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = require_str(params, "name")
        arguments = params.get("arguments") or {}
        
        if name == "lanforge_expert":
            description = "Act as a LANforge Expert"
            text = (
                f"You are an expert LANforge network test automation engineer.\n\n"
                f"ACTIVE LANFORGE MANAGER: {self.host}:{self.port}\n"
                f"Always pass --mgr {self.host} to every py-script you run via lanforge_run_script.\n\n"
                "=== MANDATORY WORKFLOW FOR RUNNING SCRIPTS ===\n"
                "1. Call lanforge_script_info with the script path to get the exact --help output.\n"
                "2. Read the help output to identify required and optional arguments.\n"
                "3. Call lanforge_run_script with script_path and a correct args list including --mgr.\n"
                "4. Parse stdout for PASSED/FAILED to report results to the user.\n\n"
                "=== TOOL REFERENCE ===\n"
                "- lanforge_list_scripts(query): Search available scripts by name or description.\n"
                "- lanforge_script_info(script_path): Get --help output with all valid argument names and types.\n"
                "- lanforge_run_script(script_path, args): Execute a script with arguments.\n"
                "- lanforge_get_request(endpoint): Query live state. Useful endpoints:\n"
                "    /ports -> all ports and stations\n"
                "    /ports/1/1/<name> -> single port details (ip_addr, alias, signal, mode)\n"
                "    /cx -> cross-connects\n"
                "    /endp -> endpoints\n"
                "- lanforge_call(command, parameters): Execute a raw CLI command via REST POST.\n"
                "- lanforge_command_info(command): Get exact schema for a raw CLI command BEFORE calling it.\n"
                "- lanforge_list_commands(query): Search available raw CLI commands.\n\n"
                "=== RULES ===\n"
                "- NEVER guess script arguments. Always use lanforge_script_info first.\n"
                "- NEVER use Python SDK method names (post_add_sta etc.) in lanforge_call - use raw command names (add_sta).\n"
                "- Prefer scripts over raw CLI commands for complex operations.\n"
                "- Minimize tool calls. Don't query data you already have.\n"
            )
        elif name == "lanforge_generate_script":
            task = arguments.get("task_description")
            if not task:
                raise McpError(-32602, "task_description argument is required")
            description = f"Generate LANforge Script: {task}"
            text = (f"Write a complete Python automation script using `lanforge_api` to achieve the "
                    f"following task:\n\n{task}\n\n"
                    f"Guidelines:\n"
                    f"1. Refer to the Python SDK documentation at `lanforge://docs/python_sdk` for the coding structure.\n"
                    f"2. Use `LFSession` to interact with LANforge.\n"
                    f"3. Make sure to import required modules from `py-json.LANforge` or `lanforge_client`.\n"
                    f"4. Provide clean comments and error handling.")
        elif name == "lanforge_troubleshoot":
            error = arguments.get("error_message")
            if not error:
                raise McpError(-32602, "error_message argument is required")
            description = "Troubleshoot LANforge Error"
            text = (f"Analyze the following error output from LANforge:\n\n{error}\n\n"
                    f"Provide a root cause analysis and a list of steps to resolve the issue. "
                    f"You may query the system state using `lanforge_get_request` or look up relevant command "
                    f"schemas using `lanforge_command_info` to verify parameter correctness.")
        else:
            raise McpError(-32602, f"Unknown prompt: {name}")
            
        return {
            "description": description,
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": text
                    }
                }
            ]
        }


def require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise McpError(-32602, f"{key} is required and must be a string")
    return value


def run_stdio(server: LanforgeMcpServer) -> None:
    # Backup sys.stdout and redirect stdout to stderr to avoid stdout pollution
    sys_stdout_original = sys.stdout
    sys.stdout = sys.stderr

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            else:
                response = server.handle(request)
            
            if response is not None:
                sys_stdout_original.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys_stdout_original.flush()
    except Exception as e:
        sys.stderr.write(f"Fatal error in stdio loop: {e}\n")
        traceback.print_exc(file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="LANforge MCP server")
    parser.add_argument("--dataset", default=str(DATASET_PATH), help="Path to lanforge_mcp_dataset.json")
    parser.add_argument("--scripts", default="", help="Path to lanforge-scripts directory")
    args = parser.parse_args()

    # Locate dataset
    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        # Try relative to script
        dataset_path = Path(__file__).parent / "lanforge_mcp_dataset.json"

    dataset = load_dataset(dataset_path)

    # Locate scripts dir
    scripts_dir_str = args.scripts or os.getenv("LANFORGE_SCRIPTS_DIR", "")
    if scripts_dir_str:
        scripts_dir = Path(scripts_dir_str)
    else:
        scripts_dir = Path(__file__).parent / "lanforge-scripts"

    base_url = os.getenv("LANFORGE_BASE_URL", DEFAULT_BASE_URL)
    host, port = parse_ip_port(base_url)

    ssh_user = os.getenv("LANFORGE_SSH_USER", "root")
    timeout = float(os.getenv("LANFORGE_TIMEOUT", "30"))
    expose_commands = os.getenv("LANFORGE_EXPOSE_COMMAND_TOOLS", "").lower() in {"1", "true", "yes"}

    client = LanforgeClient(host, ssh_user, timeout)
    server = LanforgeMcpServer(dataset, scripts_dir, client, expose_commands)
    
    run_stdio(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
