# Формат сценария — YAML

Сценарий — контент, а не код: их пишет лид и методист, читаются глазами, валидируются загрузчиком на старте приложения ([arch/BACKEND.md](../arch/BACKEND.md)). Схема описана Pydantic-моделью в `backend/app/scenarios/schema.py`; `ground_truth` из фактов выводится кодом, а не пишется руками — иначе расхождение факта и эталона штрафует курсанта за правильный ответ.

## Полный пример

```yaml
id: fire-apartment-l2
title: "Пожар в квартире, паникующий заявитель"
type: fire                      # fire | medical | police | other | mkh | era_glonass
level: L2                       # L1 | L2 | L3
topics: [fires]                 # темы для программ занятий
modes: [training, exam, self]   # в каких режимах доступен

persona:
  base: panic                   # calm | panic | aggressive | child | elderly | drunk | evasive | foreigner
  arc:                          # эмоциональная дуга по этапам приёма вызова
    - { stage: registration, mood: panic }
    - { stage: interview,    mood: panic }    # дуга — фон; директивы преподавателя перекрывают её
    - { stage: dispatch,     mood: worried }

background:                     # аудио-фон происшествия под голосом; null, если не нужен
  loop: fire_crackle.mp3
  gain_db: -18

first_line: "Алло! Алло! Помогите! Горим! А-а-а!"   # предгенерируется в WAV при публикации

facts:                          # факты сценария; звонящий выдаёт их только при правильных вопросах
  - { id: f_address,   value: "улица Ленина, 14, кв. 47, 5-й этаж" }
  - { id: f_people,    value: "в квартире жена и ребёнок, не выходят" }
  - { id: f_what_burns,value: "горит балкон, дым пошёл в квартиру" }
  - id: f_gas                          # скрытый факт: раскрывается не вопросом, а подходом
    value: "муж чем-то отравился, боится говорить"
    hidden: true
    reveal_on:
      approach: "спокойно объяснил, зачем нужен адрес и что информация конфиденциальна"

checklist:                      # эталонный опрос для этого типа происшествия
  - { id: q_address,    question: "Адрес и этаж?",               fact: f_address }
  - { id: q_people,     question: "Есть ли люди в помещении?",   fact: f_people }
  - { id: q_what_burns, question: "Что горит?",                  fact: f_what_burns }

ground_truth:                   # выводится из фактов кодом, не пишется вручную
  incident_type: fire
  dds: "01"
  required_facts: [f_address, f_people, f_what_burns]
  address: "улица Ленина, 14"
  victims: 2

era_glonass:                    # только для type: era_glonass
  vin: "XW8ZZZ61ZJG123456"
  coords: { lat: 55.751244, lon: 37.618423 }
  passengers: 2
  impact_force: "сильный"

tree:                           # офлайн-режим: предгенерируется, в YAML не пишется
  pregenerated: true            # флаг читается, значения заполняет make pregen
```

## Правила по полям

### `persona`

База поведения звонящего. `arc` задаёт настроение по этапам алгоритма приёма вызова (см. [NORMATIVES.md](NORMATIVES.md)); если дуги нет — настроение постоянное. Директивы преподавателя ([product/CALL-SIM.md](../product/CALL-SIM.md#преподаватель-режиссёр)) перекрывают дугу на время своего действия.

### `facts` и условия раскрытия

Ключевая механика: **звонящий не выдаёт информацию сам**. Факт раскрывается, когда выполнено условие `reveal_on`:

- `{ question: q_xxx }` — оператор задал вопрос из чек-листа (матчинг по эмбеддингам, не по regex: «на каком этаже?», «этаж какой?», «а этаж» — одно условие)
- `{ approach: "<описание>" }` — LLM оценивает предшествующую реплику оператора по описанию подхода. Для фактов типа «боится говорить» — эмпатия, объяснение, зачем данные нужны

Повторный вопрос по уже раскрытому факту — не ошибка, но звонящий раздражается ([product/CALL-SIM.md](../product/CALL-SIM.md#память-звонящего)).

### `checklist`

Эталонный опрос. Один артефакт — четыре применения: подсказка курсанту по запросу ([product/MODES.md](../product/MODES.md)), эталон полноты опроса в оценке, материал разбора, база аналитики группы ([product/METHODOLOGY.md](../product/METHODOLOGY.md)). Чек-листы по классификаторам ДДС переиспользуются между сценариями через `extends` (см. ниже).

```yaml
extends: checklists/fire.yaml   # общий чек-лист по классификатору + локальные дополнения
checklist:
  - { id: q_who_else, question: "Кто ещё в квартире?", fact: f_people }
```

### `ground_truth`

Не пишется руками. Собирается проекцией `facts` кодом при загрузке: `incident_type` — из `type`, `dds` — из типа, `address`/`victims` — из помеченных фактов. Так генератор сценариев не может рассинхронизировать факты и эталон.

### `modes`

Где сценарий доступен. `exam` — только для вылизанных эталонных; `self` — открыт для самостоятельной отработки ([product/MODES.md](../product/MODES.md)).

### `era_glonass`

Для типа `era_glonass`: сессия стартует не с голоса, а с карточки автоданных ([product/CALL-SIM.md](../product/CALL-SIM.md#эра-глонасс)). `first_line` — реплика после соединения с водителем.

## Валидация

`loader.py` проверяет **все** YAML на старте приложения и падает с внятным сообщением при первом же нарушении: битый YAML, факт без `reveal_on` у `hidden: true`, `checklist` с `fact`, которого нет в `facts`, `era_glonass` без `type: era_glonass`. Сломанный сценарий, найденный посреди занятия, — сценарий, которого не должно случиться.
