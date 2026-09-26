"""Browser smoke for the DDS archive and instructor lesson/scenario forms."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.environ.get("SMOKE_EVIDENCE_DIR", ROOT / "docs" / "evidence"))
BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:5173").rstrip("/")
CARDS = [
    {
        "card_id": "36814001", "scenario_id": "t01-1", "score_auto": 75,
        "score_final": 75, "ended_at": "2026-09-24T15:00:00+03:00",
        "received_at": "2026-09-24T14:55:00+03:00", "title": "Медицинская помощь",
        "address": "Вороновское, 3", "description": "Учебное обращение.",
        "incident_type": "medical", "victims_count": 1,
        "managed_service": "01", "recipient_services": ["01"],
    },
    {
        "card_id": "36814002", "scenario_id": "t01-2", "score_auto": 90,
        "score_final": 90, "ended_at": "2026-09-25T15:00:00+03:00",
        "received_at": "2026-09-25T14:50:00+03:00", "title": "Пожар в квартире",
        "address": "улица Учебная, 12", "description": "УМП н. — Тест 1; наблюдается дым со стороны подъезда.",
        "incident_type": "fire", "victims_count": 2,
        "managed_service": "01", "recipient_services": ["01", "02"],
    },
]


async def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 964}, device_scale_factor=1)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/sessions/dds-history**", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(CARDS, ensure_ascii=False),
        ))
        await page.goto(f"{BASE_URL}/dds", wait_until="networkidle")
        login = page.get_by_role("button", name="Войти в демо одним нажатием")
        if await login.is_visible():
            await login.click()
        await page.get_by_text("Медицинская помощь", exact=True).wait_for()
        history_rows = page.locator(".dds-history-registry tbody > tr:not(.dds-history-description)")
        search_toggle = page.get_by_role("button", name="Открыть поиск")
        searchbox = page.get_by_role("searchbox", name="Поиск по номеру, адресу или описанию")
        assert await searchbox.count() == 0, "поиск должен быть свёрнут по умолчанию"
        await search_toggle.click()
        await searchbox.wait_for(state="visible")
        await searchbox.fill("Пожар")
        assert await history_rows.count() == 1
        assert "Пожар в квартире" in await history_rows.first.inner_text()
        await page.get_by_role("button", name="Свернуть поиск").click()
        assert await searchbox.is_visible(), "активный поисковый фильтр нельзя прятать"
        await searchbox.fill("")
        await page.get_by_role("button", name="Свернуть поиск").click()
        assert await searchbox.count() == 0, "пустой поиск можно свернуть"
        await page.set_viewport_size({"width": 1280, "height": 800})
        desktop_table_metrics = await page.evaluate("""() => {
          const wrapper = document.querySelector('.dds-history-registry')?.parentElement;
          return { viewport: innerWidth, document: document.documentElement.scrollWidth,
            table: wrapper?.querySelector('table')?.scrollWidth, wrapper: wrapper?.clientWidth,
            tableScrolls: !!wrapper && wrapper.scrollWidth > wrapper.clientWidth };
        }""")
        assert desktop_table_metrics["document"] <= desktop_table_metrics["viewport"]
        assert desktop_table_metrics["tableScrolls"], desktop_table_metrics
        await page.set_viewport_size({"width": 1920, "height": 964})
        notification_filter = page.get_by_role("combobox", name="Фильтр уведомлений")
        await notification_filter.select_option("02")
        filtered_recipient_count = await history_rows.count()
        assert filtered_recipient_count == 1
        assert "Пожар в квартире" in await history_rows.first.inner_text()
        await notification_filter.select_option("all")
        default_first_date = await history_rows.first.locator("td").nth(5).inner_text()
        assert default_first_date == "25.09.2026", default_first_date
        sort_button = page.get_by_role("button", name="Дата ↓")
        await sort_button.click()
        ascending_first_date = await history_rows.first.locator("td").nth(5).inner_text()
        assert ascending_first_date == "24.09.2026", ascending_first_date
        await page.screenshot(path=str(EVIDENCE / "dds-history-browser-smoke-2026-09-26.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(EVIDENCE / "dds-history-mobile-smoke-2026-09-26.png"), full_page=True)
        metrics = await page.evaluate("""() => {
          const wrapper = document.querySelector('.dds-history-registry')?.parentElement;
          return { viewport: innerWidth, document: document.documentElement.scrollWidth,
            table: wrapper?.querySelector('table')?.scrollWidth, wrapper: wrapper?.clientWidth,
            tableScrolls: !!wrapper && wrapper.scrollWidth > wrapper.clientWidth };
        }""")
        await page.set_viewport_size({"width": 1920, "height": 964})
        session_list_statuses: list[int] = []
        page.on("response", lambda response: session_list_statuses.append(response.status)
                if response.url.split("?", 1)[0].endswith("/api/sessions") else None)
        await page.goto(f"{BASE_URL}/instructor", wait_until="networkidle")
        login = page.get_by_role("button", name="Войти в демо одним нажатием")
        if await login.is_visible():
            await login.click()
        await page.get_by_text("История занятий пока пуста", exact=True).wait_for()
        assert session_list_statuses and session_list_statuses[-1] == 200, session_list_statuses
        scenario_filters = page.locator("details.scenario-filters-details")
        await scenario_filters.wait_for()
        assert not await scenario_filters.evaluate("element => element.open"), "редкие фильтры должны быть свёрнуты по умолчанию"
        await page.get_by_role("searchbox", name="Поиск сценария").wait_for()
        await scenario_filters.locator("summary").click()
        await page.get_by_role("combobox", name="Билет заказчика").wait_for()
        level_filter = page.get_by_role("combobox", name="Уровень сложности")
        await level_filter.select_option("L1")
        scenario_options = page.locator("select[aria-label='Сценарий занятия'] option")
        await page.wait_for_function("""() => {
          const select = document.querySelector("select[aria-label='Сценарий занятия']");
          return select && select.options.length > 0 &&
            Array.from(select.options).every(option => option.textContent.includes('L1'));
        }""")
        l1_option_count = await scenario_options.count()
        assert l1_option_count > 0
        await level_filter.select_option("")
        await scenario_filters.locator("summary").click()
        await page.get_by_text("Настроить нормативы и критерии оценки").click()
        criterion = page.get_by_role("spinbutton", name="Лимит решения ДДС, секунд")
        await criterion.wait_for()
        await page.screenshot(path=str(EVIDENCE / "instructor-create-browser-smoke-2026-09-26.png"))
        await criterion.fill("")
        await criterion.press_sequentially("120")
        entered = await criterion.input_value()
        await page.get_by_role("heading", name="Рабочее место преподавателя").click()
        committed = await criterion.input_value()
        assert entered == "120" and committed == "120", {"typed": entered, "committed": committed}
        await page.get_by_text("Настроить веса оценки (необязательно)").click()
        weight = page.get_by_role("spinbutton", name="Вес метрики «Время отработки карточки»")
        await weight.fill("")
        await weight.press_sequentially("2.5")
        weight_entered = await weight.input_value()
        await page.get_by_role("heading", name="Рабочее место преподавателя").click()
        weight_committed = await weight.input_value()
        assert weight_entered == "2.5" and weight_committed == "2.5", {
            "weightTyped": weight_entered, "weightCommitted": weight_committed,
        }
        draft = {
            "id": "draft-smoke", "status": "draft", "generation": "template_copy",
            "body": {
                "id": "draft-smoke", "title": "Демо-сценарий", "type": "fire", "level": "L1",
                "first_line": "Помогите, в подъезде дым!",
                "facts": [{"id": "address", "value": "улица Учебная, дом 12"}],
                "checklist": [{"id": "address", "question": "Где именно дым?", "fact": "улица Учебная, дом 12"}],
            },
        }
        await page.route("**/api/scenarios/drafts/from-template", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(draft, ensure_ascii=False),
        ))
        saved_patches: list[dict] = []
        async def save_draft(route):
            patch = route.request.post_data_json
            saved_patches.append(patch)
            updated = {**draft, "body": {**draft["body"], **patch}}
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(updated, ensure_ascii=False))
        await page.route("**/api/scenarios/drafts/draft-smoke", save_draft)
        await page.route("**/api/scenarios/drafts/draft-smoke/validate", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({
                "valid": True, "errors": [], "ground_truth": {
                    "incident_type": "fire", "dds": "01", "incident_code": "01.01",
                    "notify": ["01"], "required_facts": ["address"],
                    "address": "улица Учебная, дом 12", "victims": 0,
                },
            }, ensure_ascii=False),
        ))
        await page.get_by_role("button", name="Создать черновик").click()
        first_line = page.get_by_role("textbox", name="Первая реплика звонящего")
        await first_line.wait_for()
        await first_line.fill("Помогите, в подъезде сильный дым!")
        await page.get_by_role("button", name="Сохранить", exact=True).click()
        await page.get_by_text("Черновик сохранён.", exact=True).wait_for()
        assert saved_patches == [{"first_line": "Помогите, в подъезде сильный дым!"}], saved_patches
        await page.get_by_role("button", name="Проверить", exact=True).click()
        await page.get_by_text("Проверка пройдена", exact=True).wait_for()
        visible_text = await page.locator("body").inner_text()
        assert "JSON" not in visible_text and '{"address"' not in visible_text
        await page.set_viewport_size({"width": 390, "height": 844})
        instructor_mobile = await page.evaluate("""() => ({
          viewport: innerWidth, document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
        })""")
        await page.screenshot(path=str(EVIDENCE / "instructor-create-mobile-smoke-2026-09-26.png"), full_page=True)
        assert instructor_mobile["document"] <= instructor_mobile["viewport"], instructor_mobile
        report = {"metrics": metrics, "desktopTableMetrics": desktop_table_metrics,
                  "demoSessionListStatus": session_list_statuses[-1], "criterionInput": committed,
                  "weightInput": weight_committed, "scenarioEditor": "no JSON shown",
                  "l1ScenarioOptions": l1_option_count,
                  "humanEditPatch": saved_patches, "instructorMobile": instructor_mobile,
                  "notificationFilteredRows": filtered_recipient_count,
                  "dateSort": {"descendingFirst": default_first_date,
                               "ascendingFirst": ascending_first_date},
                  "pageErrors": errors}
        (EVIDENCE / "dds-history-controls-browser-smoke-2026-09-26.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()
        if metrics["document"] > metrics["viewport"] or not metrics["tableScrolls"] or errors:
            raise SystemExit("DDS mobile smoke failed")


if __name__ == "__main__":
    asyncio.run(main())
