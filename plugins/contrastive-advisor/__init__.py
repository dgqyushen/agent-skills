"""contrastive-advisor plugin entry: register the advisory hooks and the
bundled skills.

The gateway loader execs this file as a synthetic package WITHOUT the plugin
dir on sys.path — so the sibling import goes relative FIRST (correct and
collision-free in the loader context), with a bare-import fallback for
plain-module test loads. Hooks bind ctx by closure (kanban-mirror pattern):
the gateway passes only event kwargs to the callback.

Bundled skills register under their qualified name '<plugin>:<name>'
(register_skill semantics, hermes_cli/plugins_content.py). The bundled
`contrastive-refinement` copy therefore never collides with a bare
`contrastive-refinement` skill in the user's skills tree: fresh catalog
installs get the method through this registration, while hosts that already
carry the bare skill keep theirs unchanged.
"""

import os
import sys

PLUGIN_NAME = "contrastive-advisor"
BUNDLED_SKILLS = ("contrastive-refinement",)


def _load_advisor():
    try:
        from . import advisor
        return advisor
    except (ImportError, ValueError, TypeError):
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.append(_here)
        import advisor
        return advisor


def register(ctx):
    advisor = _load_advisor()
    plugin_dir = os.path.dirname(os.path.abspath(__file__))

    def on_pre_tool_call(tool_name=None, args=None, **_kwargs):
        return advisor.on_pre_tool_call(ctx, tool_name=tool_name, args=args, **_kwargs)

    def on_pre_llm_call(session_id=None, user_message=None, **_kwargs):
        return advisor.on_pre_llm_call(
            ctx, session_id=session_id, user_message=user_message, **_kwargs
        )

    def on_session_start(session_id="", **_kwargs):
        return advisor.on_session_start(ctx, session_id=session_id, **_kwargs)

    for _skill in BUNDLED_SKILLS:
        try:
            ctx.register_skill(
                _skill, os.path.join(plugin_dir, "skills", _skill, "SKILL.md")
            )
        except Exception as _exc:
            print(
                f"contrastive-advisor: register_skill({_skill}) failed: {_exc}",
                file=sys.stderr,
            )

    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)