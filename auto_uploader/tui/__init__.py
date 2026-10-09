"""Textual control center."""


def run_tui(config_path: str = "config.yaml", verbose: bool = False) -> None:
    from ..context import AppContext
    from ..logging_setup import setup_logging
    from .app import TuiLogHandler, UploaderApp

    ctx = AppContext.load(config_path)
    app = UploaderApp(ctx)
    svc = ctx.config.service
    setup_logging("DEBUG" if verbose else svc.log_level, svc.log_file,
                  console=False, extra_handler=TuiLogHandler(app))
    app.run()
