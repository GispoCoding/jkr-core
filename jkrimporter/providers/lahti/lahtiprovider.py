import datetime
import logging
import warnings
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Union

from addrparser import AddressParser

from jkrimporter.model import Asiakas as JkrAsiakas
from jkrimporter.model import Jatelaji as JkrJatelaji
from jkrimporter.model import JkrData, Keraysvaline, KeraysvalineTyyppi
from jkrimporter.model import Keskeytys as JkrKeskeytys
from jkrimporter.model import (
    KimppaSopimus,
    Osoite,
    SopimusTyyppi,
    Tunnus,
    TyhjennysSopimus,
    Tyhjennystapahtuma,
)
from jkrimporter.model import Tyhjennysvali as JkrTyhjennysvali
from jkrimporter.model import Yhteystieto
from jkrimporter.providers.lahti.models import Asiakas, Jatelaji
from jkrimporter.utils.intervals import Interval
from jkrimporter.utils.osoite import osoite_from_parsed_address

from .siirtotiedosto import LahtiSiirtotiedosto

if TYPE_CHECKING:
    from .siirtotiedosto import Asiakas

logger = logging.getLogger(__name__)

address_parser = AddressParser("fi")


def overlap(a: TyhjennysSopimus, b: TyhjennysSopimus) -> bool:
    a1 = a.alkupvm or datetime.date.min
    a2 = a.loppupvm or datetime.date.max
    b1 = b.alkupvm or datetime.date.min
    b2 = b.loppupvm or datetime.date.max

    return a1 <= b2 and b1 <= a2


def create_haltija(row: "Asiakas"):
    try:
        postinumero, postitoimipaikka = row.Kiinteistonposti.split(" ", maxsplit=1)
    except ValueError:
        if row.Kiinteistonposti.isdigit():
            postinumero, postitoimipaikka = row.Kiinteistonposti, None
        else:
            postinumero, postitoimipaikka = None, row.Kiinteistonposti
    kohteen_osoite = Osoite(postinumero=postinumero, postitoimipaikka=postitoimipaikka)
    if row.Kiinteistonkatuosoite:
        try:
            parsed_address = address_parser.parse(row.Kiinteistonkatuosoite)
        except ValueError:
            kohteen_osoite.erikoisosoite = row.Kiinteistonkatuosoite
        else:
            o = osoite_from_parsed_address(parsed_address)
            kohteen_osoite.katunimi = o.katunimi
            kohteen_osoite.osoitenumero = o.osoitenumero
            kohteen_osoite.huoneistotunnus = o.huoneistotunnus
        kohteen_osoite.kunta = row.Kuntatun

    haltija = Yhteystieto(
        nimi=row.Haltijannimi.title(),
        osoite=kohteen_osoite,
    )
    print("got haltija")
    print(haltija)
    return haltija


def create_yhteyshenkilo(row: "Asiakas"):
    if row.palveluKimppakohdeId:
        try:
            postinumero, postitoimipaikka = row.Kimpanposti.split(" ", maxsplit=1)
        except ValueError:
            if row.Kimpanposti.isdigit():
                postinumero, postitoimipaikka = row.Kimpanposti, None
            else:
                postinumero, postitoimipaikka = None, row.Kimpanposti
    else:
        try:
            postinumero, postitoimipaikka = row.Haltijanposti.split(" ", maxsplit=1)
        except ValueError:
            if row.Haltijanposti.isdigit():
                postinumero, postitoimipaikka = row.Haltijanposti, None
            else:
                postinumero, postitoimipaikka = None, row.Haltijanposti
    yhteyshenkilon_osoite = Osoite(
        postinumero=postinumero, postitoimipaikka=postitoimipaikka
    )
    # of course, Haltijankatuosoite is *not* haltijan katuosoite in Lahti data.
    # It is the katuosoite of haltija *or* their yhteyshenkilö, whenever yhteyshenkilö
    # differs from haltija. Therefore, it should always be used for yhteyshenkilö.
    # - In our terminology, *haltija* is the one living in the building.
    # - In Lahti terminology, *haltija* is the owner, i.e. yhteyshenkilö.
    # Even in case the haltija has no external yhteyshenkilö (private owners), their
    # address may differ from that of the kohde address, and they should always be
    # saved.
    if row.Kimpankatuosoite:
        try:
            parsed_address = address_parser.parse(row.Kimpankatuosoite)
        except ValueError:
            yhteyshenkilon_osoite.erikoisosoite = row.Kimpankatuosoite
        else:
            o = osoite_from_parsed_address(parsed_address)
            yhteyshenkilon_osoite.katunimi = o.katunimi
            yhteyshenkilon_osoite.osoitenumero = o.osoitenumero
            yhteyshenkilon_osoite.huoneistotunnus = o.huoneistotunnus
    elif row.Haltijankatuosoite:
        try:
            parsed_address = address_parser.parse(row.Haltijankatuosoite)
        except ValueError:
            yhteyshenkilon_osoite.erikoisosoite = row.Haltijankatuosoite
        else:
            o = osoite_from_parsed_address(parsed_address)
            yhteyshenkilon_osoite.katunimi = o.katunimi
            yhteyshenkilon_osoite.osoitenumero = o.osoitenumero
            yhteyshenkilon_osoite.huoneistotunnus = o.huoneistotunnus

    if row.kimpanNimi:
        nimi = row.kimpanNimi.title()
    else:
        nimi = (
            row.Haltijanyhteyshlo.title()
            if row.Haltijanyhteyshlo
            else row.Haltijannimi.title()
        )
    yhteyshenkilo = Yhteystieto(
        nimi=nimi,
        osoite=yhteyshenkilon_osoite,
    )
    print("got yhteyshenkilö")
    print(yhteyshenkilo)
    return yhteyshenkilo


jatelaji_map = {
    Jatelaji.seka: JkrJatelaji.sekajate,
    Jatelaji.energia: JkrJatelaji.energia,
    Jatelaji.bio: JkrJatelaji.bio,
    Jatelaji.kartonki: JkrJatelaji.kartonki,
    Jatelaji.pahvi: JkrJatelaji.pahvi,
    Jatelaji.metalli: JkrJatelaji.metalli,
    Jatelaji.lasi: JkrJatelaji.lasi,
    Jatelaji.paperi: JkrJatelaji.paperi,
    Jatelaji.muovi: JkrJatelaji.muovi,
}


class LahtiTranslator:
    """
    In Lahti, tiedontuottaja PJH is different from urakoitsija. The same tiedontuottaja
    file contains multiple urakoitsija, which must be saved with their original ids. In
    most cases, then, we don't use tiedontuottajatunnus, we will use the tunnus for each
    urakoitsija separately to identify clients.
    """

    def __init__(
        self,
        siirtotiedosto: LahtiSiirtotiedosto,
        tiedontuottajatunnus: str,
    ):
        self._source = siirtotiedosto
        self._tiedontuottaja_tunnus = tiedontuottajatunnus
        # self._tunnus_by_urakoitsija_and_asiakasnro = defaultdict(dict)

    def as_jkr_data(self, alkupvm: Union[None, date], loppupvm: Union[None, date]):
        data = JkrData()

        data = self._add_meta(data, alkupvm, loppupvm)
        # If alkupvm and loppupvm are not known, Lahti data may contain clients
        # with lots of different intervals. Just import everything in the data.
        data = self._append_asiakkaat(data, alkupvm, loppupvm)

        return data

    def tunnus_from_urakoitsija_and_asiakasnumero(
        self, urakoitsija: str, asiakasnro: str
    ) -> Tunnus:
        return Tunnus(urakoitsija, asiakasnro)

    def _add_meta(self, data: JkrData, alkupvm, loppupvm):
        data.alkupvm = alkupvm
        data.loppupvm = loppupvm

        return data

    def _create_asiakas(self, tunnus: Tunnus, row: Asiakas) -> JkrAsiakas:
        # self._tunnus_by_urakoitsija_and_asiakasnro[row.UrakoitsijaId][
        #     row.UrakoitsijankohdeId
        # ] = tunnus
        haltija = create_haltija(row)
        yhteyshenkilo = create_yhteyshenkilo(row)
        asiakas = JkrAsiakas(
            asiakasnumero=tunnus,
            voimassa=Interval(row.Pvmalk, row.Pvmasti),
            ulkoinen_asiakastieto=row,
            # obviously, prt is called Kiinteistotunnus in Lahti data
            rakennukset=[row.Kiinteistotunnus] if row.Kiinteistotunnus else [],
            haltija=haltija,
            yhteyshenkilo=yhteyshenkilo,
        )
        return asiakas

    def _append_asiakkaat(
        self, data: JkrData, alkupvm: Union[None, date], loppupvm: Union[None, date]
    ):
        for row in self._source.asiakastiedot:
            print("-------")
            print("got asiakastiedot")
            print(row)
            if alkupvm:
                print(alkupvm)
                print(row.Pvmasti)
                if row.Pvmasti < alkupvm:
                    print("skipping, too early")
                    continue
            if loppupvm:
                print(loppupvm)
                print(row.Pvmalk)
                if row.Pvmalk > loppupvm:
                    print("skipping, too late")
                    continue
            tunnus = self.tunnus_from_urakoitsija_and_asiakasnumero(
                row.UrakoitsijaId, row.UrakoitsijankohdeId
            )
            # In Lahti data, each row only contains information on one kuljetus and
            # sopimus. If the asiakas already exists, do not add them again, just
            # append to their kuljetukset and sopimukset. Luckily, UrakoitsijankohdeId
            # means any different buildings will always be imported as separate
            # asiakas, even if their name etc. is the same. This way, each Asiakas
            # will always only have a single kohde and its sopimukset.
            if tunnus not in data.asiakkaat.keys():
                data.asiakkaat[tunnus] = self._create_asiakas(tunnus, row)
                print(f"Added new asiakas {tunnus}")
            else:
                print(f"Asiakas {tunnus} found already")

            # Lahti saves aluekeräys in the same field as jätelajit
            if row.tyyppiIdEWC == Jatelaji.aluekerays:
                sopimustyyppi = SopimusTyyppi.aluekerayssopimus
                jatelaji = JkrJatelaji.muu
            else:
                sopimustyyppi = SopimusTyyppi.tyhjennyssopimus
                jatelaji = jatelaji_map[row.tyyppiIdEWC]

            # Lahti saves kimppasopimukset along with regular sopimukset
            if row.palveluKimppakohdeId:
                isannan_asiakasnumero = self.tunnus_from_urakoitsija_and_asiakasnumero(
                    row.UrakoitsijaId, row.palveluKimppakohdeId
                )
                if isannan_asiakasnumero not in data.asiakkaat.keys():
                    data.asiakkaat[isannan_asiakasnumero] = self._create_asiakas(
                        isannan_asiakasnumero, row
                    )
                    print(f"Added new kimppaisäntä {isannan_asiakasnumero}")
                else:
                    print(f"Kimppaisäntä {isannan_asiakasnumero} found already")
                sopimus = KimppaSopimus(
                    sopimustyyppi=SopimusTyyppi.kimppasopimus,
                    jatelaji=jatelaji,
                    alkupvm=row.Pvmalk,
                    loppupvm=row.Pvmasti,
                    isannan_asiakasnumero=isannan_asiakasnumero,
                )
            else:
                sopimus = TyhjennysSopimus(
                    sopimustyyppi=sopimustyyppi,
                    jatelaji=jatelaji,
                    alkupvm=row.Pvmalk,
                    loppupvm=row.Pvmasti
                )
            if row.tyhjennysvali:
                # tyhjennysväli is missing from some data
                sopimus.tyhjennysvalit.append(
                    JkrTyhjennysvali(
                        alkuvko=row.Voimassaoloviikotalkaen,
                        loppuvko=row.Voimassaoloviikotasti,
                        tyhjennysvali=row.tyhjennysvali,
                    )
                )
            data.asiakkaat[tunnus].sopimukset.append(sopimus)
            # print(data.asiakkaat[tunnus].sopimukset)

            keraysvaline = Keraysvaline(
                maara=row.astiamaara,
                tilavuus=row.koko * 1000 if row.koko else None,
                tyyppi=KeraysvalineTyyppi.SAILIO,
            )
            sopimus.keraysvalineet.append(keraysvaline)
            # print(sopimus.keraysvalineet)

            massa = row.paino * 1000 if row.paino else None
            data.asiakkaat[tunnus].tyhjennystapahtumat.append(
                Tyhjennystapahtuma(
                    alkupvm=row.Pvmalk,
                    loppupvm=row.Pvmasti,
                    jatelaji=jatelaji,
                    tyhjennyskerrat=row.kaynnit,
                    tilavuus=row.koko * 1000 if row.koko else None,
                    massa=massa,
                )
            )
            # print(data.asiakkaat[tunnus].tyhjennystapahtumat)
            print("------")

        return data
