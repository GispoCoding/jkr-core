import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, validator

from jkrimporter.utils.kitu import short_kitu_2_long
from jkrimporter.utils.validators import date_pre_validator, split_by_comma


class Jatelaji(str, Enum):
    liete = "LIETE"
    harmaaliete = "LIETE-HARMAA"
    mustaliete = "LIETE-MUSTA"


class KaivoTyyppi(str, Enum):
    umpi = "Umpikaivo"
    sako = "Sakokaivo"
    puhdistamo = "Pienpuhdistamo"


class Asiakas(BaseModel):
    asiakasnumero: str = Field(alias="Asiakasnumero")
    asiakas_tunnus: str = Field(alias="ÄLÄ KOSKE - Sisäinen asiakkuustunnus")
    haltija_nimi: str = Field(alias="Asiakkaan nimi")
    kohde_katuosoite: str = Field(alias="Kohteen katuosoite")
    kohde_kunta: str = Field(alias="Kohteen sijaintikunta")
    kiinteistotunnukset: List[str] = Field(alias="Kohteen kiinteistötunnus")
    yhteyshenkilo_nimi: Optional[str] = Field(alias="Yhteyshenkilön nimi")
    yhteyshenkilo_katuosoite: Optional[str] = Field(alias="Yhteyshenkilön postiosoite")
    yhteyshenkilo_postinumero: Optional[str] = Field(alias="Yhteyshenkilön postinumero")
    yhteyshenkilo_postitoimipaikka: Optional[str] = Field(
        alias="Yhteyshenkilön postitoimipaikka"
    )

    def __init__(self, *args, **data):
        data["Kohteen kiinteistötunnus"] = [
            short_kitu_2_long(kitu)
            for kitu in split_by_comma(data["Kohteen kiinteistötunnus"])
        ]

        super().__init__(*args, **data)

    @validator("yhteyshenkilo_nimi", pre=True)
    def trim_str(value):
        if isinstance(value, str):
            value = value.strip()
        return value


class Tyhjennystapahtuma(BaseModel):
    asiakas_tunnus: str = Field(alias="Asiakas")
    pvm: Optional[datetime.date] = Field(alias="Päivämäärä")
    kaivon_tyyppi: KaivoTyyppi = Field(alias="Kaivon tyyppi")
    jatelaji: Jatelaji = Field(alias="Jätelaji")
    tilavuus: Optional[int] = Field(alias="Tilavuus m3")
    toimituspaikka: Optional[str] = Field(alias="Toimituspaikka")

    # Validators
    _date_validator = date_pre_validator("pvm")

    @validator("tilavuus", pre=True)
    def replace_comma(cls, v: str):
        if isinstance(v, str):
            v = v.replace(",", ".").replace(" ", "")
        return int(float(v))
