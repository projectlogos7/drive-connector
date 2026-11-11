"""In-house / low-cost Phase 2 providers: local embedding model, Ollama and cheapest-Claude summaries."""

import math
import sys
import types

import httpx

from nous_drive.config import reset_settings
from nous_drive.content import embed, summarize


class FakeModel:
    def embed(self, texts, batch_size=32):
        for t in texts:
            yield [float(len(t)), 1.0, 0.0] + [0.5] * 381  # 384 dims like MiniLM


def test_fastembed_vectors_are_padded_and_normalised(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_EMBEDDING_PROVIDER", "fastembed")
    reset_settings()
    monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))
    e = embed.get_embedder()
    assert isinstance(e, embed.FastEmbedEmbedder) and "MiniLM" in e.name
    embed.FastEmbedEmbedder._models[e.model_name] = FakeModel()
    v = e.embed(["abc"])[0]
    assert len(v) == 1024 and all(x == 0.0 for x in v[384:])
    assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6)


def test_falls_back_to_hash_without_fastembed(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_EMBEDDING_PROVIDER", "fastembed")
    reset_settings()
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import fails
    assert isinstance(embed.get_embedder(), embed.LocalHashEmbedder)


def test_ollama_summary(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_SUMMARY_PROVIDER", "ollama")
    reset_settings()
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, json=json)
        return httpx.Response(200, json={"message": {"content": " A script for episode 2. "}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(summarize.httpx, "post", fake_post)
    s, model = summarize.summarize("x" * 100_000, "s.docx", "/Ep2/s.docx")
    assert s == "A script for episode 2." and model == "ollama:qwen2.5:3b"
    assert sent["url"].endswith("/api/chat")
    assert len(sent["json"]["messages"][1]["content"]) < 25_000  # capped input


def test_ollama_down_falls_back(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_SUMMARY_PROVIDER", "ollama")
    reset_settings()
    monkeypatch.setattr(summarize.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    s, model = summarize.summarize("First sentence. Second one.", "a", "/a")
    assert model == "extractive" and s.startswith("First sentence.")


def test_claude_uses_cheapest_model_without_beta(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_SUMMARY_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    reset_settings()
    calls = {}

    class Msgs:
        def create(self, **kw):
            calls["kw"] = kw
            return types.SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                         content=[types.SimpleNamespace(type="text", text="Summary.")])

    class Beta:
        messages = types.SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(AssertionError("beta used")))

    fake = types.SimpleNamespace(Anthropic=lambda: types.SimpleNamespace(messages=Msgs(), beta=Beta()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    s, model = summarize.summarize("Some text.", "a", "/a")
    assert (s, model) == ("Summary.", "claude-haiku-5-5")
    assert calls["kw"]["output_config"] == {"effort": "low"}
