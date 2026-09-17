"""Каналы занятия целиком: преподаватель запускает, курсант работает,
наблюдатель смотрит и не может вмешаться.

Проверяются пункты приёмки карточки lct-05, а не отдельные функции.
"""

import contextlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        hub.journal = None  # тесты не пишут в БД: проверяется поведение каналов
        yield test_client


def wait_for(predicate, timeout: float = 3.0):
    """Ждёт истинного значения. Для чисел не годится: опрос в тесте длится
    0 мс, и ноль — законный результат. Для них есть wait_value."""
    return _wait(predicate, timeout, lambda value: bool(value))


def wait_value(predicate, timeout: float = 3.0):
    """Ждёт любого значения, кроме None: ноль миллисекунд — тоже измерение."""
    return _wait(predicate, timeout, lambda value: value is not None)


def _wait(predicate, timeout: float, ready):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if ready(value):
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались")


def read_until(ws, event_type: str, limit: int = 20) -> dict:
    """Пропускает такты таймера: они идут раз в секунду и мешают разбирать поток."""
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == event_type:
            return message
    raise AssertionError(f"событие {event_type} не пришло")


@contextlib.contextmanager
def lesson(client, mode: str = "training"):
    """Занятие, запущенное преподавателем.

    Сокет закрывается выходом из контекстного менеджера, а не `close()`:
    `close()` шлёт кадр закрытия, но не дожидается завершения задачи
    соединения, и тестовый клиент потом не может закрыться.
    """
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json(
            {
                "type": "scenario.start",
                "scenario_id": "fire-apartment-l2",
                "trainee": "Иванов И.И.",
                "mode": mode,
            }
        )
        wait_for(lambda: hub.get(session_id))
        yield session_id, control


def test_observer_joining_midway_sees_the_whole_state(client):
    """Монитор в классе включают посреди занятия — он обязан показать
    текущее состояние, а не ждать следующего события."""
    with lesson(client) as (session_id, _):
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            trainee.send_json({"type": "kio.patch", "fields": {"floor": "5"}})
            wait_for(lambda: hub.get(session_id).kio.floor == "5")

            with client.websocket_connect(f"/ws/observe/{session_id}") as observer:
                snapshot = observer.receive_json()

    assert snapshot["type"] == "session.snapshot"
    assert snapshot["scenario_title"] == "Пожар в квартире, паникующий заявитель"
    assert snapshot["kio"]["floor"] == "5", "карточка в снимке отстаёт от состояния"
    assert snapshot["mode"] == "training"
    assert snapshot["required_fields"], "обязательные поля не доехали до наблюдателя"


def test_interview_timer_starts_on_answer_and_stops_on_dispatch(client):
    """Центральная метрика продукта. Таймер останавливается событием,
    а не таймаутом: иначе время опроса недоказуемо."""
    from app.domain.timers import TimerCode

    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            wait_for(lambda: TimerCode.INTERVIEW in state.timers.timers)
            assert state.timers.measured_ms(TimerCode.INTERVIEW) is None, "таймер уже остановлен"

            trainee.send_json({"type": "dds.dispatch", "service": "01"})
            measured = wait_value(lambda: state.timers.measured_ms(TimerCode.INTERVIEW))

    assert measured >= 0
    assert state.kio.dds.value == "01"
    assert state.dispatched_card is not None, "карточка не заморожена снимком"


def test_card_edit_reaches_observer(client):
    with lesson(client) as (session_id, _):
        with client.websocket_connect(f"/ws/observe/{session_id}") as observer:
            observer.receive_json()  # снимок при подключении
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "kio.patch", "fields": {"address": "улица Ленина, 14"}})
                message = read_until(observer, "kio.state")

    assert message["kio"]["address"] == "улица Ленина, 14"


def test_observer_cannot_influence_the_lesson(client):
    """Разделение прав архитектурное: на сокете наблюдателя нет ни одного
    обработчика входящих, поэтому влиять нечем."""
    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/observe/{session_id}") as observer:
            observer.receive_json()
            observer.send_json({"type": "kio.patch", "fields": {"address": "подмена"}})
            observer.send_json({"type": "session.stop"})
            time.sleep(0.3)

        assert state.kio.address is None, "наблюдатель изменил карточку"
        assert not state.ended, "наблюдатель завершил занятие"


def test_hint_is_denied_in_exam_and_given_in_training(client):
    with lesson(client, mode="exam") as (session_id, _):
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "hint.request"})
            message = read_until(trainee, "error")
        assert message["code"] == "hint_denied_in_exam"

    with lesson(client, mode="training") as (session_id, _):
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "hint.request"})
            message = read_until(trainee, "hint.shown")
        assert message["question"], "подсказка без вопроса"
        assert hub.get(session_id).hints_shown, "использование подсказки не зафиксировано"


def test_instructor_cannot_touch_the_card(client):
    """В канале `control` нет ни одной команды, меняющей карточку курсанта."""
    from app.domain.events import InstructorToServer
    from typing import get_args

    union = get_args(get_args(InstructorToServer)[0])
    fields = {name for model in union for name in model.model_fields}
    assert "fields" not in fields and "kio" not in fields


def test_call_socket_refuses_session_that_was_not_started(client):
    with client.websocket_connect(f"/ws/call/{uuid4()}") as trainee:
        message = trainee.receive_json()
    assert message["type"] == "error" and message["code"] == "session_not_found"


def test_audio_frames_reach_the_server(client):
    """Веха lct-08: кадры PCM16 16 кГц по 20 мс долетают до бэкенда."""
    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            for _ in range(50):
                trainee.send_bytes(b"\x00\x00" * 320)  # секунда тишины
            trainee.send_bytes(b"\x00" * 100)  # кадр не того размера
            wait_value(lambda: state.bad_frames or None)

        assert state.audio_frames == 50
        assert state.bad_frames == 1, "кадр не того размера должен отбрасываться, а не считаться звуком"


def test_events_still_work_between_audio_frames(client):
    """Звук и события идут по одному сокету: бинарь не должен ломать разбор JSON."""
    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_bytes(b"\x00\x00" * 320)
            trainee.send_json({"type": "kio.patch", "fields": {"floor": "5"}})
            trainee.send_bytes(b"\x00\x00" * 320)
            wait_for(lambda: state.kio.floor == "5")
        assert state.audio_frames == 2
