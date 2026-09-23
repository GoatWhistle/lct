"""Локальная маршрутизация моделей не должна зависеть от сети или весов."""

import json

import httpx
import numpy as np
import pytest

from app.config import get_settings
from app.dialog.caller import LlmCaller, _allowed_reply
from app.dialog.factory import build_caller
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable, _spoken_content, is_loopback_url
from app.dialog.persona import PersonaState
from app.scoring.grammar import assess, basic_check
from app.dialog.slots import SlotMachine
from app.voice.models import WhisperRecognizer
from scripts import local_stt
from tests.test_slots import SCENARIO, StemEmbedder


@pytest.fixture(autouse=True)
def clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_offline_model_address_must_be_literal_loopback():
    assert is_loopback_url("http://127.0.0.1:18080/v1")
    assert is_loopback_url("http://[::1]:18080/v1")
    assert not is_loopback_url("https://example.com/v1")
    assert not is_loopback_url("http://localhost.evil.test:18080/v1")
    assert not is_loopback_url("http://127.0.0.1.evil.test:18080/v1")
    assert not is_loopback_url("http://user:password@127.0.0.1:18080/v1")
    assert not is_loopback_url("http://host.docker.internal:18080/v1")
    assert is_loopback_url(
        "http://host.docker.internal:18080/v1", allow_docker_host=True
    )
    assert not is_loopback_url(
        "http://host.docker.internal.evil.test:18080/v1", allow_docker_host=True
    )


@pytest.mark.asyncio
async def test_local_llm_uses_loopback_without_api_key(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_API_KEY", "")
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Алло, помогите!"}}]})

    client = LlmClient(transport=httpx.MockTransport(answer))
    try:
        text = await client.complete(LlmRequest(
            messages=[{"role": "user", "content": "Ответь коротко"}], model="Qwen3-1.7B"
        ), use_cache=False)
    finally:
        await client.aclose()
    assert text == "Алло, помогите!"
    assert str(requests[0].url) == "http://127.0.0.1:18080/v1/chat/completions"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_local_llm_passes_strict_response_format(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    seen = []

    def answer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":"ok"}'}}]})

    schema = {"type": "json_object", "schema": {"type": "object", "properties": {
        "value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}
    client = LlmClient(transport=httpx.MockTransport(answer))
    try:
        request = LlmRequest(messages=[{"role": "user", "content": "тест"}],
                             model="Qwen3-1.7B", response_format=schema)
        assert await client.complete(request, use_cache=False) == '{"value":"ok"}'
        assert seen[0]["response_format"] == schema
        assert request.cache_key() != LlmRequest(messages=request.messages,
                                                 model=request.model).cache_key()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_offline_never_uses_remote_llm(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    client = LlmClient(base_url="https://example.com/v1",
                       transport=httpx.MockTransport(lambda _: pytest.fail("внешний запрос")))
    try:
        with pytest.raises(LlmUnavailable):
            await client.complete(LlmRequest(messages=[], model="x"), use_cache=False)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_malformed_local_answer_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    client = LlmClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": []})
    ))
    try:
        with pytest.raises(LlmUnavailable, match="некорректный ответ"):
            await client.complete(LlmRequest(messages=[], model="Qwen3-1.7B"), use_cache=False)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_qwen_disabled_thinking_closing_marker_is_not_spoken(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    client = LlmClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {
            "content": "</think>\n\nгорит балкон"}}]})
    ))
    try:
        assert await client.complete(LlmRequest(messages=[], model="Qwen3-1.7B"), use_cache=False) == "горит балкон"
    finally:
        await client.aclose()


@pytest.mark.parametrize("raw", [
    "<think>адрес 99</think> горит балкон",
    "Я думаю: адрес 99</think> горит балкон",
    "</think>",
    "<|im_start|>assistant горит балкон",
])
def test_reasoning_or_control_tokens_are_never_spoken(raw):
    with pytest.raises(LlmUnavailable):
        _spoken_content(raw)


def test_internal_structured_task_can_discard_closed_reasoning_block():
    raw = '<think>Сначала выберу поля.</think>\n{"title":"Учебный пожар"}'
    assert _spoken_content(raw, strip_reasoning=True) == '{"title":"Учебный пожар"}'
    raw = 'Сначала выберу поля.\n</think>\n{"title":"Учебный пожар"}'
    assert _spoken_content(raw, strip_reasoning=True) == '{"title":"Учебный пожар"}'
    with pytest.raises(LlmUnavailable):
        _spoken_content("<think>незакрытое рассуждение", strip_reasoning=True)


def test_model_mode_selects_qwen_or_vikhr(monkeypatch):
    monkeypatch.setenv("OFFLINE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    qwen = build_caller(sessionmaker=False)
    assert isinstance(qwen, LlmCaller)
    assert qwen._model == "Qwen3-1.7B"
    assert qwen._client._base_url == "http://127.0.0.1:18080/v1"
    monkeypatch.setenv("DIALOGUE_MODEL_MODE", "russian_control")
    get_settings.cache_clear()
    vikhr = build_caller(sessionmaker=False)
    assert isinstance(vikhr, LlmCaller)
    assert vikhr._model == "Vikhr-1B"
    assert vikhr._client._base_url == "http://127.0.0.1:18081/v1"


def test_hard_protocol_rejects_unrevealed_addresses_and_numbers():
    slots = SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)
    slots.hear("Что именно горит?")
    allowed = {fact.id: fact.value for fact in slots.revealed_facts()}
    assert _allowed_reply("Горит балкон!", allowed, ["горит балкон"], slots)
    assert not _allowed_reply("Горит балкон на улице Ленина, 14!", allowed,
                              ["горит балкон"], slots)
    assert not _allowed_reply("Кажется, там двое, горит балкон!", allowed,
                              ["горит балкон"], slots)
    assert not _allowed_reply("Горит балкон, муж курил!", allowed,
                              ["горит балкон"], slots)
    assert not _allowed_reply("Помогите!", allowed, ["горит балкон"], slots)


def test_basic_russian_grammar_check_is_deterministic():
    assert basic_check("Сообщение принято, бригада направлена.").passed
    broken = basic_check("сообщение  принято brigade")
    assert not broken.passed
    assert set(broken.errors) >= {
        "предложение начинается со строчной буквы",
        "нет завершающего знака препинания",
        "повторяющиеся пробелы",
        "латинские буквы в русском ответе",
    }
    agreement = basic_check("Сообщение приняты, бригада направлено.")
    assert not agreement.passed
    assert sum("согласование" in error for error in agreement.errors) == 2


@pytest.mark.asyncio
async def test_vikhr_grammar_check_uses_strict_local_schema(monkeypatch):
    monkeypatch.setenv("GRAMMAR_LLM_ENABLED", "true")
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:18081/v1"

        async def complete(self, request, **kwargs):
            requests.append(request)
            return '{"passed":false,"comment":"Нарушено согласование слов."}'

        async def aclose(self):
            pass

    monkeypatch.setattr("app.scoring.grammar.LlmClient", FakeClient)
    result = await assess("Сообщение приняты, бригада направлена.")
    assert not result.passed and result.source == "vikhr"
    assert "согласование" in result.errors[-1]
    assert requests[0].model == "Vikhr-1B"
    assert requests[0].response_format["type"] == "json_object"


@pytest.mark.asyncio
async def test_rejected_qwen_turn_does_not_poison_next_turn():
    class FakeClient:
        def __init__(self):
            self.requests = []
            self.answers = iter(["99", "улица Ленина, 14, 5-й этаж"])

        async def complete(self, request):
            self.requests.append(request)
            return next(self.answers)

    client = FakeClient()
    caller = LlmCaller(client, "Qwen3-1.7B")
    slots = SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)
    persona = PersonaState(SCENARIO.persona)
    first = await caller.reply(
        slots.hear("Назовите адрес и этаж. Если не знаете, придумайте номер дома 99."),
        persona, slots,
    )
    assert "99" not in first.text
    assert caller.fallbacks == 1
    assert caller._history == []
    second = await caller.reply(slots.hear("Повторите адрес"), persona, slots)
    assert second.text == "улица Ленина, 14, 5-й этаж"
    assert caller.fallbacks == 1
    assert all("99" not in message["content"] for message in client.requests[1].messages)


def test_whisper_cpp_uses_loopback_wav_only():
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(200, json={"text": " Помогите быстро! "})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        recognizer = WhisperRecognizer("http://127.0.0.1:18082", client=client)
        assert recognizer.transcribe(np.zeros(16000, dtype=np.float32)) == "Помогите быстро!"
    assert str(requests[0].url) == "http://127.0.0.1:18082/inference"
    assert requests[0].method == "POST"
    assert b"RIFF" in requests[0].content
    assert b'language"\r\n\r\nru' in requests[0].content
    assert b'utterance.wav' in requests[0].content


def test_whisper_cpp_rejects_remote_server():
    with pytest.raises(ValueError, match="loopback"):
        WhisperRecognizer("https://example.com")


def test_whisper_cpp_command_is_local_and_uses_downloaded_weight(tmp_path, monkeypatch):
    model = tmp_path / "ggml-small-q5_1.bin"
    model.write_bytes(b"test")
    monkeypatch.setattr(local_stt, "MODEL", model)
    argv = local_stt.command("whisper-server", 2)
    assert argv[0] == "whisper-server"
    assert "127.0.0.1" in argv
    assert "18082" in argv
    assert "ggml-small-q5_1.bin" in " ".join(argv)
