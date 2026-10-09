"""Unit tests for pure helpers (no database)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.chat.language import detect_language
from app.chat.pii import mask_pii
from app.chat.prompt import NO_ANSWER_TAG, build_messages, parse_answer, render_system_prompt
from app.chat.service import normalize_question
from app.indexer.chunker import chunk_text
from app.llm.providers import PROVIDERS, is_loopback_url
from app.search.hybrid import SearchHit, keyword_query, rrf_merge, words
from app.security.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.security.passwords import hash_password, password_problem, verify_password
from app.services.clients import ClientError, normalize_domain, normalize_domains, origin_allowed, validate_slug
from app.worker.scheduler import crawl_due

# ----------------------------------------------------------------------------- PII


@pytest.mark.parametrize(
    "text, expected",
    [
        ("my email is john.doe+x@example.co.in thanks", "my email is [email] thanks"),
        ("call me on +91 98470 12345", "call me on [phone]"),
        ("phone: (0484) 271-2345", "phone: [phone]"),
        ("9847012345", "[phone]"),
    ],
)
def test_mask_pii_masks(text: str, expected: str) -> None:
    assert mask_pii(text) == expected


@pytest.mark.parametrize(
    "text",
    ["Is it open on 2024-10-08?", "Timings 10:00 - 22:00", "Shop no 12 on floor 2", "Price is 1,500 rupees", "Open 09.00 - 23.00"],
)
def test_mask_pii_keeps_non_personal_numbers(text: str) -> None:
    assert mask_pii(text) == text


# ----------------------------------------------------------------------------- language


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ASICS സ്റ്റോർ എവിടെയാണ്?", "ml"),
        ("मूवी कब है?", "hi"),
        ("أين المتجر؟", "ar"),
        ("Where is the ASICS store located in this mall?", "en"),
        ("hi", "en"),
    ],
)
def test_detect_language(text: str, expected: str) -> None:
    assert detect_language(text, "en") == expected


def test_detect_language_short_latin_uses_default() -> None:
    assert detect_language("ok", "ml") == "ml"


# ----------------------------------------------------------------------------- text helpers


def test_words_keep_malayalam_combining_marks() -> None:
    assert words("ലുലു മാളിൽ ASICS?") == ["ലുലു", "മാളിൽ", "asics"]


def test_normalize_question_groups_variants() -> None:
    assert normalize_question("Where is ASICS?") == normalize_question("  where is   asics ")


def test_keyword_query_drops_stop_words_and_quotes_terms() -> None:
    assert keyword_query("Where is the ASICS store?") == "'asics' | 'store'"
    assert keyword_query("what is the") is None
    # tsquery operators in user input are neutralised by quoting
    assert "'" in keyword_query("a & b | !c") if keyword_query("a & b | !c") else True


def test_rrf_merge_rewards_agreement() -> None:
    a = [SearchHit(1, "web", "u1", "", "x", similarity=0.9), SearchHit(2, "web", "u2", "", "y", similarity=0.8)]
    b = [SearchHit(2, "web", "u2", "", "y", text_rank=0.5), SearchHit(3, "web", "u3", "", "z", text_rank=0.4)]
    merged = rrf_merge([a, b], limit=3)
    assert [h.chunk_id for h in merged][0] == 2  # in both lists
    assert merged[0].similarity == 0.8 and merged[0].text_rank == 0.5


# ----------------------------------------------------------------------------- chunker


def test_chunker_respects_size_and_overlap() -> None:
    text = "\n".join(f"Line {i} " + "word " * 20 for i in range(100))  # 100 lines x 22 tokens
    chunks = chunk_text(text, lambda t: len(t.split()), max_tokens=100, overlap_tokens=25)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.split()) <= 100
    # consecutive chunks share the overlap line(s)
    first_last_line = chunks[0].splitlines()[-1]
    assert chunks[1].splitlines()[0] == first_last_line


def test_chunker_splits_very_long_lines() -> None:
    text = "word " * 1200
    chunks = chunk_text(text, lambda t: len(t.split()), max_tokens=500, overlap_tokens=50)
    assert all(len(c.split()) <= 500 for c in chunks)
    assert sum(len(c.split()) for c in chunks) >= 1200


def test_chunker_empty() -> None:
    assert chunk_text("   \n\n", lambda t: len(t.split())) == []


# ----------------------------------------------------------------------------- prompt


def test_system_prompt_placeholders_and_braces_safe() -> None:
    out = render_system_prompt("Hi {bot_name} for {client_name}. JSON {x: 1}. {context}", "Bot", "Acme", "CTX")
    assert out == "Hi Bot for Acme. JSON {x: 1}. CTX"
    assert render_system_prompt("No context placeholder", "B", "C", "CTX").endswith("CONTEXT:\nCTX")


def test_build_messages_includes_history_and_context() -> None:
    hits = [SearchHit(1, "web", "https://x/asics", "ASICS", "Second Floor")]
    msgs = build_messages("{context}", "Bot", "Acme", hits, [("q1", "a1")], "q2")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "Second Floor" in msgs[0]["content"] and "https://x/asics" in msgs[0]["content"]


def test_parse_answer_detects_and_strips_tag() -> None:
    text, flagged = parse_answer(f"Sorry, I don't have that information. {NO_ANSWER_TAG}")
    assert flagged and NO_ANSWER_TAG not in text
    assert parse_answer("<think>hmm</think>It is on the second floor.") == ("It is on the second floor.", False)


# ----------------------------------------------------------------------------- clients / origins


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.Example.com/path", "www.example.com"),
        ("example.com", "example.com"),
        ("*.example.com", "*.example.com"),
        ("localhost:5500", "localhost:5500"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


def test_normalize_domain_rejects_garbage() -> None:
    with pytest.raises(ClientError):
        normalize_domain("not a domain!")


@pytest.mark.parametrize(
    "origin, allowed, ok",
    [
        ("https://www.kochi.lulumall.in", ["kochi.lulumall.in"], True),
        ("https://kochi.lulumall.in", ["www.kochi.lulumall.in"], True),
        ("https://evil.com", ["kochi.lulumall.in"], False),
        ("https://kochi.lulumall.in.evil.com", ["kochi.lulumall.in"], False),
        ("https://shop.example.com", ["*.example.com"], True),
        ("https://example.com", ["*.example.com"], True),
        ("http://localhost:5500", ["localhost:5500"], True),
        ("http://localhost:8080", ["localhost:5500"], False),
        (None, ["example.com"], False),
        ("null", ["example.com"], False),
    ],
)
def test_origin_allowed(origin: str | None, allowed: list[str], ok: bool) -> None:
    assert origin_allowed(origin, allowed) is ok


def test_slug_validation() -> None:
    assert validate_slug(" Lulu-Kochi ") == "lulu-kochi"
    for bad in ("-x", "a b", "x" * 70, "ä"):
        with pytest.raises(ClientError):
            validate_slug(bad)


def test_normalize_domains_dedupes() -> None:
    assert normalize_domains(["a.com", "https://a.com/", ""]) == ["a.com"]


# ----------------------------------------------------------------------------- security


def test_passwords() -> None:
    h = hash_password("correct horse battery")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")
    assert not verify_password("not-a-hash", "x")
    assert password_problem("short") and password_problem("1234567890") and password_problem("long enough pass") is None


def test_secret_encryption_roundtrip() -> None:
    token = encrypt_secret("sk-abcdef123456")
    assert "sk-abcdef" not in token
    assert decrypt_secret(token) == "sk-abcdef123456"
    assert decrypt_secret("garbage") is None
    assert mask_secret("sk-abcdef123456") == "••••3456"


def test_loopback_detection_and_provider_presets() -> None:
    assert is_loopback_url("http://localhost:11434/v1")
    assert is_loopback_url("http://127.0.0.1:8000")
    assert not is_loopback_url("http://host.docker.internal:7800/v1")
    assert {"openai", "anthropic", "gemini", "moonshot", "deepseek", "mistral", "groq", "openrouter", "bionic", "ollama", "vllm", "lmstudio"} <= set(PROVIDERS)
    assert all(not p.cloud for p in PROVIDERS.values() if p.key in ("bionic", "ollama", "vllm", "lmstudio"))


# ----------------------------------------------------------------------------- scheduler


def _at(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC"))


def test_crawl_due_daily() -> None:
    schedule = {"frequency": "daily", "time": "03:00"}
    assert crawl_due(schedule, _at(2026, 10, 8, 3, 5), None)
    assert not crawl_due(schedule, _at(2026, 10, 8, 2, 59), _at(2026, 10, 7, 3, 1).isoformat())
    assert not crawl_due(schedule, _at(2026, 10, 8, 4, 0), _at(2026, 10, 8, 3, 1).isoformat())
    assert crawl_due(schedule, _at(2026, 10, 9, 3, 0), _at(2026, 10, 8, 3, 1).isoformat())


def test_crawl_due_weekly_and_off() -> None:
    weekly = {"frequency": "weekly", "time": "03:00", "weekday": 0}  # Mondays
    monday = _at(2026, 10, 5, 3, 10)  # 2026-10-05 is a Monday
    assert crawl_due(weekly, monday, _at(2026, 9, 28, 3, 1).isoformat())
    assert not crawl_due(weekly, _at(2026, 10, 7, 12, 0), monday.isoformat())
    assert not crawl_due({"frequency": "off"}, monday, None)


@pytest.mark.parametrize(
    "reply, flagged",
    [
        ("I'm sorry, but I don't have that information. Please contact the mall.", True),
        ("I don't have information about helicopter pads at the mall.", True),
        ("Sorry, I couldn't find any details on that.", True),
        ("ASICS is on the Second Floor, open 10 AM to 10 PM.", False),
        ("We have information desks on every floor.", False),
    ],
)
def test_parse_answer_detects_untagged_no_answer(reply: str, flagged: bool) -> None:
    assert parse_answer(reply)[1] is flagged


# ----------------------------------------------------------------------------- LLM options
@pytest.mark.parametrize(
    "provider, model, expected",
    [
        ("gemini", "gemini-3.5-flash", {"thinkingConfig": {"thinkingLevel": "minimal"}}),
        ("gemini", "gemini-3.5-flash-lite", {"thinkingConfig": {"thinkingLevel": "minimal"}}),
        ("gemini", "gemini-3.1-pro-preview", {"thinkingConfig": {"thinkingLevel": "low"}}),
        ("gemini", "gemini-2.5-flash", {"thinkingConfig": {"thinkingBudget": 0}}),
        ("gemini", "gemini-2.0-flash", {}),
        ("gemini", "gemini-flash-latest", {}),
        ("openai", "gpt-4o-mini", {}),
    ],
)
def test_fast_thinking_for_gemini(provider: str, model: str, expected: dict) -> None:
    from app.llm.client import ModelConfig, _fast_thinking

    assert _fast_thinking(ModelConfig(provider=provider, model_name=model)) == expected


@pytest.mark.parametrize(
    "model, no_sampling, effort",
    [
        ("claude-haiku-5-5", True, True),
        ("claude-sonnet-5-5", True, True),
        ("claude-opus-5-5", True, True),
        ("claude-fable-5-1", True, True),
        ("claude-opus-4-8", True, True),
        ("claude-opus-4-6", False, True),
        ("claude-sonnet-4-6", False, True),
        ("claude-haiku-4-5", False, False),
        ("claude-haiku-4-5-20251001", False, False),
        ("claude-sonnet-4-20250514", False, False),
    ],
)
def test_claude_options(model: str, no_sampling: bool, effort: bool) -> None:
    from app.llm.client import ModelConfig, _claude_options

    options = _claude_options(ModelConfig(provider="anthropic", model_name=model))
    assert options.get("_no_sampling", False) is no_sampling
    assert ("output_config" in options) is effort
    assert _claude_options(ModelConfig(provider="openai", model_name=model)) == {}


def test_complete_omits_temperature_for_current_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    from app.llm.client import ModelConfig, complete

    sent: dict = {}

    def fake_completion(**kwargs):
        sent.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(litellm, "completion", fake_completion)
    with pytest.raises(Exception):  # noqa: B017 - only the request arguments matter here
        complete(ModelConfig(provider="anthropic", model_name="claude-haiku-5-5", api_key="k"), [{"role": "user", "content": "hi"}])
    assert "temperature" not in sent and sent["output_config"] == {"effort": "low"} and "_no_sampling" not in sent
    with pytest.raises(Exception):  # noqa: B017
        complete(ModelConfig(provider="openai", model_name="gpt-4o-mini", api_key="k"), [{"role": "user", "content": "hi"}])
    assert sent["temperature"] == 0.2


# ----------------------------------------------------------------------------- model discovery
def _fake_get(calls: list[dict], payload: dict, status: int = 200):
    import httpx

    def fake_get(self, url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    return fake_get


@pytest.fixture(autouse=True)
def _empty_model_list_cache() -> None:
    from app.llm import client

    client._model_list_cache.clear()


def test_list_models_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.llm.client import list_available_models

    calls: list[dict] = []
    monkeypatch.setattr(httpx.Client, "get", _fake_get(calls, {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-5-5"}]}))
    assert list_available_models("anthropic", "https://api.anthropic.com", "sk-ant-x") == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert calls[0]["url"] == "https://api.anthropic.com/v1/models"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-x"
    # Repeat lookups come from the cache; refresh bypasses it.
    list_available_models("anthropic", "https://api.anthropic.com", "sk-ant-x")
    assert len(calls) == 1
    list_available_models("anthropic", "https://api.anthropic.com", "sk-ant-x", use_cache=False)
    assert len(calls) == 2


def test_list_models_gemini_keeps_chat_models(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.llm.client import list_available_models

    models = [
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]
    calls: list[dict] = []
    monkeypatch.setattr(httpx.Client, "get", _fake_get(calls, {"models": models}))
    assert list_available_models("gemini", None, "key") == ["gemini-3.5-flash"]
    assert calls[0]["url"].endswith("/v1beta/models")


def test_list_models_openai_compatible_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.llm.client import LLMError, list_available_models

    calls: list[dict] = []
    monkeypatch.setattr(httpx.Client, "get", _fake_get(calls, {"data": [{"id": "qwen2.5:7b"}]}))
    assert list_available_models("ollama", "http://host.docker.internal:11434/v1/", None) == ["qwen2.5:7b"]
    assert calls[0]["url"] == "http://host.docker.internal:11434/v1/models"
    assert calls[0]["headers"] == {}

    monkeypatch.setattr(httpx.Client, "get", _fake_get([], {"error": "bad key"}, status=401))
    with pytest.raises(LLMError, match="API key"):
        list_available_models("openai", None, "sk-bad")
    with pytest.raises(LLMError, match="API key first"):
        list_available_models("openai", None, None)


def test_documents_any_topic_rule_is_optional() -> None:
    hits = [SearchHit(1, "doc", "letter.pdf", "Letter", "The road will be repaired after the new work starts.")]
    plain = build_messages("{context}", "Bot", "Acme", hits, [], "When is the road repaired?")
    assert "ADDITIONAL RULE" not in plain[0]["content"]
    wide = build_messages("{context}", "Bot", "Acme", hits, [], "When is the road repaired?", documents_any_topic=True)
    assert "ADDITIONAL RULE" in wide[0]["content"] and "Acme's business" in wide[0]["content"]
