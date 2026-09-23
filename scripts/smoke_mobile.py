#!/usr/bin/env python3
"""Browser smoke for the local demo, including mobile layout and DDS workflow."""

import argparse
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from load_browser import local_url


def layout(page) -> dict:
    return page.evaluate(
        """() => {
          const root = document.documentElement;
          const visible = [...document.querySelectorAll('body *')].filter((element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            const closed = element.closest('details:not([open])');
            return (!closed || element.tagName === 'SUMMARY') && style.display !== 'none'
              && style.visibility !== 'hidden' && rect.width > 0;
          });
          return {
            viewport: innerWidth,
            document_width: root.scrollWidth,
            overflowing: visible.filter((element) => {
              const rect = element.getBoundingClientRect();
              return rect.right > innerWidth + 2 || rect.left < -2;
            }).slice(0, 20).map((element) => ({
              tag: element.tagName.toLowerCase(),
              class: element.className || '',
              text: (element.textContent || '').trim().slice(0, 80),
              left: Math.round(element.getBoundingClientRect().left),
              right: Math.round(element.getBoundingClientRect().right),
            })),
            small_controls: [...document.querySelectorAll(
              'button, select, textarea, input:not([type=radio]):not([type=checkbox]), summary'
            )].filter((element) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== 'none' && style.visibility !== 'hidden'
                && !element.disabled && (rect.height < 40 || rect.width < 40);
            }).slice(0, 20).map((element) => ({
              tag: element.tagName.toLowerCase(),
              label: element.getAttribute('aria-label') || element.textContent?.trim().slice(0, 60)
                || element.getAttribute('placeholder') || '',
              width: Math.round(element.getBoundingClientRect().width),
              height: Math.round(element.getBoundingClientRect().height),
            })),
          };
        }"""
    )


def open_instructor(page, base_url: str) -> None:
    page.goto(f"{base_url}/instructor", wait_until="networkidle")
    demo_login = page.get_by_role("button", name="Войти в демо одним нажатием")
    if demo_login.is_visible():
        demo_login.click()
    page.locator("#ai-scenario-editor").wait_for()


def stop_exercise_from_instructor(page, base_url: str, exercise_href: str) -> None:
    session_id = parse_qs(urlparse(exercise_href).query).get("session", [None])[0]
    if not session_id:
        raise RuntimeError(f"в ссылке АРМ нет session id: {exercise_href}")
    page.goto(f"{base_url}/instructor?session={session_id}", wait_until="networkidle")
    page.get_by_role("button", name="Завершить занятие").click()
    page.get_by_text("занятие завершено", exact=True).wait_for()


def choose_trainee(page) -> None:
    # Match the label cell, not any row whose body happens to mention a
    # trainee (the scenario/category tables include that word too).
    exercise_table = page.locator(".instructor-layout > section").first.locator(
        "table.grid"
    ).first
    trainee_row = exercise_table.locator("th").get_by_text(
        "Курсант", exact=True
    ).locator("xpath=..")
    trainee_select = trainee_row.locator("select")
    if trainee_select.count():
        if trainee_select.locator("option").count() < 2:
            raise RuntimeError("Список курсантов не содержит доступного выбора")
        trainee_select.select_option(index=1)
    else:
        trainee_row.locator('input[type="text"]').fill("Мобильный smoke")


def start_exercise(page, label: str, link_row: str) -> str:
    page.get_by_role("radio", name=label).check()
    choose_trainee(page)
    page.get_by_role("button", name="Начать занятие").click()
    row = page.locator("tr").filter(has_text=link_row).last
    link = row.get_by_role("link", name="открыть", exact=False)
    link.wait_for()
    href = link.get_attribute("href")
    if not href:
        raise RuntimeError(f"не получена ссылка {link_row}")
    return href


def exercise_dds_flow(page, scenario_title: str) -> dict:
    """Accept the card, select its crew and complete the training briefing."""
    service_button = page.locator(".dds-service-dock button").first
    service_title = service_button.locator("b").inner_text()
    service_button.click()

    next_status = page.locator(
        '.dds-status-editor select[aria-label^="Следующий статус"]'
    )
    next_status.wait_for()
    labels = next_status.locator("option").all_text_contents()
    if not any("Принята" in label for label in labels):
        raise RuntimeError(f"статус «Принята» недоступен для {service_title}: {labels}")
    next_status.select_option(value="accepted")
    page.locator('button[aria-label="Сохранить статус"]').click()
    page.wait_for_function("""() => [...document.querySelectorAll(
      '.dds-service-dock button')].some((button) => button.innerText.includes('Принята'))""")

    page.locator(".dds-service-dock button").filter(has_text="Связь").click()
    crew_select = page.locator("#dds-crew")
    crew_select.wait_for()
    options = crew_select.locator("option").all_text_contents()
    crew = next((option.strip() for option in options
                 if option.strip().startswith(service_title)), None)
    if not crew:
        raise RuntimeError(f"для {service_title} нет доступной бригады: {options}")
    crew_select.select_option(label=crew)
    page.get_by_role("button", name="Позвонить старшему группы").click()

    address = page.locator(".dds-case-address > strong").inner_text().strip()
    page.locator("#dds-phone-address").fill(address)
    page.locator("#dds-phone-incident").fill(scenario_title)
    page.locator("#dds-phone-request").fill(
        "Подтвердите выезд и доложите о прибытии."
    )
    page.get_by_role("button", name="Передать сообщение").click()
    transcript = page.locator(".dds-phone-transcript")
    transcript.wait_for()
    page.wait_for_function("""() => {
      const transcript = document.querySelector('.dds-phone-transcript');
      const alert = document.querySelector('.dds-alert[role="alert"]');
      return transcript?.innerText.includes('Адрес:') || Boolean(alert?.innerText.trim());
    }""", timeout=10_000)
    text = transcript.inner_text()
    if "Адрес:" not in text or "Старший группы" not in text:
        alert = page.locator('.dds-alert[role="alert"]')
        error = alert.inner_text() if alert.count() else "нет сообщения об ошибке"
        raise RuntimeError(
            f"учебный доклад не попал в журнал: {text}; ошибка: {error}; "
            f"адрес={address!r}, происшествие={scenario_title!r}"
        )
    return {"service": service_title, "status": "Принята", "crew": crew,
            "brief_recorded": True}


def smoke_dds_queue(page, base_url: str, output: Path, screenshot_suffix: str) -> dict:
    """Check per-card state isolation while a second card arrives in queue."""
    open_instructor(page, base_url)
    queue = page.locator("details.scenario-queue")
    queue.locator("summary").click()
    queue.locator('input[type="checkbox"]').first.check()
    page.get_by_label("Интервал поступления карточек ДДС, секунд").fill("6")
    page.get_by_label("Максимум ожидающих карточек ДДС").fill("1")
    dds_href = start_exercise(page, "Диспетчер ДДС · готовая карточка", "АРМ ДДС")
    page.goto(dds_href, wait_until="networkidle")
    page.locator(".dds-workspace").wait_for()
    first_row = page.locator('.dds-registry tbody tr[aria-current="true"]')
    first_row.wait_for()
    first_number = first_row.locator('td[data-label="Номер"] button').inner_text()
    first_row.get_by_role("button", name="Открыть карточку").click()
    page.locator(".dds-case-layout").wait_for()
    first_status_button = page.locator(".dds-service-dock button").first
    service_title = first_status_button.locator("b").inner_text()
    first_status_button.click()
    next_status = page.locator(
        '.dds-status-editor select[aria-label^="Следующий статус"]'
    )
    next_status.wait_for()
    service_name = next_status.get_attribute("aria-label").removeprefix("Следующий статус ")
    if not any("Принята" in option for option in next_status.locator("option").all_text_contents()):
        raise RuntimeError(f"для первой карточки недоступен статус «Принята»: {service_title}")
    next_status.select_option(value="accepted")
    page.locator('button[aria-label="Сохранить статус"]').click()
    page.wait_for_function(
        """(service) => [...document.querySelectorAll('.dds-service-dock button')]
          .some((button) => button.innerText.includes(service)
            && button.innerText.includes('Принята'))""",
        arg=service_title,
        timeout=5_000,
    )
    page.locator(".dds-case-top button").click()
    page.wait_for_function(
        "document.querySelectorAll('.dds-registry tbody tr').length === 2",
        timeout=20_000,
    )
    rows = page.locator(".dds-registry tbody tr")
    if rows.count() != 2 or page.locator('.dds-registry tbody tr[aria-current="true"]').count() != 1:
        raise RuntimeError("ожидались две карточки в очереди и ровно одна активная")
    second_row = rows.nth(1)
    second_number = second_row.locator('td[data-label="Номер"] button').inner_text()
    second_row.get_by_role("button", name="Открыть карточку").click()
    page.locator(".dds-case-top").get_by_text(second_number, exact=False).wait_for()
    page.locator(".dds-case-top button").click()
    page.locator(".dds-registry tbody tr").filter(has_text=first_number).get_by_role(
        "button", name="Открыть карточку"
    ).click()
    page.locator(".dds-case-top").get_by_text(first_number, exact=False).wait_for()
    restored_status = page.locator(".dds-service-dock button").filter(
        has_text=service_title
    ).first.inner_text()
    if "Принята" not in restored_status:
        raise RuntimeError(f"статус первой карточки потерян после поступления/переключения: {restored_status}")
    page.locator(".dds-case-top button").click()
    screen = layout(page)
    page.screenshot(path=output / f"dds-queue-{screenshot_suffix}.png", full_page=True)
    stop_exercise_from_instructor(page, base_url, dds_href)
    return {"cards_visible": 2, "active_cards": 1,
            "arrival_interval_seconds": 6, "max_waiting": 1,
            "managed_service": service_name,
            "first_card_status_preserved_after_second_arrival": True,
            "switched_to_second_and_back": True,
            "session_finished": True, "screen": screen}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5175")
    parser.add_argument("--output", type=Path, default=Path("/tmp/lct-mobile-smoke"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--login")
    parser.add_argument("--password")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--device-scale-factor", type=float, default=2)
    args = parser.parse_args()
    if args.width < 320 or args.height < 480 or args.device_scale_factor <= 0:
        parser.error("viewport must be at least 320×480 and scale factor must be positive")
    screenshot_suffix = "mobile" if args.width <= 600 else "desktop"
    args.base_url = local_url(args.base_url, {"http", "https"})
    args.output.mkdir(parents=True, exist_ok=True)

    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "viewport": {
            "width": args.width,
            "height": args.height,
            "device_scale_factor": args.device_scale_factor,
            "touch_target_minimum_px": 40 if args.width <= 600 else None,
        },
        "screens": {},
        "workflow": {},
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.device_scale_factor,
            locale="ru-RU",
        )
        page = context.new_page()
        page.goto(f"{args.base_url}/instructor", wait_until="networkidle")
        demo_login = page.get_by_role("button", name="Войти в демо одним нажатием")
        if demo_login.is_visible():
            demo_login.click()
        elif args.login and args.password:
            page.locator('input:not([type="password"])').first.fill(args.login)
            page.locator('input[type="password"]').fill(args.password)
            page.get_by_role("button", name="Войти", exact=True).click()
        elif page.get_by_role("heading", name="Учебный симулятор системы-112").is_visible():
            raise RuntimeError("stand requires --login and --password")
        page.locator("#ai-scenario-editor").wait_for()
        page.screenshot(path=args.output / f"instructor-{screenshot_suffix}.png", full_page=True)
        report["screens"]["instructor"] = layout(page)

        trainee_href = start_exercise(
            page, "Оператор 112 · текстовая вводная", "АРМ курсанта"
        )
        page.goto(trainee_href, wait_until="networkidle")
        page.locator(".kio-arm").wait_for()
        page.screenshot(path=args.output / f"trainee-{screenshot_suffix}.png", full_page=True)
        report["screens"]["trainee"] = layout(page)
        stop_exercise_from_instructor(page, args.base_url, trainee_href)

        open_instructor(page, args.base_url)
        dds_href = start_exercise(
            page, "Диспетчер ДДС · готовая карточка", "АРМ ДДС"
        )
        page.goto(dds_href, wait_until="networkidle")
        page.locator(".dds-workspace").wait_for()
        page.screenshot(path=args.output / f"dds-{screenshot_suffix}.png", full_page=True)
        report["screens"]["dds"] = layout(page)

        active_row = page.locator(".dds-registry tbody tr.dds-selected")
        scenario_title = active_row.locator(
            'td[data-label="Тип происшествия"] .dds-cell-value > span'
        ).inner_text().split(" · ", 1)[0]
        page.get_by_role("button", name="Открыть карточку").first.click()
        page.locator(".dds-case-layout").wait_for()
        page.screenshot(path=args.output / f"dds-card-{screenshot_suffix}.png", full_page=True)
        report["screens"]["dds_card"] = layout(page)
        report["workflow"] = exercise_dds_flow(page, scenario_title)
        page.screenshot(path=args.output / f"dds-workflow-{screenshot_suffix}.png", full_page=True)
        report["screens"]["dds_workflow"] = layout(page)
        page.locator(".dds-service-dock button").last.click()
        page.locator(".dds-done").wait_for()
        report["workflow"]["session_finished"] = True

        queue = smoke_dds_queue(page, args.base_url, args.output, screenshot_suffix)
        report["screens"]["dds_queue"] = queue.pop("screen")
        report["queue"] = queue
        browser.close()

    problems = {
        name: {
            "horizontal_overflow": data["document_width"] > data["viewport"] + 2,
            "small_controls": len(data["small_controls"]),
            "touch_target_check_applies": args.width <= 600,
        }
        for name, data in report["screens"].items()
    }
    report["problems"] = problems
    report["passed"] = all(
        not item["horizontal_overflow"] and (
            not item["touch_target_check_applies"] or item["small_controls"] == 0
        )
        for item in problems.values()
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
