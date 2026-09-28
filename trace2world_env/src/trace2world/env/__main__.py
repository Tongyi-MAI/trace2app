
import click
import uvicorn


@click.command()
@click.option("--port", default=6800, type=int, help="Port to serve on.")
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option(
    "--environments-dir",
    "portfolio_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Host the whole Trace2World environment release and select an environment by task id.",
)
@click.option(
    "--environment-dir",
    "resource_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Host one exported Trace2World environment package (environments/<id>).",
)
@click.option(
    "--state-root",
    default=None,
    type=click.Path(file_okay=False),
    help="Writable directory for SQLite episode state.",
)
@click.option(
    "--browser-executable",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional Chromium executable for a local browser host.",
)
@click.option(
    "--response-mode",
    "response_mode_option",
    default="env_text",
    type=click.Choice(["visual_notice", "env_text", "silent"]),
    show_default=True,
    help="Actionability response mode injected into task pages.",
)
@click.option(
    "--actionability-preflight/--no-actionability-preflight",
    "actionability_preflight_option",
    default=True,
    show_default=True,
    help="Inspect click-like actions with the packaged actionability contract before executing.",
)
@click.option(
    "--allow-package-validation-status",
    "allowed_validation_statuses",
    multiple=True,
    default=("pass",),
    show_default=True,
    help=(
        "Explicit package-validation statuses accepted by the runtime. "
        "Repeat the option to add a non-pass status."
    ),
)
def main(
    port: int,
    host: str,
    portfolio_dir: str | None,
    resource_dir: str | None,
    state_root: str | None,
    browser_executable: str | None,
    response_mode_option: str,
    actionability_preflight_option: bool,
    allowed_validation_statuses: tuple[str, ...],
) -> None:
    from trace2world.env.server import create_webenv_app

    if bool(resource_dir) == bool(portfolio_dir):
        raise click.UsageError("Provide exactly one of --environments-dir or --environment-dir")

    resource_manifest = None
    evaluator_plan = None
    evaluators_dir = None
    tasks_file = None
    entry_page = None
    portfolio_layout = None
    if portfolio_dir:
        from trace2world.env.layout import resolve_portfolio_layout

        portfolio_layout = resolve_portfolio_layout(
            portfolio_dir,
            allowed_validation_statuses=frozenset(allowed_validation_statuses),
        )
        static_dir = str(portfolio_layout.root)
    else:
        from trace2world.env.layout import resolve_resource_layout

        layout = resolve_resource_layout(
            resource_dir,
            allowed_validation_statuses=frozenset(allowed_validation_statuses),
        )
        static_dir = str(layout.static_dir)
        tasks_file = str(layout.tasks_file)
        entry_page = layout.entry_page
        evaluators_dir = str(layout.evaluators_dir) if layout.evaluators_dir else None
        resource_manifest = layout.resource_manifest
        evaluator_plan = str(layout.evaluator_plan) if layout.evaluator_plan else None
    entry_page = entry_page or "homepage.html"

    app = create_webenv_app(
        static_dir=static_dir,
        evaluators_dir=evaluators_dir,
        tasks_file=tasks_file,
        entry_page=entry_page,
        server_port=port,
        response_mode=response_mode_option,
        actionability_preflight=actionability_preflight_option,
        resource_dir=resource_dir,
        resource_manifest=resource_manifest,
        evaluator_plan=evaluator_plan,
        state_root=state_root,
        browser_executable_path=browser_executable,
        portfolio_layout=portfolio_layout,
    )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
