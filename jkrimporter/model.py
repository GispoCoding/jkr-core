from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, List, NamedTuple, Optional, Union

from jkrimporter.utils.intervals import Interval

Kiinteistonumero = str
Rakennustunnus = str


@dataclass
class Osoite:
    katunimi: Optional[str] = None
    osoitenumero: Optional[str] = None
    huoneistotunnus: Optional[str] = None
    postinumero: Optional[str] = None
    postitoimipaikka: Optional[str] = None
    kunta: Optional[str] = None
    erikoisosoite: Optional[str] = None

    def osoite_rakennus(self):
        osoite = ""
        if self.katunimi:
            osoite += self.katunimi.title()
        if self.osoitenumero:
            osoite += f" {self.osoitenumero}"
        if self.postinumero:
            osoite += f", {self.postinumero}"
        if self.postitoimipaikka:
            osoite += f" {self.postitoimipaikka.title()}"

        return osoite

    def katuosoite(self):
        osoite = ""
        if self.katunimi:
            osoite += self.katunimi.title()
        if self.osoitenumero:
            osoite += f" {self.osoitenumero}"
        if self.huoneistotunnus:
            osoite += f" {self.huoneistotunnus}"

        return osoite

    def __str__(self):
        osoite = ""
        if self.katunimi:
            osoite += self.katunimi.title()
        if self.osoitenumero:
            osoite += f" {self.osoitenumero}"
        if self.huoneistotunnus:
            osoite += f" {self.huoneistotunnus}"
        if self.postinumero:
            osoite += f", {self.postinumero}"
        if self.postitoimipaikka:
            osoite += f" {self.postitoimipaikka.title()}"

        return osoite


@dataclass
class Yhteystieto:
    nimi: str
    osoite: Osoite
    ytunnus: Optional[str] = None
    henkilotunnus: Optional[str] = None


@dataclass
class Yritys:
    nimi: str
    osoite: Osoite
    ytunnus: str


class Tunnus(NamedTuple):
    jarjestelma: str
    tunnus: str


class Jatelaji(str, Enum):
    sekajate = "Sekajäte"
    bio = "Biojäte"
    lasi = "Lasi"
    paperi = "Paperi"
    kartonki = "Kartonki"
    muovi = "Muovi"
    metalli = "Metalli"
    liete = "Liete"
    harmaaliete = "Harmaaliete"
    mustaliete = "Mustaliete"
    pahvi = "Pahvi"
    energia = "Energia"
    muu = "Muu"


class KeraysvalineTyyppi(str, Enum):
    PINTA = "PINTA"
    SYVA = "SYVÄ"
    SAKO = "SAKO"
    UMPI = "UMPI"
    RULLAKKO = "RULLAKKO"
    SAILIO = "SÄILIÖ"
    PIENPUHDISTAMO = "PIENPUHDISTAMO"
    PIKAKONTTI = "PIKAKONTTI"
    NOSTOKONTTI = "NOSTOKONTTI"
    VAIHTOLAVA = "VAIHTOLAVA"
    JATESAKKI = "JÄTESÄKKI"
    PURISTINSAILIO = "PURISTINSÄILIÖ"
    PURISTIN = "PURISTIN"
    VAIHTOLAVASAILIO = "VAIHTOLAVASÄILIÖ"
    PAALI = "PAALI"
    MONILOKERO = "MONILOKERO"
    MUU = "Muu"


class Keskeytys(NamedTuple):
    alkupvm: date
    loppupvm: date
    selite: Optional[str]


@dataclass
class Keraysvaline:
    maara: int
    tilavuus: Optional[int] = None
    tyyppi: Optional[KeraysvalineTyyppi] = None


class Tyhjennysvali(NamedTuple):
    alkuvko: int
    loppuvko: int
    tyhjennysvali: float


@dataclass
class Tyhjennystapahtuma:
    jatelaji: Jatelaji
    alkupvm: Optional[date]
    loppupvm: Optional[date]
    tyhjennyskerrat: int
    tilavuus: Optional[int]
    massa: Optional[int] = None


class SopimusTyyppi(str, Enum):
    tyhjennyssopimus = "Tyhjennyssopimus"
    kimppasopimus = "Kimppasopimus"
    aluekerayssopimus = "Aluekeräyssopimus"
    putkikerayssopimus = "Putkikeräyssopimus"


@dataclass
class BaseSopimus:
    sopimustyyppi: SopimusTyyppi
    jatelaji: Optional[Jatelaji]
    alkupvm: Optional[date] = None
    loppupvm: Optional[date] = None
    keskeytykset: Optional[List[Keskeytys]] = field(default_factory=list)


@dataclass
class TyhjennysSopimus(BaseSopimus):
    keraysvalineet: Optional[List[Keraysvaline]] = field(default_factory=list)
    tyhjennysvalit: Optional[List[Tyhjennysvali]] = field(default_factory=list)


@dataclass
class KimppaSopimus(TyhjennysSopimus):
    isannan_asiakasnumero: Optional[Tunnus] = None


@dataclass
class Asiakas:
    asiakasnumero: Tunnus
    ulkoinen_asiakastieto: dict
    voimassa: Interval
    haltija: Yhteystieto
    yhteyshenkilo: Optional[Yhteystieto] = None
    kiinteistot: List[Kiinteistonumero] = field(default_factory=list)
    rakennukset: List[Rakennustunnus] = field(default_factory=list)
    sopimukset: List[Union[TyhjennysSopimus, KimppaSopimus]] = field(
        default_factory=list
    )
    tyhjennystapahtumat: List[Tyhjennystapahtuma] = field(default_factory=list)


ToimituspaikkaID = int


@dataclass
class ToimitusPaikka(Yritys):
    id: ToimituspaikkaID


@dataclass
class Toimitus:
    toimituspaikka_id: ToimituspaikkaID
    jatelaji: Jatelaji
    kayntikerrat: Optional[int]
    massa: int
    tilavuus: int


@dataclass
class JkrData:
    # The dates may be empty. This means that the total time span of the imported data
    # is unknown. Each asiakastieto may have different start and end dates, when
    # importing combined client data in a batch.
    alkupvm: Optional[date] = None
    loppupvm: Optional[date] = None
    asiakkaat: Dict[Tunnus, Asiakas] = field(default_factory=dict)
    toimituspaikat: Dict[ToimituspaikkaID, ToimitusPaikka] = field(default_factory=dict)
    toimitukset: List[Toimitus] = field(default_factory=list)
