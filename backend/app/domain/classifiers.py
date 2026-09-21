"""Классификаторы ДДС, типы происшествий, уровни сложности.

Норматив: docs/spec/NORMATIVES.md#классификаторы-ддс
"""

from enum import StrEnum


class DDSCode(StrEnum):
    """Дежурно-диспетчерская служба, в которую уходит карточка."""

    FIRE = "01"
    POLICE = "02"
    MEDICAL = "03"
    OTHER = "04"
    GKH = "gkh"


class IncidentType(StrEnum):
    """Тип происшествия. Совпадает с полем `type` сценария."""

    FIRE = "fire"
    MEDICAL = "medical"
    POLICE = "police"
    OTHER = "other"
    GKH = "gkh"
    ERA_GLONASS = "era_glonass"


class Outcome(StrEnum):
    """Чем заканчивается вызов.

    Весь продукт по умолчанию считает, что звонок → карточка → выезд. В билетах
    заказчика это не всегда так: «поругался с продавцом „Мегафон“» — справка,
    а вызов из Волгоградской области передаётся в систему-112 другого субъекта
    (docs/spec/TICKETS.md). Курсант, заведший карточку на такой вызов, ошибся,
    и система обязана это увидеть.
    """

    CARD = "card"                        # заполнить КИО и передать в ДДС
    CONSULTATION = "consultation"        # справка, карточка не заводится
    TRANSFER_REGION = "transfer_region"  # передать в систему-112 другого субъекта


OUTCOME_LABELS: dict[Outcome, str] = {
    Outcome.CARD: "Карточка и передача в ДДС",
    Outcome.CONSULTATION: "Справочно-консультационная помощь",
    Outcome.TRANSFER_REGION: "Передача в систему-112 другого региона",
}


class Level(StrEnum):
    """Уровень сложности сценария."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


DDS_LABELS: dict[DDSCode, str] = {
    DDSCode.FIRE: "01 — МЧС",
    DDSCode.POLICE: "02 — полиция",
    DDSCode.MEDICAL: "03 — скорая помощь",
    DDSCode.OTHER: "04 — прочие службы",
    DDSCode.GKH: "ЖКХ",
}

INCIDENT_LABELS: dict[IncidentType, str] = {
    IncidentType.FIRE: "Пожар",
    IncidentType.MEDICAL: "Медицинский вызов",
    IncidentType.POLICE: "Правонарушение",
    IncidentType.OTHER: "Прочее",
    IncidentType.GKH: "Авария ЖКХ",
    IncidentType.ERA_GLONASS: "ДТП, автовызов ЭРА-ГЛОНАСС",
}

#: Штатное соответствие типа происшествия и службы. Используется как значение
#: по умолчанию при выводе ground_truth сценария; сценарий вправе переопределить.
DDS_BY_INCIDENT: dict[IncidentType, DDSCode] = {
    IncidentType.FIRE: DDSCode.FIRE,
    IncidentType.MEDICAL: DDSCode.MEDICAL,
    IncidentType.POLICE: DDSCode.POLICE,
    IncidentType.OTHER: DDSCode.OTHER,
    IncidentType.GKH: DDSCode.GKH,
    IncidentType.ERA_GLONASS: DDSCode.MEDICAL,
}
