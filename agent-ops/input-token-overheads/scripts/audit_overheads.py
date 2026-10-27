#!/usr/bin/env python3
"""Overhead audit for a Hermes instance (input-token-overheads v1.8).

Three-way, category-by-category measurement (v1.8):

1. MEASURED (ground truth): the script starts a REAL prompt
   (`hermes -z <trivial query> --usage-file ...`) and reads the input tokens
   the provider actually charged for a fresh session. Provider usage is the
   only total; the provider does not break it down by category.
2. INDEPENDENT (category-by-category): the script builds Hermes' own offline
   inspection agent the way a live session is built, renders the real system
   prompt parts, and measures each category separately with the build's
   rough tokenizer: fixed system text, skills index, memory, user profile,
   builtin / MCP / subagent tool schemas. This does NOT read prompt-size
   output — it re-derives categories from the rendered prompt.
3. BUDGET (`hermes prompt-size --json`): the build's offline byte accounting,
   used only for compare rows (bytes/4 estimator).

Output: per-category table (independent vs budget, with delta), totals
row against the provider-measured input, plus injection-cap checks
(context-doctor port): TRUNCATED = silently cut every turn,
MISSING = broken reference. Exit 1 when any injected file needs attention.

--skip-live skips the real run (no inference call); then only the
independent vs budget table is printed and the totals row is flagged
unmeasured.

Usage:
  python3 audit_overheads.py                      # live run + full compare
  python3 audit_overheads.py --skip-live          # categories, no inference
  python3 audit_overheads.py --chart out.png      # + PNG chart
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # PyYAML is optional; config-dependent stats degrade
    yaml = None

HERMES = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
DEFAULT_CTX = 200000
DIV = 4  # chars/bytes -> tokens for the budget-side estimator
# Injection caps. The memory tool enforces these budgets; mandatory-skill
# full-body loads are capped by the retrieval plugin's per-skill limit.
CAPS = {
    "memory.md": 2200,
    "user profile": 3000,
    "mandatory skill body": 10000,
}


def load_config():
    if yaml is None:
        return {}
    p = os.path.join(HERMES, "config.yaml")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def run_live_prompt(timeout=600):
    """Start a real one-shot prompt; return the usage report it writes.

    Ground truth: input_tokens = tokens the provider actually received for
    a fresh session (system prompt + tool schemas + trivial user message).
    Runs with cwd=HERMES so no CWD AGENTS.md contaminates the measurement.
    """
    fd, path = tempfile.mkstemp(suffix="_usage.json")
    os.close(fd)
    os.unlink(path)
    try:
        r = subprocess.run(
            ["hermes", "-z", "Reply with OK", "--usage-file", path],
            capture_output=True, text=True, timeout=timeout, cwd=HERMES,
        )
        if not os.path.isfile(path):
            return {"error": f"usage file not written: {(r.stderr or '').strip()[-200:]}"}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        return {"error": str(e)}
    finally:
        if os.path.isfile(path):
            os.unlink(path)


def prompt_size(platform="cli"):
    """Run `hermes prompt-size --platform P --json`; parse dict or None.

    Used ONLY as the budget column of the comparison table.
    """
    try:
        r = subprocess.run(
            ["hermes", "prompt-size", "--platform", platform, "--json"],
            capture_output=True, text=True, timeout=120, cwd=HERMES,
        )
        out = r.stdout or ""
        if r.returncode != 0 or "{" not in out:
            return None
        return json.loads(out[out.index("{"):])
    except Exception:
        return None


def independent_categories(platform="cli"):
    """Category-by-category measurement from the REAL rendered prompt.

    Builds Hermes' own offline inspection agent (dummy credentials, no
    network — the same construction prompt-size uses, but we read the
    rendered parts and tool list directly), then splits the render the way
    the live /context breakdown does. Returns None when the build's
    internals are not importable from this interpreter.
    """
    try:
        from agent.rough_tokenizer import estimate_tokens_rough  # noqa
    except ImportError:
        try:
            from agent.model_metadata import estimate_tokens_rough  # noqa: F401
        except ImportError:
            return None
    try:
        from agent.context_breakdown import _memory_blocks
        from agent.context_breakdown import (  # noqa: F401
            _SKILLS_BLOCK_RE, _json_tokens, _join, _strip_blocks)
        from hermes_cli.prompt_size import _build_inspection_agent
    except ImportError:
        return None
    try:
        agent = _build_inspection_agent(platform)
        from agent.system_prompt import build_system_prompt_parts
        from agent.model_metadata import estimate_tokens_rough
        parts = build_system_prompt_parts(agent)
        stable = parts.get("stable", "") or ""
        volatile = parts.get("volatile", "") or ""
        # Skills index lives in the volatile tier on current builds (moved
        # out of stable to keep the cached identity prefix stable); older
        # layouts keep it in stable. Search both, strip from both.
        m = _SKILLS_BLOCK_RE.search(volatile) or _SKILLS_BLOCK_RE.search(stable)
        skills_text = m.group(0) if m else ""
        memory_text, user_text = _memory_blocks(agent)
        fixed = _join(_strip_blocks(stable, skills_text),
                      _strip_blocks(volatile, skills_text, memory_text,
                                    user_text))
        rules = parts.get("context", "") or ""
        tools = list(getattr(agent, "tools", None) or [])
        builtin, mcp, subagent = [], [], []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            name = str((fn if isinstance(fn, dict) else t).get("name") or "")
            (mcp if name.startswith("mcp_") else
             subagent if name == "delegate_task" else builtin).append(t)
        return {
            "system fixed text": estimate_tokens_rough(fixed),
            "rules (AGENTS.md/context)": estimate_tokens_rough(rules),
            "skills index": estimate_tokens_rough(skills_text),
            "memory (own facts)": estimate_tokens_rough(memory_text),
            "user profile": estimate_tokens_rough(user_text),
            "tool schemas (builtin)": _json_tokens(builtin),
            "tool schemas (mcp)": _json_tokens(mcp),
            "tool schemas (subagent)": _json_tokens(subagent),
            "tool count": len(tools),
        }
    except Exception as e:
        return {"error": str(e)}


def check_file(path, cap, label):
    """context-doctor-style status for one injected file."""
    if not os.path.isfile(path):
        return {"file": label, "status": "MISSING", "chars": None, "cap": cap}
    size = os.path.getsize(path)
    status = "TRUNCATED" if size > cap else "OK"
    return {"file": label, "status": status, "chars": size, "cap": cap}


def audit_injected_files():
    """Truncation/missing check on always-injected files."""
    out = []
    for mem in glob.glob(os.path.join(HERMES, "memories", "MEMORY.md")):
        out.append(check_file(mem, CAPS["memory.md"], "memory.md"))
    profile = "profiles/{}/memories/USER.md".format(
        os.environ.get("HERMES_PROFILE", "default"))
    for path in (profile, os.path.join(HERMES, "memories", "USER.md")):
        if os.path.isfile(os.path.join(HERMES, path)):
            out.append(check_file(os.path.join(HERMES, path),
                                  CAPS["user profile"], "user profile"))
            break
    cfg = load_config()
    mandatory = ((cfg.get("skills") or {}).get("mandatory") or [])
    for name in mandatory:
        hits = glob.glob(os.path.join(HERMES, "skills", "**", name,
                                      "SKILL.md"), recursive=True)
        if not hits:
            out.append({"file": f"mandatory:{name}", "status": "MISSING",
                        "chars": None, "cap": CAPS["mandatory skill body"]})
        else:
            out.append(check_file(hits[0], CAPS["mandatory skill body"],
                                  f"mandatory:{name}"))
    return out


def health_ratio(overhead_tokens, ctx=DEFAULT_CTX):
    pct = overhead_tokens / ctx * 100
    if pct < 5:
        return pct, "excellent"
    if pct < 15:
        return pct, "healthy"
    if pct < 25:
        return pct, "acceptable"
    return pct, "unhealthy"


def make_chart(rows, path, ctx=DEFAULT_CTX):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("chart skipped: matplotlib not available", file=sys.stderr)
        return None
    labels = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 1.2 + 0.5 * len(labels)))
    ax.barh(labels[::-1], vals[::-1], color="#4a7c59")
    ax.set_xlabel("tokens")
    ax.set_title(f"Hermes input overhead (window {ctx:,} tok)")
    for i, v in enumerate(vals[::-1]):
        ax.text(v, i, f" {v:,}", va="center", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chart", metavar="PNG", default=None,
                    help="also write a PNG chart to this path")
    ap.add_argument("--skip-live", action="store_true",
                    help="skip the real one-shot prompt (inference call)")
    ap.add_argument("--platform", default="cli",
                    help="surface to simulate (cli, telegram, discord, ...)")
    ap.add_argument("--live-timeout", type=int, default=600,
                    help="seconds to wait for the one-shot run")
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                    help="context window size in tokens")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    injected = audit_injected_files()
    live = run_live_prompt(args.live_timeout) if not args.skip_live else None
    ps = prompt_size(args.platform)
    indep = independent_categories(args.platform)

    # Budget column from prompt-size (bytes/4 estimator).
    budget = None
    if ps:
        sp = ps.get("system_prompt", {}).get("chars", 0)
        blocks = {
            "skills_index": (ps.get("skills_index", {}).get("chars", 0)) // DIV,
            "memory": (ps.get("memory", {}).get("bytes", 0)) // DIV,
            "user_profile": (ps.get("user_profile", {}).get("bytes", 0)) // DIV,
        }
        budget = {
            "rows": {
                "skills index": blocks["skills_index"],
                "memory (own facts)": blocks["memory"],
                "user profile": blocks["user_profile"],
                "tool schemas (builtin+mcp+subagent)":
                    (ps.get("tools", {}).get("json_bytes", 0)) // DIV,
                "system fixed text": (sp
                                      - ps.get("skills_index", {}).get("chars", 0)
                                      - ps.get("memory", {}).get("bytes", 0)
                                      - ps.get("user_profile", {}).get("bytes", 0)) // DIV,
            },
            "total_tokens": (sp + ps.get("tools", {}).get("json_bytes", 0)) // DIV,
            "tool_count": ps.get("tools", {}).get("count"),
            "model": ps.get("model"),
        }

    # Comparison rows: independent estimate vs budget, per category.
    rows = []
    if indep and budget:
        indep_rows = {k: v for k, v in indep.items()
                      if not k.startswith("tool")}
        pairs = [
            ("system fixed text", "system fixed text"),
            ("skills index", "skills index"),
            ("memory (own facts)", "memory (own facts)"),
            ("user profile", "user profile"),
            ("tool schemas (all)", "tool schemas (builtin+mcp+subagent)"),
        ]
        for li_key, bu_key in pairs:
            if li_key == "tool schemas (all)":
                li_val = indep.get("tool schemas (builtin)", 0) \
                    + indep.get("tool schemas (mcp)", 0) \
                    + indep.get("tool schemas (subagent)", 0)
            else:
                li_val = indep_rows.get(li_key, 0)
            rows.append({
                "category": li_key,
                "independent_est": li_val,
                "prompt_size_budget": budget["rows"].get(bu_key, 0),
                "delta": li_val - budget["rows"].get(bu_key, 0),
            })
    detail_tools = None
    if indep:
        detail_tools = {
            "builtin": indep.get("tool schemas (builtin)", 0),
            "mcp": indep.get("tool schemas (mcp)", 0),
            "subagent": indep.get("tool schemas (subagent)", 0),
            "count": indep.get("tool count"),
        }

    # Totals: provider-measured reality vs estimates.
    measured = None
    if live and "input_tokens" in live:
        measured = {
            "input_tokens": live["input_tokens"],
            "cache_read_tokens": live.get("cache_read_tokens", 0),
            "output_tokens": live.get("output_tokens", 0),
            "model": live.get("model"),
            "provider": live.get("provider"),
            "api_calls": live.get("api_calls"),
        }
    indep_total = sum(r["independent_est"] for r in rows) if rows else None
    budget_total = budget["total_tokens"] if budget else None
    overhead = measured["input_tokens"] if measured \
        else (indep_total or budget_total or 0)
    source = ("live one-shot run (provider usage)" if measured
              else "estimates only (run without --skip-live to measure)")
    pct, rating = health_ratio(overhead, args.ctx)

    total_row = None
    if rows:
        total_row = {
            "category": "TOTAL",
            "independent_est": indep_total,
            "prompt_size_budget": budget_total,
            "delta": (indep_total - budget_total) if indep_total is not None
            and budget_total is not None else None,
            "provider_measured": measured["input_tokens"] if measured else None,
        }

    result = {
        "measured": measured
        or {"source": source,
            "error": (live or {}).get("error", "skipped (--skip-live)")},
        "independent_categories": rows,
        "independent_detail": detail_tools,
        "independent_error": indep.get("error") if isinstance(indep, dict)
        and "error" in indep else None,
        "prompt_size_budget_total": budget_total,
        "overhead_tokens": overhead,
        "overhead_pct_of_window": round(pct, 1),
        "health": rating,
        "injected_files": injected,
        "mem_provider_active": bool(glob.glob(
            os.path.join(HERMES, "**", "mnemosyne*.db"), recursive=True)),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Measured per-turn input: {overhead:,} tok "
              f"({pct:.1f}% of {args.ctx:,}) -> {rating}")
        print(f"  source: {source}\n")
        if rows:
            print(f"{'category':<28}{'indep est':>10}{'budget':>10}{'delta':>9}")
            for r in rows:
                print(f"{r['category']:<28}{r['independent_est']:>10,}"
                      f"{r['prompt_size_budget']:>10,}{r['delta']:>9,}")
            if total_row:
                pr = total_row["provider_measured"]
                pr_s = f"{pr:,}" if pr is not None else "unmeasured"
                delta_s = (f"{total_row['delta']:,}"
                           if total_row["delta"] is not None else "-")
                print(f"{'TOTAL':<28}{total_row['independent_est']:>10,}"
                      f"{total_row['prompt_size_budget']:>10,}{delta_s:>9}")
                print(f"{'provider measured':<28}{pr_s:>20}")
            print(f"\n  note: independent = rendered-prompt categories with "
                  f"the build's rough tokenizer; budget = prompt-size "
                  f"bytes/4; measured = provider usage (includes user "
                  f"message + wrapper tokens)")
        if detail_tools:
            print(f"  tool split (independent): builtin "
                  f"{detail_tools['builtin']:,} / mcp {detail_tools['mcp']:,} / "
                  f"subagent {detail_tools['subagent']:,} tok over "
                  f"{detail_tools['count']} tools")
        if result["independent_error"]:
            print(f"  independent: {result['independent_error']}")
        if result["measured"].get("error"):
            print(f"  live run: {result['measured']['error']}")
        print("\nInjected-file check (context-doctor port):")
        for f in injected:
            marker = {"OK": "OK ", "TRUNCATED": "!!", "MISSING": "??"}[f["status"]]
            detail = ("-" if f["chars"] is None else
                      f"{f['chars']:,} chars (cap {f['cap']:,})")
            print(f"  [{marker}] {f['status']:<9} {f['file']}: {detail}")
        if result["mem_provider_active"]:
            print("  note: mnemosyne active — recall is injected per turn "
                  "outside the prompt-size budget; the measured live input "
                  "includes it")

    if args.chart:
        chart_rows = [(r["category"], r["independent_est"]) for r in rows]
        if measured:
            chart_rows.append(("measured real input", measured["input_tokens"]))
        if rows:
            out = make_chart(chart_rows, args.chart, args.ctx)
            if out:
                print(f"chart written: {out}")
    truncated = [f for f in injected if f["status"] != "OK"]
    if truncated:
        print(f"\nACTION: {len(truncated)} injected file(s) need attention "
              "(TRUNCATED = silently cut every turn; MISSING = broken "
              "reference)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
