from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from . import __version__
from .config import load_config
from .service import BackupService

app = typer.Typer(help="Hot, deduplicated backups for standalone VMware ESXi hosts.")
ConfigOption = Annotated[Path | None, typer.Option("--config", "-c")]
JsonOption = Annotated[bool, typer.Option("--json")]


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=version_callback, is_eager=True,
            help="Show the application version and exit.",
        ),
    ] = False,
) -> None:
    """Hot, deduplicated backups for standalone VMware ESXi hosts."""


def service(config: Path | None) -> BackupService:
    return BackupService(load_config(config))


@app.command("vms")
def list_vms(config: ConfigOption = None, json_output: JsonOption = False):
    """List virtual machines on the configured ESXi host."""
    vms = service(config).list_vms()
    if json_output:
        typer.echo(json.dumps([vm.model_dump() for vm in vms], indent=2))
    else:
        for vm in vms:
            typer.echo(f"{vm.id}\t{vm.power_state}\t{vm.name}")


@app.command()
def backup(vm: str, config: ConfigOption = None):
    """Create a hot backup by VM name or managed-object ID."""
    record = service(config).backup(vm)
    typer.echo(f"{record.id}\t{record.status}\t{record.logical_bytes} bytes")


@app.command()
def history(vm: str | None = None, config: ConfigOption = None, json_output: JsonOption = False):
    """Show backup history."""
    records = service(config).repository.list(vm)
    if json_output:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
    else:
        for r in records:
            typer.echo(f"{r.id}\t{r.status}\t{r.started_at.isoformat()}\t{r.vm_name}")


@app.command()
def restore(backup_id: str, destination: Path, config: ConfigOption = None):
    """Reconstruct OVF/VMDK export files from a backup."""
    for path in service(config).restore(backup_id, destination):
        typer.echo(path)


@app.command("restore-to-esxi")
def restore_to_esxi(
    backup_id: str,
    name: Annotated[str, typer.Option("--name", help="Name for the newly imported VM")],
    datastore: Annotated[str | None, typer.Option("--datastore")] = None,
    config: ConfigOption = None,
):
    """Import an OVF-enabled recovery point into ESXi as a new VM."""
    service(config).restore_to_esxi(backup_id, name, datastore)
    typer.echo(f"Imported {backup_id} as {name}")


@app.command("export-ova")
def export_ova(
    backup_id: str,
    output: Path,
    config: ConfigOption = None,
):
    """Stream an OVF-enabled recovery point into a portable OVA archive."""
    path = service(config).export_ova(backup_id, output)
    typer.echo(path)


@app.command()
def web(config: ConfigOption = None, host: str = "127.0.0.1", port: int = 8080):
    """Run the web dashboard."""
    from .web import create_app
    uvicorn.run(create_app(config), host=host, port=port)


if __name__ == "__main__":
    app()
