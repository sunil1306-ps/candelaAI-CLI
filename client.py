import os
import json
import httpx
from urllib.parse import urlparse
from google import genai
from google.genai import types
from mcp_client import McpManager
from rich.panel import Panel
from rich.live import Live
class CurrentTurn:
    def __init__(self):
        self.user_question = ""
        self.tool_calls = []  # List of dict: {"name": ..., "args": ..., "output": ...}
        self.assistant_response = ""
        self.is_full = False

class CandelaSession:
    def __init__(self, provider, session_obj):
        self.provider = provider
        self.session_obj = session_obj  # Gemini Chat or OpenAI message list
        self.tool_history = []  # List of tuples (tool_name, full_output)
        self.current_turn = CurrentTurn()

FALLBACK_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemma-4.31b-it",
    "gemma-4-26b-a4b-it"
]

def read_char():
    import sys
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            msvcrt.getch()
            return None
        return ch
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch.encode('utf-8')

class CandelaClient:
    def __init__(self, config):
        self.config = config
        self.provider = config.get("provider", "openai")
        self.openai_base_url = config.get("openai_base_url", "")
        self.openai_api_key = config.get("openai_api_key", "")
        self.openai_model = config.get("openai_model", "")
        self.gemini_api_key = config.get("gemini_api_key", "")
        self.gemini_model = config.get("gemini_model", "gemini-3.1-flash-lite")
        
        # Initialize Google GenAI client if provider is gemini
        self.gemini_client = None
        if self.provider == "gemini":
            api_key = self.gemini_api_key or os.environ.get("GEMINI_API_KEY")
            self.gemini_client = genai.Client(api_key=api_key)

    def create_session(self):
        if self.provider == "gemini":
            if not self.gemini_client:
                import os
                api_key = self.gemini_api_key or os.environ.get("GEMINI_API_KEY")
                self.gemini_client = genai.Client(api_key=api_key)
            session_obj = self.gemini_client.chats.create(model=self.gemini_model)
        else:
            # For OpenAI, session is a list of messages
            session_obj = []
        return CandelaSession(self.provider, session_obj)

    async def send_message(self, session, message_text, mcp_manager: McpManager, console):
        if self.provider == "gemini":
            return await self._send_gemini(session, message_text, mcp_manager, console)
        else:
            return await self._send_openai(session, message_text, mcp_manager, console)

    def _build_system_prompt(self, mcp_manager: McpManager) -> str:
        # Resolve the LANforge manager IP from MCP config
        manager_ip = "192.168.215.183"
        if mcp_manager:
            lf_cfg = mcp_manager.servers_config.get("lanforge", {})
            base_url = lf_cfg.get("env", {}).get("LANFORGE_BASE_URL", "")
            if base_url:
                try:
                    manager_ip = urlparse(base_url).hostname or manager_ip
                except Exception:
                    pass

        return (
            f"You are CandelaAI, an expert LANforge network test automation assistant.\n\n"
            f"ACTIVE LANFORGE MANAGER: {manager_ip}\n"
            f"Always pass --mgr {manager_ip} to every py-script via lanforge_run_script.\n\n"
            "=== MANDATORY WORKFLOW FOR RUNNING SCRIPTS ===\n"
            "1. Call lanforge_script_info(script_path) FIRST to get exact --help output before running any script.\n"
            "2. Read the help output to identify required vs optional args and their exact flag names.\n"
            "3. Call lanforge_run_script(script_path, args) with a complete args list including --mgr.\n"
            "4. Parse stdout for PASSED/FAILED lines and report the full result to the user.\n\n"
            "=== TOOL REFERENCE ===\n"
            "- lanforge_list_scripts(query): Discover available scripts by name or description.\n"
            "- lanforge_script_info(script_path): Get --help output with ALL valid argument names and types. Use before every run.\n"
            "- lanforge_run_script(script_path, args): Execute a script. args is a flat list of strings.\n"
            "- lanforge_get_request(endpoint): Query live state.\n"
            "    /ports -> all ports/stations\n"
            "    /ports/1/1/<name>?fields=ip_addr,alias,signal -> single port status\n"
            "    /cx -> cross-connects   /endp -> endpoints\n"
            "- lanforge_call(command, parameters): Raw CLI command via REST. Use lanforge_command_info first.\n"
            "- lanforge_command_info(command): Get exact schema for a raw CLI command before calling it.\n\n"
            "=== RULES ===\n"
            "- NEVER guess script argument names. Always call lanforge_script_info first.\n"
            "- NEVER use SDK method names (post_add_sta) in lanforge_call. Use raw names only (add_sta).\n"
            "- Prefer scripts over raw CLI for complex ops (station creation, traffic, capacity tests).\n"
            "- Minimize tool calls. Do not re-query data you already have.\n"
            "- After any script completes, always include PASSED/FAILED status in your response.\n"
        )


    async def _send_gemini(self, session, message_text, mcp_manager: McpManager, console):
        # 1. Prepare tools
        gemini_tools = mcp_manager.get_tools_schemas_for_gemini() if mcp_manager else []
        
        # 2. Build config with system instruction and tools
        config_args = {}
        if gemini_tools:
            config_args["tools"] = gemini_tools
        config_args["system_instruction"] = self._build_system_prompt(mcp_manager)
        config = types.GenerateContentConfig(**config_args)
        
        import asyncio
        loop = asyncio.get_event_loop()
        
        models_to_try = [self.gemini_model] + [m for m in FALLBACK_MODELS if m != self.gemini_model]
        current_model_idx = 0
        chat_session = session.session_obj
        
        def run_with_fallback(send_payload):
            nonlocal chat_session, current_model_idx
            while True:
                try:
                    return chat_session.send_message(send_payload, config=config)
                except Exception as e:
                    current_model_idx += 1
                    if current_model_idx >= len(models_to_try):
                        raise e
                    
                    next_model = models_to_try[current_model_idx]
                    console.print(f"[bold yellow]\n[Fallback] Model call failed with {models_to_try[current_model_idx-1]}. Trying fallback model: {next_model}...[/bold yellow]")
                    
                    try:
                        history = chat_session.get_history()
                    except Exception:
                        history = []
                    
                    chat_session = self.gemini_client.chats.create(model=next_model, history=history)
                    session.session_obj = chat_session
        
        # Send message to Gemini
        response = await loop.run_in_executor(
            None,
            lambda: run_with_fallback(message_text)
        )
        
        while response.function_calls:
            parts = []
            for function_call in response.function_calls:
                name = function_call.name
                args = function_call.args
                args_dict = dict(args) if args else {}
                
                tool_call_details = f"[bold cyan]Tool Name:[/bold cyan] {name}\n[bold cyan]Arguments:[/bold cyan] {json.dumps(args_dict, indent=2)}"
                
                try:
                    tool_result = await mcp_manager.call_tool(name, args_dict)
                    result_text = ""
                    for content_item in tool_result.content:
                        if hasattr(content_item, "text"):
                            result_text += content_item.text
                        elif isinstance(content_item, dict) and "text" in content_item:
                            result_text += content_item["text"]
                        else:
                            result_text += str(content_item)
                except Exception as e:
                    result_text = f"Error executing tool: {e}"
                
                # Record to tool history
                session.tool_history.append((name, result_text))
                session.current_turn.tool_calls.append({"name": name, "args": args_dict, "output": result_text})
                
                # Truncate output representation
                is_truncated = len(result_text) > 300
                display_output = result_text[:300]
                if is_truncated:
                    display_output += "\n\n[dim]... [Output truncated. Press Ctrl+O in prompt to toggle full logs] [/dim]"
                
                box_content = f"{tool_call_details}\n\n[bold green]Output:[/bold green]\n{display_output}"
                console.print(Panel(box_content, title="MCP Tool Execution", border_style="cyan"))
                
                part = types.Part.from_function_response(
                    name=name,
                    response={"result": result_text}
                )
                parts.append(part)
            
            # Send all responses back in one turn
            response = await loop.run_in_executor(
                None,
                lambda: run_with_fallback(parts)
            )
        
        return response.text
 
    async def _send_openai(self, session, message_text, mcp_manager: McpManager, console):
        messages = session.session_obj
        # 1. Append system message if not present
        if not any(msg.get("role") == "system" for msg in messages):
            messages.append({"role": "system", "content": self._build_system_prompt(mcp_manager)})
            
        messages.append({"role": "user", "content": message_text})
        
        # 2. Get tools
        tools = mcp_manager.get_tools_schemas_for_openai() if mcp_manager else []
        
        # 3. Request loop
        while True:
            payload = {
                "model": self.openai_model,
                "messages": messages,
                "max_tokens": 4096
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
                
            headers = {
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": "application/json"
            }
            
            # Set OpenRouter specific headers if targeting openrouter
            if "openrouter.ai" in self.openai_base_url:
                headers["HTTP-Referer"] = "https://candela-ai.tech"
                headers["X-Title"] = "CandelaAI CLI"
            
            async with httpx.AsyncClient(timeout=180.0) as http_client:
                response = await http_client.post(
                    f"{self.openai_base_url}/chat/completions",
                    json=payload,
                    headers=headers
                )
                
                if response.status_code != 200:
                    raise RuntimeError(f"OpenAI-compatible API error: {response.status_code} - {response.text}")
                
                res_json = response.json()
                
                # Guard against missing choices
                if not res_json.get("choices"):
                    error_msg = res_json.get("error", {}).get("message", "No choices returned from API")
                    raise RuntimeError(f"OpenAI API returned no choices: {error_msg}")
                
                choice = res_json["choices"][0]
                finish_reason = choice.get("finish_reason")
                message = choice["message"]
                
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls")
                
                if not tool_calls:
                    # Append assistant's response to history and return
                    messages.append(message)
                    return content or "(No response)"
                
                # Append the tool call message to history
                messages.append(message)
                
                # Execute each tool call
                for tool_call in tool_calls:
                    func = tool_call["function"]
                    name = func["name"]
                    args_str = func["arguments"]
                    try:
                        args = json.loads(args_str)
                    except Exception:
                        args = {}
                        
                    tool_call_details = f"[bold cyan]Tool Name:[/bold cyan] {name}\n[bold cyan]Arguments:[/bold cyan] {json.dumps(args, indent=2)}"
                    
                    try:
                        tool_result = await mcp_manager.call_tool(name, args)
                        result_text = ""
                        for content_item in tool_result.content:
                            if hasattr(content_item, "text"):
                                result_text += content_item.text
                            elif isinstance(content_item, dict) and "text" in content_item:
                                result_text += content_item["text"]
                            else:
                                result_text += str(content_item)
                    except Exception as e:
                        result_text = f"Error executing tool: {e}"
                    
                    # Record to tool history
                    session.tool_history.append((name, result_text))
                    session.current_turn.tool_calls.append({"name": name, "args": args, "output": result_text})
                    
                    # Truncate output representation
                    is_truncated = len(result_text) > 300
                    display_output = result_text[:300]
                    if is_truncated:
                        display_output += "\n\n[dim]... [Output truncated. Press Ctrl+O in prompt to toggle full logs] [/dim]"
                    
                    box_content = f"{tool_call_details}\n\n[bold green]Output:[/bold green]\n{display_output}"
                    console.print(Panel(box_content, title="MCP Tool Execution", border_style="cyan"))
                    
                    # Append tool completion response to history
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": name,
                        "content": result_text
                    })
            
            # Continue loop after tool calls - send messages back to get final response
