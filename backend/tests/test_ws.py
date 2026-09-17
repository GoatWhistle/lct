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
from app.scenarios import store
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


def test_score_waits_for_self_assessment(client):
    """Курсант сначала сверяет своё ощущение с объективной картиной:
    расхождение самооценки с автооценкой — отдельный материал для преподавателя."""
    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/observe/{session_id}") as observer:
            observer.receive_json()
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "call.answer"})
                trainee.send_json({"type": "kio.patch", "fields": {"address": "улица Ленина, 14"}})
                trainee.send_json({"type": "call.hangup"})

                # Преподавателю и монитору — сразу: им ждать нечего.
                assert read_until(observer, "score.ready")["session_id"] == str(session_id)
                wait_for(lambda: state.score is not None)

                trainee.send_json({"type": "self_assessment.submit",
                                   "missed": ["q_people"], "comment": "растерялся на адресе"})
                message = read_until(trainee, "score.ready")

    assert message["session_id"] == str(session_id)
    assert state.self_assessment == {"missed": ["q_people"], "comment": "растерялся на адресе"}
    assert state.score["score_auto"] >= 0


def test_trainee_gets_no_score_without_self_assessment(client):
    """Отсутствие события проверяется подпиской на очередь курсанта:
    ждать его из сокета нечем — чтение заблокируется навсегда."""
    from app.domain.events import ScoreReady

    with lesson(client) as (session_id, _):
        state = hub.get(session_id)
        with hub.trainee(session_id) as queue:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "call.answer"})
                trainee.send_json({"type": "call.hangup"})
                wait_for(lambda: state.score is not None)
                time.sleep(0.3)

            delivered = []
            while not queue.empty():
                delivered.append(queue.get_nowait())

    assert not any(isinstance(event, ScoreReady) for event in delivered), (
        "оценка ушла курсанту до самооценки"
    )


def test_checklist_is_closed_until_the_call_is_over(client):
    """Во время звонка чек-лист — это содержимое подсказок."""
    with lesson(client) as (session_id, _):
        during = client.get(f"/api/sessions/{session_id}/checklist")
        assert during.status_code == 409, during.text

        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            trainee.send_json({"type": "call.hangup"})
            wait_for(lambda: hub.get(session_id).ended)

        after = client.get(f"/api/sessions/{session_id}/checklist")
        assert after.status_code == 200
        assert all(item["question"] for item in after.json())


def test_report_shows_missed_questions_and_self_assessment_gap(client):
    """Главное в разборе: по пропущенному пункту виден эталонный вопрос,
    а расхождение самооценки — отдельным блоком."""
    with lesson(client) as (session_id, _):
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            trainee.send_json({"type": "hint.request"})
            trainee.send_json({"type": "kio.patch", "fields": {"address": "улица Ленина, 14", "dds": "01"}})
            trainee.send_json({"type": "call.hangup"})
            wait_for(lambda: hub.get(session_id).score is not None)
            # Курсант считает, что пропустил только адрес — а не добыл он всё.
            trainee.send_json({"type": "self_assessment.submit", "missed": ["q_address"], "comment": "торопился"})
            wait_for(lambda: hub.get(session_id).self_assessed)

        response = client.get(f"/api/sessions/{session_id}/report")
    assert response.status_code == 200, response.text
    report = response.json()

    assert report["missed_checklist"], "не добытые пункты должны быть перечислены"
    questions = {item["checklist_id"]: item["question"] for item in report["reference_questions"]}
    assert all(questions.get(item) for item in report["missed_checklist"]), "у пропущенного нет эталонного вопроса"

    for finding in report["findings"]:
        assert finding["code"] and finding["fact"], "отметка без обоснования"

    diff = report["self_assessment_diff"]
    assert "q_address" in diff["overcautious"] or "q_address" in diff["noticed"]
    assert diff["unnoticed"], "курсант не заметил часть пропущенного — это и есть материал разбора"
    assert report["hints_used"], "использованные подсказки попадают в разбор"


def test_instructor_correction_keeps_the_automatic_score(client):
    with lesson(client) as (session_id, _):
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            trainee.send_json({"type": "call.hangup"})
            wait_for(lambda: hub.get(session_id).score is not None)

        auto = client.get(f"/api/sessions/{session_id}/report").json()["score_auto"]
        corrected = client.patch(
            f"/api/sessions/{session_id}/report",
            json={"score_final": 80.0, "comment": "связь рвалась не по вине курсанта"},
        ).json()

    assert corrected["score_final"] == 80.0
    assert corrected["score_auto"] == auto, "автооценка должна сохраниться рядом"
    assert corrected["overridden_by"] == "преподаватель"


def test_soft_directive_changes_how_the_caller_sounds(client):
    from app.domain.events import Mood

    with lesson(client) as (session_id, control):
        state = hub.get(session_id)
        control.send_json({"type": "director.inject", "directive": "turns_aggressive", "mode": "next_turn"})
        wait_for(lambda: state.persona.directive == "turns_aggressive")

    assert state.persona.mood is Mood.AGGRESSIVE, "директива должна перекрывать дугу сценария"


def test_wrong_address_makes_the_operator_ask_again(client):
    """Жёсткая директива правит факты: раскрытый адрес снимается,
    и в оценке полнота опроса снова считает его недобытым."""
    with lesson(client) as (session_id, control):
        state = hub.get(session_id)
        if state.slots is None:
            pytest.skip("нет модели эмбеддингов — make models")
        state.slots.hear("Назовите адрес")
        assert "f_address" in state.slots.revealed

        control.send_json({"type": "director.inject", "directive": "address_wrong", "mode": "next_turn"})
        wait_for(lambda: "f_address" not in state.slots.revealed)

    assert "f_address" in state.slots.missing_required()


def test_second_victim_corrects_the_reference(client):
    with lesson(client) as (session_id, control):
        state = hub.get(session_id)
        before = state.scenario.ground_truth.victims
        control.send_json({"type": "director.inject", "directive": "second_victim", "mode": "next_turn"})
        wait_for(lambda: state.scenario.ground_truth.victims != before)

    assert state.scenario.ground_truth.victims == before + 1
    library = store.get(state.scenario_id)
    assert library.ground_truth.victims == before, "правка занятия не должна менять библиотеку"


def test_dropped_line_ends_the_call_and_starts_callback(client):
    from app.domain.timers import TimerCode

    with lesson(client) as (session_id, control):
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            wait_for(lambda: state.started_at)
            control.send_json({"type": "director.inject", "directive": "line_dropped", "mode": "immediate"})
            message = read_until(trainee, "call.ended")

    assert message["reason"] == "dropped"
    assert TimerCode.CALLBACK in state.timers.timers, "норматив обратного дозвона должен пойти"


def test_free_text_directive_says_it_needs_network(client):
    """Офлайн-дерево предгенерировано: произвольную фразу взять неоткуда."""
    with lesson(client) as (session_id, control):
        with client.websocket_connect(f"/ws/observe/{session_id}") as observer:
            observer.receive_json()
            control.send_json({"type": "director.inject", "directive": "скажи, что у тебя кот на балконе", "mode": "next_turn"})
            message = read_until(observer, "error")

    assert message["code"] == "directive_needs_network"
