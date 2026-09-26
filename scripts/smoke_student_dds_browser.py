#!/usr/bin/env python3
"""Live-browser acceptance of trainee KIO -> instructor review -> DDS status.

Only use with a disposable loopback stack/database. The script creates unique
accounts and one group, then removes every row it created in a finally block.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from playwright.async_api import async_playwright
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def require_loopback(value: str, name: str) -> None:
    if urlsplit(value).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{name} must use loopback")


async def login(page, base: str, user: str, password: str, target: str) -> None:
    await page.goto(f"{base}{target}", wait_until="domcontentloaded")
    login_heading = page.get_by_role("heading", name="Учебный симулятор системы-112")
    if await login_heading.is_visible():
        await page.get_by_label("Логин").fill(user)
        await page.get_by_label("Пароль").fill(password)
        await page.get_by_role("button", name="Войти", exact=True).click()
    await page.get_by_role("heading", name={"/profile": "Профиль курсанта", "/instructor": "Рабочее место преподавателя"}[target]).wait_for()


async def main(args: argparse.Namespace) -> int:
    require_loopback(args.frontend_url, "frontend-url")
    require_loopback(args.database_url, "database-url")

    from app.api.auth import hash_password
    from app.db.models import AuditLog, Group, Scenario as ScenarioRow, ScenarioSubmission, Session, Trainee, User
    from app.domain import ekp
    from app.scenarios import store

    scenarios = store.load_from_disk(args.scenarios)
    source = next((item for item in scenarios if item.id == args.source_scenario), None)
    if source is None or source.ground_truth.incident_code is None or source.ground_truth.dds is None:
        raise ValueError(f"source scenario must be a classified DDS case: {args.source_scenario}")
    incident_group = ekp.incident(source.ground_truth.incident_code).group
    service = source.ground_truth.dds.value
    suffix = secrets.token_hex(5)
    teacher_login = f"browser-i-{suffix}"
    trainee_login = f"browser-t-{suffix}"
    teacher_password = secrets.token_urlsafe(24)
    trainee_password = secrets.token_urlsafe(24)
    teacher_name = f"Browser instructor {suffix}"
    trainee_name = f"Browser trainee {suffix}"
    group = Group(name=f"browser-smoke-{suffix}", owner_login=teacher_login)
    trainee = Trainee(name=trainee_name)
    session_ids: list[UUID] = []
    submission_id: UUID | None = None
    published_scenario_id: str | None = None
    session_id: UUID | None = None
    result: dict = {"checks": {}, "source_scenario": source.id}
    engine = create_async_engine(args.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as db:
            db.add(group)
            await db.flush()
            trainee.group_id = group.id
            db.add(trainee)
            await db.flush()
            db.add_all([
                User(login=teacher_login, full_name=teacher_name,
                     password_hash=hash_password(teacher_password), role="instructor", blocked=False),
                User(login=trainee_login, full_name=trainee_name,
                     password_hash=hash_password(trainee_password), role="trainee",
                     trainee_id=trainee.id, service=service, blocked=False),
            ])
            await db.commit()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            student_page = await browser.new_page(ignore_https_errors=True)
            await login(student_page, args.frontend_url, trainee_login, trainee_password, "/profile")

            title = f"КИО из браузера {suffix}"
            address = f"Москва, учебная улица, дом {suffix[:2]}"
            description = "В учебном помещении виден дым, требуется проверка и выезд службы."
            await student_page.get_by_label("Название").fill(title)
            await student_page.locator('input[aria-label="Адрес"]').fill(address)
            await student_page.locator('textarea[aria-label="Описание"]').fill(description)
            await student_page.get_by_label("Категория происшествия").select_option(value=str(incident_group))
            await student_page.get_by_role("group", name="Что случилось?").get_by_role(
                "button", name="Пожар", exact=True
            ).click()
            for index, sign in enumerate(source.signs, start=1):
                picker = student_page.get_by_label(f"Признак происшествия {index}")
                await picker.wait_for(state="visible")
                await picker.select_option(label=sign)
            await student_page.get_by_role("button", name="Сохранить черновик преподавателю").click()
            await student_page.get_by_text("Черновик КИО отправлен и ожидает проверки.", exact=True).wait_for()
            result["checks"]["student_submitted_kio_from_profile"] = True

            async with factory() as db:
                deadline = time.monotonic() + 10
                submission = None
                while time.monotonic() < deadline:
                    submission = await db.scalar(select(ScenarioSubmission).where(ScenarioSubmission.title == title))
                    if submission is not None:
                        break
                    await asyncio.sleep(0.1)
                if submission is None:
                    raise RuntimeError("browser-submitted KIO was not persisted in PostgreSQL")
                submission_id = submission.id

            instructor_page = await browser.new_page(ignore_https_errors=True)
            await login(instructor_page, args.frontend_url, teacher_login, teacher_password, "/instructor")
            proposal = instructor_page.locator(".student-scenario-proposal").filter(has_text=title)
            await proposal.wait_for(state="visible")
            await proposal.get_by_role("button", name="Проверил(а), утвердить в банк ДДС").click()
            await proposal.wait_for(state="hidden")
            result["checks"]["instructor_approved_in_browser"] = True

            await instructor_page.get_by_label("Источник карточек").select_option(label="Сформированные курсантами")
            scenario_select = instructor_page.get_by_label("Сценарий занятия")
            await scenario_select.wait_for()
            deadline = time.monotonic() + 10
            selected_value = None
            while time.monotonic() < deadline:
                options = await scenario_select.locator("option").all()
                for option in options:
                    if title in (await option.text_content() or ""):
                        selected_value = await option.get_attribute("value")
                        break
                if selected_value:
                    break
                await asyncio.sleep(0.1)
            if not selected_value:
                raise RuntimeError("approved trainee KIO did not enter instructor's DDS scenario bank")
            published_scenario_id = selected_value
            await scenario_select.select_option(value=selected_value)
            trainee_select = instructor_page.get_by_label("Учётная запись курсанта")
            trainee_options = await trainee_select.locator("option").all()
            trainee_value = None
            for option in trainee_options:
                if trainee_name in (await option.text_content() or ""):
                    trainee_value = await option.get_attribute("value")
                    break
            if not trainee_value:
                raise RuntimeError("temporary trainee account is missing from instructor's trainee list")
            await trainee_select.select_option(value=trainee_value)
            await instructor_page.get_by_role("button", name="Начать занятие").click()
            await instructor_page.wait_for_function("new URL(location.href).searchParams.has('session')", timeout=15000)
            session_id = UUID(urlsplit(instructor_page.url).query.split("session=", 1)[1].split("&", 1)[0])
            session_ids.append(session_id)
            await instructor_page.get_by_role("link", name=re.compile("открыть ДДС")).wait_for()
            result["checks"]["instructor_started_dds_lesson"] = True

            await student_page.goto(f"{args.frontend_url}/dds?session={session_id}&role=dds_{service}", wait_until="domcontentloaded")
            await student_page.locator(".dds-connection-live").filter(has_text="на связи").wait_for(timeout=15000)
            queue_row = student_page.locator("tr").filter(has_text=title)
            await queue_row.wait_for(state="visible", timeout=15000)
            await queue_row.get_by_role("button", name="Открыть карточку").click()
            await student_page.locator(".dds-case-address").get_by_text(address, exact=True).wait_for()
            result["checks"]["approved_kio_received_in_live_dds"] = True

            service_buttons = student_page.locator(".dds-service-recipient-list .dds-service-recipient")
            await service_buttons.first.wait_for(state="visible")
            result["recipient_buttons"] = await service_buttons.all_text_contents()
            managed_button = None
            for index in range(await service_buttons.count()):
                candidate = service_buttons.nth(index)
                if await candidate.is_enabled():
                    managed_button = candidate
                    break
            if managed_button is None:
                raise RuntimeError(f"no manageable DDS service button: {result['recipient_buttons']}")
            managed_button_text = await managed_button.locator("b").inner_text()
            await managed_button.click()
            classification = await student_page.locator(".dds-case-classification").inner_text()
            result["actual_managed_service_button"] = " ".join(managed_button_text.split())
            result["service_assignment_matches_card"] = result["actual_managed_service_button"] in classification
            if not result["service_assignment_matches_card"]:
                raise RuntimeError("enabled managed-service button differs from the service shown on the card")
            await student_page.locator(".dds-case-drawer-status").wait_for(state="visible", timeout=5000)
            status_controls = student_page.locator('select[aria-label^="Следующий статус "]')
            if await status_controls.count() == 0:
                raise RuntimeError("DDS status pane opened without a status control: " +
                                   (await student_page.locator("body").inner_text())[-1600:])
            status_select = status_controls.first
            await status_select.select_option(label="Принята")
            await student_page.get_by_label("Основание статуса").fill("Карточка получена и принята ДДС.")
            await student_page.get_by_label("Полученные сведения").fill("Направление реагирования подтверждено.")
            await student_page.get_by_role("button", name="Сохранить статус").click()
            await student_page.locator(".dds-status-history").get_by_text("Принята", exact=False).first.wait_for()
            result["checks"]["dds_primary_status_saved"] = True

            crew_select = student_page.get_by_label(f"Наряд {service}")
            crew_options = await crew_select.locator("option").all()
            crew_value = None
            for option in crew_options:
                candidate = await option.get_attribute("value")
                if candidate:
                    crew_value = candidate
                    break
            if crew_value:
                await crew_select.select_option(value=crew_value)
            await status_select.select_option(label="Начало реагирования")
            await student_page.get_by_label("Основание статуса").fill("Получен доклад о начале реагирования.")
            await student_page.get_by_label("Полученные сведения").fill("Бригада выехала к месту происшествия.")
            await student_page.get_by_role("button", name="Сохранить статус").click()
            history = student_page.locator(".dds-status-history")
            await history.get_by_text("Начало реагирования", exact=False).first.wait_for()
            result["checks"]["dds_followup_status_saved"] = True

            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                await student_page.screenshot(path=args.output, full_page=True)
            result["session_id"] = str(session_id)
            result["scenario_id"] = published_scenario_id
            result["managed_service"] = service
            result["all_checks_passed"] = all(result["checks"].values())
            await browser.close()

        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.json_output:
            Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_output).write_text(rendered + "\n", encoding="utf-8")
        return 0 if result["all_checks_passed"] else 1
    finally:
        async with factory() as db:
            await db.execute(delete(Session).where(or_(
                Session.trainee_id == trainee.id,
                Session.id.in_(session_ids) if session_ids else False,
            )))
            if submission_id is not None:
                await db.execute(delete(ScenarioSubmission).where(ScenarioSubmission.id == submission_id))
            if published_scenario_id:
                await db.execute(delete(ScenarioRow).where(ScenarioRow.id == published_scenario_id))
            await db.execute(delete(AuditLog).where(AuditLog.actor.in_([teacher_login, trainee_login])))
            await db.execute(delete(User).where(User.login.in_([teacher_login, trainee_login])))
            await db.execute(delete(Trainee).where(Trainee.name == trainee_name))
            await db.execute(delete(Group).where(Group.name == group.name))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--scenarios", type=Path, default=ROOT / "scenarios")
    parser.add_argument("--source-scenario", default="t01-1-fire-container")
    parser.add_argument("--output", help="optional DDS screenshot path")
    parser.add_argument("--json-output", help="optional machine-readable evidence path")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
