import asyncio
import os
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack
from google.genai import types

def json_schema_to_gemini_schema(schema) -> dict:
    if not isinstance(schema, dict):
        if isinstance(schema, list):
            return [json_schema_to_gemini_schema(item) for item in schema]
        return schema
    
    result = {}
    for k, v in schema.items():
        if k in ("$schema", "additionalProperties", "additional_properties"):
            continue
        if k == "type" and isinstance(v, str):
            result[k] = v.upper()
        else:
            result[k] = json_schema_to_gemini_schema(v)
    return result

class McpManager:
    def __init__(self, servers_config, mode="local", ssh_host=None, ssh_user="root"):
        self.servers_config = servers_config
        self.mode = mode
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.exit_stack = AsyncExitStack()
        self.sessions = {}  # server_name -> ClientSession
        self.tools = {}     # tool_name -> (server_name, tool_obj)

    async def connect_server(self, name, config):
        command = config.get("command", "python")
        args = config.get("args", [])
        env = os.environ.copy()
        if "env" in config:
            env.update(config["env"])
        
        if self.mode == "remote" and self.ssh_host:
            # Dynamically map the project's mcp folder path to the remote path
            local_mcp_dir = str(Path(__file__).parent / "lanforge_mcp").replace("\\", "/")
            remote_paths = config.get("remote_paths", {
                local_mcp_dir + "/lanforge-scripts": "/home/lanforge/lanforge-scripts",
                local_mcp_dir: "/home/lanforge/Desktop/sunil/candela_cli/lanforge",
                "C:/Users/Sunil.S/Desktop/Test Data/lanforge/lanforge-scripts": "/home/lanforge/lanforge-scripts",
                "C:/Users/Sunil.S/Desktop/Test Data/lanforge": "/home/lanforge/Desktop/sunil/candela_cli/lanforge",
                "C:\\Users\\Sunil.S\\Desktop\\Test Data\\lanforge": "/home/lanforge/Desktop/sunil/candela_cli/lanforge",
            })
            
            # Translate all args from Windows local paths to remote Linux paths
            remote_args = []
            for arg in args:
                translated = arg.replace("\\", "/")
                matched = False
                for local_prefix, remote_prefix in remote_paths.items():
                    local_norm = local_prefix.replace("\\", "/")
                    if translated.startswith(local_norm):
                        translated = remote_prefix + translated[len(local_norm):]
                        matched = True
                        break
                remote_args.append(translated)
            
            # Setup command for SSH stdio tunneling
            command = "ssh"
            
            # Set remote environment variables
            remote_env_str = ""
            if "env" in config:
                for k, v in config["env"].items():
                    remote_env_str += f"export {k}={v}; "
            
            # Quote args with spaces for shell safety
            quoted_args = []
            for a in remote_args:
                if " " in a:
                    quoted_args.append(f"'{a}'")
                else:
                    quoted_args.append(a)
            
            remote_cmd_str = " ".join(quoted_args)
            payload = f"{remote_env_str}python3 {remote_cmd_str}"
            args = ["-o", "BatchMode=yes", f"{self.ssh_user}@{self.ssh_host}", payload]
            
            # Remove LANFORGE_BASE_URL on local side to prevent mismatch
            env.pop("LANFORGE_BASE_URL", None)
            
            # We use stderr printing to not pollute stdout/stdin streams
            print(f"[Remote SSH Connect] Tunneling MCP server '{name}' via SSH: {command} {' '.join(args)}", file=sys.stderr)
        else:
            if command == "python" and sys.platform != "win32":
                command = "python3"
        
        try:
            # Connect to server
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=env
            )
            read, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            
            self.sessions[name] = session
            
            # List tools and cache them
            tools_response = await session.list_tools()
            for tool in tools_response.tools:
                # Store tool and its origin server
                self.tools[tool.name] = (name, tool)
                
            return True
        except Exception as e:
            print(f"Error connecting to MCP server {name}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            return False

    async def connect_all(self):
        connected = []
        for name, config in self.servers_config.items():
            ok = await self.connect_server(name, config)
            if ok:
                connected.append(name)
        return connected

    def get_tools_schemas_for_openai(self):
        openai_tools = []
        for name, (server_name, tool) in self.tools.items():
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            })
        return openai_tools

    def get_tools_schemas_for_gemini(self):
        declarations = []
        for name, (server_name, tool) in self.tools.items():
            # Clean and translate the schema recursively
            gemini_schema = json_schema_to_gemini_schema(tool.inputSchema)
            
            decl = types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=gemini_schema
            )
            declarations.append(decl)
        
        if declarations:
            return [types.Tool(function_declarations=declarations)]
        return []

    async def call_tool(self, tool_name, arguments):
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found")
        
        # Automatically inject --mgr for lanforge scripts if missing
        if tool_name == "lanforge_run_script" and isinstance(arguments, dict):
            args = arguments.get("args", [])
            # Check if any manager parameter is specified
            has_mgr = any(x in args for x in ["--mgr", "-m", "--lfmgr"])
            if not has_mgr:
                manager_ip = "127.0.0.1"
                lanforge_cfg = self.servers_config.get("lanforge", {})
                env = lanforge_cfg.get("env", {})
                base_url = env.get("LANFORGE_BASE_URL")
                if base_url:
                    from urllib.parse import urlparse
                    try:
                        parsed = urlparse(base_url)
                        manager_ip = parsed.hostname or "127.0.0.1"
                    except Exception:
                        pass
                
                if manager_ip and manager_ip != "127.0.0.1":
                    # Inject --mgr at the beginning of arguments
                    arguments["args"] = ["--mgr", manager_ip] + args
                    # Import sys to print to stderr for debug clarity
                    import sys
                    print(f"[Auto-inject] Injected --mgr {manager_ip} into script arguments.", file=sys.stderr)
        
        server_name, tool = self.tools[tool_name]
        session = self.sessions[server_name]
        
        result = await session.call_tool(tool_name, arguments)
        return result

    async def close(self):
        await self.exit_stack.aclose()
