import asyncio
from config import load_config
from mcp_client import McpManager
from client import CandelaClient
from rich.console import Console

async def main():
    console = Console()
    config = load_config()
    
    console.print("[bold cyan]Testing full MCP chat flow...[/bold cyan]")
    
    mcp_servers = config.get("mcp_servers", {})
    mcp_manager = McpManager(mcp_servers)
    
    connected = await mcp_manager.connect_all()
    if not connected:
        console.print("[bold red]Error: Failed to connect to MCP.[/bold red]")
        return
        
    client = CandelaClient(config)
    session = client.create_session()
    
    prompt = "Find a script in lanforge related to DHCP. What is its description?"
    console.print(f"\n[bold yellow]User Prompt:[/bold yellow] {prompt}")
    
    try:
        response = await client.send_message(session, prompt, mcp_manager, console)
        console.print(f"\n[bold green]Final Model Response:[/bold green]")
        console.print(response)
    except Exception as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
    finally:
        await mcp_manager.close()

if __name__ == "__main__":
    asyncio.run(main())
