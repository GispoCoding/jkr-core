import datetime
import re
from datetime import date
from enum import Enum
from typing import Dict, Optional, Union

from pydantic import BaseModel, Field, ValidationError, root_validator, validator

# from jkrimporter.utils.validators import (
#     date_pre_validator,
#     date_range_root_validator,
#     int_validator,
#     split_by_comma,
#     trim_ytunnus,
# )


class Jatelaji(str, Enum):
    aluekerays = "Aluekeräys"
    seka = "Sekajäte"
    energia = "Energia"
    bio = "Bio"
    kartonki = "Kartonki"
    pahvi = "Pahvi"
    metalli = "Metalli"
    lasi = "Lasi"
    paperi = "Paperi"
    muovi = "Muovi"
    liete = "Liete"
    musta_liete = "Musta liete"
    harmaa_liete = "Harmaa liete"


class Asiakas(BaseModel):
    UrakoitsijaId: str
    UrakoitsijankohdeId: str
    Kiinteistotunnus: Optional[str] = None
    Kiinteistonkatuosoite: Optional[str] = None
    Kiinteistonposti: str
    # kimppa: Optional[bool] = False
    # kimppaid: Optional[str] = None
    Haltijannimi: str
    Haltijanyhteyshlo: Optional[str] = None
    Haltijankatuosoite: Optional[str] = None
    Haltijanposti: str
    Haltijanmaakoodi: Optional[str] = None
    Haltijanulkomaanpaikkakunta: Optional[str] = None
    Pvmalk: datetime.date
    Pvmasti: datetime.date
    tyyppiIdEWC: Jatelaji
    kaynnit: int = Field(alias="COUNT(kaynnit)")
    astiamaara: float = Field(alias="SUM(astiamaara)")
    koko: Optional[float]
    paino: Optional[float] = Field(alias="SUM(paino)")
    tyhjennysvali: Optional[int] = None
    Voimassaoloviikotalkaen: Optional[int] = None
    Voimassaoloviikotasti: Optional[int] = None
    Kuntatun: Optional[int] = None
    palveluKimppakohdeId: Optional[str] = None
    kimpanNimi: Optional[str] = None
    Kimpankatuosoite: Optional[str] = None
    Kimpanposti: Optional[str] = None
    # Keskeytysalkaen: Optional[datetime.date] = None,
    # Keskeytysasti: Optional[datetime.date] = None,
    # Kompostoi: Optional[str] = None,
    # Kaatopaikka: Optional[str] = None

    # @validator("UrakoitsijankohdeId", pre=True)
    # TODO: Cannot construct id from name. Name is validated later.
    # def construct_missing_id(value: Union[str, None], values: Dict):
    #     print(value)
    #     print(values)
    #     if not value:
    #         try:
    #             value = values["Haltijannimi"].lower().replace(" ", "_")
    #         except AttributeError:
    #             raise ValidationError("Asiakas must have name or id.")
    #     return str(value)

    @validator("Haltijannimi", pre=True)
    def construct_missing_name(value: Union[str, None], values: Dict):
        print(values)
        if not value:
            try:
                value = str(values["UrakoitsijankohdeId"])
            except KeyError:
                raise ValidationError("Asiakas must have name or id.")
        return str(value)

    @validator(
        "Kiinteistonkatuosoite", "Haltijankatuosoite", "Kimpankatuosoite", pre=True
    )
    def fix_katuosoite(value: Union[str, None]):
        # address may be missing
        if not value:
            return None
        # osoite must be in title case for parsing
        value = value.title()
        # asunto abbreviation should be lowercase
        asunto_without_dot_regex = r"([0-9]+[a-z]?\s)(As)(\s[0-9]+$)"
        asunto_with_dot_regex = r"\1as.\3"
        return re.sub(asunto_without_dot_regex, asunto_with_dot_regex, value)

    @validator("Haltijanposti", "Kiinteistonposti", "Kimpanposti", pre=True)
    def fix_posti(value: Union[str, int]):
        # looks like some postiosoite only have postinumero
        value = str(value)
        while "  " in value:
            value = value.replace("  ", " ")
        return value

    # @validator("kimppa", pre=True)
    # def parse_kimppa(value: Union[bool, str]):
    #     # no idea why pydantic has trouble with coercing empty strings
    #     # to boolean
    #     return bool(value)

    @validator("koko", "paino", pre=True)
    def parse_float(value: Union[float, str]):
        # If float wasn't parsed, let's parse them
        if type(value) is str:
            # we might have . or , as the separator
            return float(value.replace(",", "."))
        return value

    @validator("tyyppiIdEWC", pre=True)
    def parse_jatelaji(value: str):
        if value == "Sekaj":
            value = "Sekajäte"
        return value.title()

    @validator("Pvmalk", "Pvmasti", pre=True)
    def parse_date(value: Union[date, str]):
        # If date wasn't parsed, let's parse them.
        # Date may be in a variety of formats. Pydantic cannot parse all
        # of them by default
        if type(value) is str and "." in value:
            return datetime.datetime.strptime(value, "%d.%m.%Y").date()
        return value

    @validator(
        "tyhjennysvali", "Voimassaoloviikotalkaen", "Voimassaoloviikotasti", pre=True
    )
    def fix_na(value: str):
        # Many fields may have strings such as #N/A. Parse them to None.
        if value == "#N/A":
            return None
        return value

    @validator("Kuntatun", pre=True)
    def parse_kuntatunnus(value: str):
        # kuntatunnus may be missing. No idea why pydantic has trouble
        # with parsing it as optional None.
        if value:
            return int(value)
        return None
