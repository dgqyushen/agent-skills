"""skill-manifest plugin entry: register the three lifecycle hooks."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _same_file(mod, real: str) -> bool:
    mod_file = getattr(mod, "__file__", None)
    if not isinstance(mod_file, str):
        return False
    try:
        return os.path.realpath(mod_file) == real
    except OSError:
        return False


def _load_by_path(unique_name: str, path: str):
    """Load `path` as `unique_name` so a bare `import hooks` cannot win."""
    import importlib.util
    import sys

    real = os.path.realpath(path)
    bare = os.path.splitext(os.path.basename(real))[0]
    exact = sys.modules.get(unique_name)
    if exact is not None and _same_file(exact, real):
        return exact
    bare_mod = sys.modules.get(bare)
    if bare_mod is not None and _same_file(bare_mod, real):
        sys.modules.setdefault(unique_name, bare_mod)
        return bare_mod
    spec = importlib.util.spec_from_file_location(unique_name, real)
    if spec is None or spec.loader is None:
        raise ImportError(real)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    claimed = bare not in sys.modules
    if claimed:
        sys.modules[bare] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(unique_name, None)
        if claimed:
            sys.modules.pop(bare, None)
        raise
    return mod


def _import_hooks():
    try:
        from . import hooks as hooks_mod
    except ImportError:
        # Package load resolves `from . import hooks`. A plain-module load has
        # no parent package, so that import raises ImportError (not only
        # ModuleNotFoundError). Put this directory and shared/scripts on
        # sys.path, then load hooks.py by file path under a unique module
        # name. A bare `import hooks` would bind whatever hooks.py sits
        # earlier on sys.path.
        import sys as _sys

        here = os.path.dirname(os.path.abspath(__file__))
        scripts = os.path.join(here, "shared", "scripts")
        for entry in (here, scripts):
            if entry not in _sys.path:
                _sys.path.append(entry)
        hooks_mod = _load_by_path("skill_manifest_hooks", os.path.join(here, "hooks.py"))
    return hooks_mod


def register(ctx) -> None:
    try:
        hooks_mod = _import_hooks()
        globals()["hooks"] = hooks_mod
        hooks_mod.configure(ctx)
        ctx.register_hook("on_skill_lifecycle", hooks_mod.on_skill_lifecycle)
        ctx.register_hook("on_session_start", hooks_mod.on_session_start)
        ctx.register_hook("pre_llm_call", hooks_mod.on_pre_llm_call)
    except Exception:
        logger.warning("skill-manifest register failed", exc_info=True)
        return
    _try_register_cli(ctx, hooks_mod)
    _try_register_slash(ctx, hooks_mod)


def _try_register_cli(ctx, hooks_mod) -> None:
    """Register `hermes skill-manifest`. A host without the API is skipped silently."""
    register_cli = getattr(ctx, "register_cli_command", None)
    if not callable(register_cli):
        return
    try:
        register_cli(
            "skill-manifest",
            hooks_mod.CLI_HELP,
            hooks_mod.cli_setup,
            hooks_mod.cli_handler,
        )
    except Exception:
        logger.warning("skill-manifest cli register failed", exc_info=True)


def _try_register_slash(ctx, hooks_mod) -> None:
    """Register /skill-manifest-ack. A host without the API is skipped silently."""
    register_cmd = getattr(ctx, "register_command", None)
    if not callable(register_cmd):
        return
    try:
        register_cmd(
            "skill-manifest-ack",
            hooks_mod.slash_skill_manifest_ack,
            description=(
                "Acknowledge a reviewed skill-manifest digest: "
                "/skill-manifest-ack <name> <hex>"
            ),
            args_hint="<name> <hex>",
        )
    except Exception:
        logger.warning("skill-manifest slash register failed", exc_info=True)
