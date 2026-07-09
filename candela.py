import click
import asyncio
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from config import load_config, save_config, CONFIG_FILE, get_default_mcp_config
from mcp_client import McpManager
from client import CandelaClient

console = Console()

@click.group()
def cli():
    """CandelaAI CLI - Your Personal Agentic Coder CLI"""
    pass

@cli.command()
@click.option("--provider", type=click.Choice(["gemini", "openai"]), help="LLM Provider")
@click.option("--gemini-key", help="Google Gemini API Key")
@click.option("--openai-key", help="OpenAI / OpenRouter API Key")
@click.option("--openai-url", help="OpenAI-compatible Base URL")
@click.option("--openai-model", help="OpenAI-compatible Model Name")
@click.option("--gemini-model", help="Google Gemini Model Name")
@click.option("--setup-mcp", is_flag=True, default=False, help="Set up LANforge MCP server paths")
def configure(provider, gemini_key, openai_key, openai_url, openai_model, gemini_model, setup_mcp):
    """Configure CandelaAI settings (API keys, model, and LANforge MCP paths)"""
    config = load_config()

    if not (provider or gemini_key or openai_key or openai_url or openai_model or gemini_model or setup_mcp):
        console.print("[bold cyan]Configure CandelaAI Settings[/bold cyan]")
        provider = click.prompt("LLM Provider", type=click.Choice(["gemini", "openai"]), default=config.get("provider", "gemini"))

        if provider == "gemini":
            gemini_key = click.prompt("Gemini API Key", default=config.get("gemini_api_key", ""), show_default=False)
            gemini_model = click.prompt("Gemini Model", default=config.get("gemini_model", "gemini-3.1-flash-lite"))
        else:
            openai_key = click.prompt("OpenAI API Key", default=config.get("openai_api_key", ""), show_default=False)
            openai_url = click.prompt("OpenAI Base URL", default=config.get("openai_base_url", "https://openrouter.ai/api/v1"))
            openai_model = click.prompt("OpenAI Model", default=config.get("openai_model", "qwen/qwen3-coder:free"))

        setup_mcp = click.confirm("Configure LANforge MCP server paths?", default=not bool(config.get("mcp_servers")))

    if provider:
        config["provider"] = provider
    if gemini_key is not None:
        config["gemini_api_key"] = gemini_key
    if openai_key is not None:
        config["openai_api_key"] = openai_key
    if openai_url is not None:
        config["openai_base_url"] = openai_url
    if openai_model is not None:
        config["openai_model"] = openai_model
    if gemini_model is not None:
        config["gemini_model"] = gemini_model

    if setup_mcp:
        console.print("\n[bold cyan]LANforge MCP Server Setup[/bold cyan]")
        console.print("[dim]Enter the paths to the LANforge files on THIS machine.[/dim]\n")

        existing = config.get("mcp_servers", {}).get("lanforge", {})
        existing_args = existing.get("args", [])
        existing_env  = existing.get("env", {})

        def _extract_arg(args, flag):
            try:
                return args[args.index(flag) + 1]
            except (ValueError, IndexError):
                return ""

        cur_server  = existing_args[0] if existing_args else ""
        cur_dataset = _extract_arg(existing_args, "--dataset")
        cur_scripts = _extract_arg(existing_args, "--scripts")
        cur_url     = existing_env.get("LANFORGE_BASE_URL", "http://192.168.215.183:8080")

        server_py = click.prompt("Path to server.py",                        default=cur_server  or "")
        dataset   = click.prompt("Path to lanforge_mcp_dataset.json",        default=cur_dataset or "")
        scripts   = click.prompt("Path to lanforge-scripts directory",        default=cur_scripts or "")
        mgr_url   = click.prompt("LANforge Manager URL (http://<ip>:8080)",   default=cur_url)

        if server_py and dataset and scripts:
            config["mcp_servers"] = get_default_mcp_config(server_py, dataset, scripts, mgr_url)
            console.print("[green]  MCP server configured.[/green]")
        else:
            console.print("[yellow]  Skipped MCP setup — incomplete paths provided.[/yellow]")

    save_config(config)
    console.print("[bold green][Success] Configuration saved successfully![/bold green]")
    console.print(f"[dim]Config stored at: {CONFIG_FILE}[/dim]")

async def wait_for_esc(cancel_event: asyncio.Event):
    try:
        import msvcrt
        while not cancel_event.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b':  # ESC key
                    cancel_event.set()
                    return
            await asyncio.sleep(0.05)
    except ImportError:
        # Non-Windows fallback
        pass

async def chat_async(mode=None, ssh_host=None, ssh_user="root"):
    config = load_config()
    
    # Verify we have keys
    if config["provider"] == "gemini" and not config.get("gemini_api_key"):
        import os
        if not os.environ.get("GEMINI_API_KEY"):
            console.print("[bold red]Error: Gemini API key is not configured. Run `candela configure` or set GEMINI_API_KEY in environment.[/bold red]")
            return
            
    if config["provider"] == "openai" and not config.get("openai_api_key"):
        import os
        if not os.environ.get("OPENAI_API_KEY"):
            console.print("[bold red]Error: OpenAI API key is not configured. Run `candela configure` or set OPENAI_API_KEY in environment.[/bold red]")
            return

    if not mode:
        mode = click.prompt("Run mode", type=click.Choice(["local", "remote"]), default="local")
    
    if mode == "remote" and not ssh_host:
        ssh_host = click.prompt("SSH Host (LANforge Manager IP)", default="192.168.215.183")
        ssh_user = click.prompt("SSH User", default="root")

    console.print(Panel(f"[bold magenta]** CandelaAI Agentic CLI ({mode.upper()} mode) **[/bold magenta]\n[dim]Initializing chat session with tools...[/dim]", expand=False))
    
    # Initialize MCP Manager
    mcp_servers = config.get("mcp_servers", {})
    mcp_manager = McpManager(mcp_servers, mode=mode, ssh_host=ssh_host, ssh_user=ssh_user)
    
    console.print("[yellow]Connecting to MCP servers...[/yellow]")
    connected = await mcp_manager.connect_all()
    if connected:
        console.print(f"[green][Success] Connected to MCP servers: {', '.join(connected)}[/green]")
        table = Table(title="Available Tools", title_style="bold cyan")
        table.add_column("Tool Name", style="bold green")
        table.add_column("Description", style="dim")
        for tool_name, (server_name, tool) in mcp_manager.tools.items():
            desc = tool.description or ""
            if len(desc) > 80:
                desc = desc[:77] + "..."
            table.add_row(tool_name, desc)
        console.print(table)
    else:
        console.print("[yellow]No MCP servers connected. Operating without tools.[/yellow]")
        
    client = CandelaClient(config)
    session = client.create_session()
    
    console.print("\n[bold green]Chat is ready! Type 'exit' to quit.[/bold green]\n")
    
    while True:
        try:
            user_input = click.prompt("User")
            if user_input.strip().lower() in ["exit", "quit"]:
                break
                
            if not user_input.strip():
                continue
                
            console.print("\n[bold magenta]CandelaAI is thinking... (Press ESC to interrupt)[/bold magenta]")
            
            # Setup cancellation and task execution
            cancel_event = asyncio.Event()
            esc_task = asyncio.create_task(wait_for_esc(cancel_event))
            send_task = asyncio.create_task(client.send_message(session, user_input, mcp_manager, console))
            
            while not send_task.done():
                if cancel_event.is_set():
                    send_task.cancel()
                    console.print("[bold red]\n[Cancelled] Execution interrupted by user (ESC pressed).[/bold red]")
                    break
                await asyncio.sleep(0.05)
            
            # Clean up ESC task
            cancel_event.set()
            try:
                await esc_task
            except Exception:
                pass
                
            if send_task.done() and not send_task.cancelled():
                response = send_task.result()
                console.print("\n[bold magenta]Assistant:[/bold magenta]")
                console.print(Markdown(response))
                console.print()
                
        except KeyboardInterrupt:
            break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            console.print(f"[bold red]Error during turn: {e}[/bold red]")
            import traceback
            traceback.print_exc()
            
    console.print("[yellow]Shutting down MCP servers...[/yellow]")
    await mcp_manager.close()
    console.print("[green]Goodbye![/green]")

@cli.command()
@click.option("--mode", type=click.Choice(["local", "remote"]), default=None, help="Execution mode (local or remote)")
@click.option("--ssh-host", help="SSH Host for remote execution")
@click.option("--ssh-user", default="root", help="SSH User for remote execution")
def chat(mode, ssh_host, ssh_user):
    """Start an interactive chat session with tool execution capability"""
    asyncio.run(chat_async(mode, ssh_host, ssh_user))

if __name__ == "__main__":
    cli()
