"""INJECT_K split tests: candidate pool (TOP_K) vs injected set (INJECT_K).

Covers _parse_inject_k and the truncation at the tail of _on_pre_llm_call.
All offline; the Jev call is stubbed. The import pattern matches
test_rerank_hook_wire.py: load __init__.py through spec_from_file_location
so module globals can be patched per test.
"""

import importlib.util
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _fresh_plugin(env_vars):
    """Load plugin __init__ as a fresh module with controlled env.

    Do not pop or re-import bm25_retriever/jev_rerank here: conftest.py
    aliases ``scripts.bm25_retriever`` and ``bm25_retriever`` onto one
    module object, and splitting that identity breaks later tests whose
    monkeypatches land on the wrong instance.
    """
    import os

    old = {k: os.environ.get(k) for k in env_vars}
    try:
        for k, v in env_vars.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location(
            "skill_retrieval_injectk_test", PLUGIN_DIR / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _FakeIndex:
    """Returns ids a..l (12 candidates) in a fixed BM25 order."""

    def retrieve(self, query, top_k=6):
        ids = [f"s{i}" for i in range(min(top_k, 12))]
        return [(sid, 1.0 - i * 0.01) for i, sid in enumerate(ids)]


def _info(skill_id, **kwargs):
    return {"name": skill_id, "description": f"{skill_id} description"}


def test_inject_k_defaults_to_top_k_when_unset():
    mod = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                         "SKILL_RETRIEVAL_INJECT_K": None})
    assert mod.TOP_K == 12
    assert mod.INJECT_K == 12
    mod2 = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": None,
                          "SKILL_RETRIEVAL_INJECT_K": None})
    assert mod2.TOP_K == 6
    assert mod2.INJECT_K == 6


def test_inject_k_parsed_and_clamped():
    mod = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                         "SKILL_RETRIEVAL_INJECT_K": "6"})
    assert mod.INJECT_K == 6
    # Above the candidate pool → clamped to top_k.
    mod2 = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                          "SKILL_RETRIEVAL_INJECT_K": "99"})
    assert mod2.INJECT_K == 12
    # Invalid → falls back to top_k.
    mod3 = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                          "SKILL_RETRIEVAL_INJECT_K": "bogus"})
    assert mod3.INJECT_K == 12


def _run_hook(mod, rerank_result):
    mod.get_index = lambda **k: _FakeIndex()
    _orig_info = mod.get_skill_info
    mod.get_skill_info = _info
    mod._jev_rerank = lambda q, shortlist: rerank_result
    try:
        return mod._on_pre_llm_call("", "test query", is_first_turn=True)
    finally:
        mod.get_skill_info = _orig_info


def test_rerank_applied_injects_jev_top_inject_k():
    """Pool of 12, Jev reorders, injection keeps only Jev's top INJECT_K."""
    mod = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                         "SKILL_RETRIEVAL_INJECT_K": "6",
                         "SKILL_RETRIEVAL_RERANK": "jev"})
    # Jev promotes the tail (s9, s10, s11) to the top.
    order = ["s11", "s9", "s6", "s0", "s5", "s2",
             "s1", "s3", "s4", "s7", "s8", "s10"]
    mod.RERANK_MODE = "jev"
    rr = {"order": order, "skipped": False,
          "input_tokens": 100, "latency_ms": 5}
    result = _run_hook(mod, rr)
    assert result is not None
    lines = result["context"].split("\n")
    bullets = [line for line in lines if line.startswith("- **")]
    assert len(bullets) == 6
    assert [line.split("**")[1] for line in bullets] == [
        "s11", "s9", "s6", "s0", "s5", "s2"]
    # The note records the filter.
    assert "kept top 6/12" in result["context"]


def test_rerank_skipped_injects_bm25_top_inject_k():
    """Jev failure → BM25's first INJECT_K stand, no failure blocks injection."""
    mod = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                         "SKILL_RETRIEVAL_INJECT_K": "6",
                         "SKILL_RETRIEVAL_RERANK": "jev"})
    mod.RERANK_MODE = "jev"
    rr = {"skipped": True, "fallback_reason": "api_error"}
    result = _run_hook(mod, rr)
    bullets = [line for line in result["context"].split("\n")
               if line.startswith("- **")]
    assert len(bullets) == 6
    assert [line.split("**")[1] for line in bullets] == [
        "s0", "s1", "s2", "s3", "s4", "s5"]
    assert "kept top" not in result["context"]


def test_no_split_when_default():
    """INJECT_K unset → injection size equals candidate pool (old behavior)."""
    mod = _fresh_plugin({"SKILL_RETRIEVAL_TOP_K": "12",
                         "SKILL_RETRIEVAL_INJECT_K": None,
                         "SKILL_RETRIEVAL_RERANK": "jev"})
    assert mod.INJECT_K == mod.TOP_K
    mod.RERANK_MODE = "off"
    result = _run_hook(mod, {"skipped": True})
    bullets = [line for line in result["context"].split("\n")
               if line.startswith("- **")]
    assert len(bullets) == 12