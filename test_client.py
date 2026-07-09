import asyncio
from config import load_config
from client import CandelaClient
from rich.console import Console

async def main():
    console = Console()
    config = load_config()
    # Setup test configuration
    if not config.get("provider"):
        config["provider"] = "gemini"
    if not config.get("gemini_model"):
        config["gemini_model"] = "gemini-3.1-flash-lite"
    
    console.print(f"Testing client with provider: [bold cyan]{config['provider']}[/bold cyan], model: [bold cyan]{config['gemini_model']}[/bold cyan]")
    
    client = CandelaClient(config)
    session = client.create_session()
    
    try:
        response = await client.send_message(session, "Hello! Say test is working in 5 words.", None, console)
        console.print(f"Response: [green]{response}[/green]")
    except Exception as e:
        console.print(f"[red]Error calling model: {e}[/red]")

if __name__ == "__main__":
    asyncio.run(main())
