"""
Quick authentication test — verifies both source and target org connections.
Runs independently of the full migration pipeline.
"""

from dotenv import load_dotenv
from core.connector import connector_from_env
from rich.console import Console

load_dotenv()
console = Console()

for prefix in ("SOURCE", "TARGET"):
    console.print(f"\n[bold]Testing {prefix} org...[/bold]")
    try:
        conn = connector_from_env(prefix)
        conn.connect()
        console.print(f"  [green]✓ Authenticated[/green]")
        console.print(f"  [green]✓ Instance URL : {conn.instance_url}[/green]")
        console.print(f"  [green]✓ API Version  : {conn.api_version}[/green]")

        # Quick sanity check — describe Account
        desc = conn.describe_object("Account")
        console.print(f"  [green]✓ Describe API : Account has {len(desc.get('fields', []))} fields[/green]")

    except Exception as e:
        console.print(f"  [red]✗ FAILED: {e}[/red]")
