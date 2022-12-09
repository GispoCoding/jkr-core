import json
import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from jkrimporter.model import Yhteystieto

oy_strings = ("oy", "ab")

oy_regexps = [
    re.compile(rf"( |^){re.escape(string)}( |\.|$)", flags=re.IGNORECASE)
    for string in oy_strings
]

asoy_strings = (
    "asoy",
    "as oy",
    "asunto-osakeyhtiö",
    "asunto-oy",
    "asunto oy",
    "bostadsaktiebolag",
    "bostads ab",
    "bost. ab",
)

asoy_regexps = [
    re.compile(rf"(^| ){re.escape(string)}( |\.|$)", flags=re.IGNORECASE)
    for string in asoy_strings
]

yhteiso_strings = (
    "yhtymä",
    "yhdistys",
    "yhtiö",
    "tmi",
    "kaupunki",
    "kunta",
    "seurakunta",
    "sr",
    "ry",
    "r.y."
)

yhteiso_regexps = [
    # some strings have an extra space in the end
    re.compile(rf"{re.escape(string)} ?$", flags=re.IGNORECASE)
    for string in yhteiso_strings
]


def clean_asoy_name(name: str):
    for pattern in asoy_regexps:
        name = pattern.sub("", name)

    return name


def is_asoy(name: str) -> bool:
    return any(pattern.search(name) for pattern in asoy_regexps)


def is_company(name: str) -> bool:
    return any(pattern.search(name) for pattern in oy_regexps)


def is_yhteiso(name: str) -> bool:
    return any(pattern.search(name) for pattern in yhteiso_regexps)


def form_display_name(haltija: "Yhteystieto") -> str:
    if not haltija.henkilotunnus and (
        haltija.ytunnus
        or is_company(haltija.nimi)
        or is_asoy(haltija.nimi)
        or is_yhteiso(haltija.nimi)
    ):
        display_name = haltija.nimi.title()
    else:
        display_name = haltija.nimi.split()[0].title()

    return display_name


class JSONEncoderWithDateSupport(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, BaseModel):
            return value.json()

        if isinstance(value, (datetime, date)):
            return value.isoformat()

        try:
            v = super().default(value)
        except Exception:
            logger.error(f"Json encoding failed with value: {v}")

        return v
