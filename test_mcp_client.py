import asyncio
from config import load_config
from mcp_client import McpManager
from client import CandelaClient
from rich.console import Console

async def main():
    console = Console()
    config = load_config()
    
    console.print("[bold cyan]Step 2: Connecting lanforge MCP to custom client...[/bold cyan]")
    
    # Initialize MCP Manager
    mcp_servers = config.get("mcp_servers", {})
    mcp_manager = McpManager(mcp_servers)
    
    console.print("[yellow]Connecting to MCP servers...[/yellow]")
    connected = await mcp_manager.connect_all()
    if not connected:
        console.print("[bold red]Error: Failed to connect to any MCP server.[/bold red]")
        return
        
    console.print(f"[green]Connected to MCP servers: {connected}[/green]")
    console.print(f"Available tools: {list(mcp_manager.tools.keys())}")
    
    client = CandelaClient(config)
    session = client.create_session()
    
    # We ask the model to call an MCP tool
    prompt = "List the available scripts in lanforge. Please use the appropriate tool to do so."
    console.print(f"\n[bold yellow]User Prompt:[/bold yellow] {prompt}")
    
    try:
        response = await client.send_message(session, prompt, mcp_manager, console)
        console.print(f"\n[bold green]Final Model Response:[/bold green]")
        console.print(response)
    except Exception as e:
        console.print(f"[bold red]Error during run: {e}[/bold red]")
        import traceback
        traceback.print_exc()
    finally:
        console.print("\n[yellow]Shutting down MCP servers...[/yellow]")
        await mcp_manager.close()
        console.print("[green]Done![/green]")

if __name__ == "__main__":
    asyncio.run(main())
