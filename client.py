import os
import json
import httpx
from urllib.parse import urlparse
from google import genai
from google.genai import types
from mcp_client import McpManager

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
            return self.gemini_client.chats.create(model=self.gemini_model)
        else:
            # For OpenAI, session is a list of messages
            return []

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


    async def _send_gemini(self, chat_session, message_text, mcp_manager: McpManager, console):
        # 1. Prepare tools
        gemini_tools = mcp_manager.get_tools_schemas_for_gemini() if mcp_manager else []
        
        # 2. Build config with system instruction and tools
        config_args = {}
        if gemini_tools:
            config_args["tools"] = gemini_tools
        config_args["system_instruction"] = self._build_system_prompt(mcp_manager)
        config = types.GenerateContentConfig(**config_args)
        
        # Send message to Gemini
        # Run in executor because google-genai is synchronous for standard generate/chat
        # Let's import threadpool or run in executor to keep it async friendly
        import asyncio
        loop = asyncio.get_event_loop()
        
        response = await loop.run_in_executor(
            None,
            lambda: chat_session.send_message(message_text, config=config)
        )
        
        while response.function_calls:
            parts = []
            for function_call in response.function_calls:
                name = function_call.name
                args = function_call.args
                args_dict = dict(args) if args else {}
                
                console.print(f"[bold cyan][AI Tool Call][/bold cyan] {name}({args_dict})")
                
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
                    console.print(f"[bold red][Tool Error][/bold red] {e}")
                
                console.print(f"[bold green][Tool Output][/bold green] {result_text[:500]}..." if len(result_text) > 500 else f"[bold green][Tool Output][/bold green] {result_text}")
                
                part = types.Part.from_function_response(
                    name=name,
                    response={"result": result_text}
                )
                parts.append(part)
            
            # Send all responses back in one turn
            response = await loop.run_in_executor(
                None,
                lambda: chat_session.send_message(parts, config=config)
            )
        
        return response.text
 
    async def _send_openai(self, messages, message_text, mcp_manager: McpManager, console):
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
                        
                    console.print(f"[bold cyan][AI Tool Call][/bold cyan] {name}({args})")
                    
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
                        console.print(f"[bold red][Tool Error][/bold red] {e}")
                        
                    console.print(f"[bold green][Tool Output][/bold green] {result_text[:500]}..." if len(result_text) > 500 else f"[bold green][Tool Output][/bold green] {result_text}")
                    
                    # Append tool completion response to history
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": name,
                        "content": result_text
                    })
            
            # Continue loop after tool calls - send messages back to get final response
