import logging
import warnings
from datetime import date
from typing import Union

from addrparser import AddressParser

from jkrimporter.model import Asiakas as JkrAsiakas
from jkrimporter.model import Jatelaji as JkrJatelaji
from jkrimporter.model import JkrData, Keraysvaline
from jkrimporter.model import KeraysvalineTyyppi as JkrKeraysvalineTyyppi
from jkrimporter.model import (
    Osoite,
    SopimusTyyppi,
    Tunnus,
    TyhjennysSopimus,
    Tyhjennystapahtuma,
    Yhteystieto,
)
from jkrimporter.providers.nokia.models import Asiakas, Jatelaji, KaivoTyyppi
from jkrimporter.providers.nokia.siirtotiedosto import NokiaSiirtotiedosto
from jkrimporter.utils.intervals import Interval
from jkrimporter.utils.osoite import osoite_from_parsed_address

logger = logging.getLogger(__name__)

address_parser = AddressParser("fi")


def create_haltija(row: "Asiakas"):
    kohteen_osoite = Osoite(kunta=row.kohde_kunta)
    if row.kohde_katuosoite:
        try:
            parsed_address = address_parser.parse(row.kohde_katuosoite)
        except ValueError:
            kohteen_osoite.erikoisosoite = row.kohde_katuosoite
        else:
            o = osoite_from_parsed_address(parsed_address)
            kohteen_osoite.katunimi = o.katunimi
            kohteen_osoite.osoitenumero = o.osoitenumero
            kohteen_osoite.huoneistotunnus = o.huoneistotunnus

    haltija = Yhteystieto(
        nimi=row.haltija_nimi if row.haltija_nimi else "",
        osoite=kohteen_osoite,
    )
    return haltija


def create_yhteyshenkilo(row: "Asiakas"):
    yhteyshenkilon_osoite = Osoite(
        postinumero=row.yhteyshenkilo_postinumero,
        postitoimipaikka=row.yhteyshenkilo_postitoimipaikka,
    )
    if row.yhteyshenkilo_katuosoite:
        try:
            parsed_address = address_parser.parse(row.kohde_katuosoite)
        except ValueError:
            yhteyshenkilon_osoite.erikoisosoite = row.yhteyshenkilo_katuosoite
        else:
            o = osoite_from_parsed_address(parsed_address)
            yhteyshenkilon_osoite.katunimi = o.katunimi
            yhteyshenkilon_osoite.osoitenumero = o.osoitenumero
            yhteyshenkilon_osoite.huoneistotunnus = o.huoneistotunnus

    yhteyshenkilo = Yhteystieto(
        nimi=row.yhteyshenkilo_nimi if row.yhteyshenkilo_nimi else "",
        osoite=yhteyshenkilon_osoite,
    )
    return yhteyshenkilo


jatelaji_map = {
    Jatelaji.harmaaliete: JkrJatelaji.harmaaliete,
    Jatelaji.mustaliete: JkrJatelaji.mustaliete,
}
kaivotyyppi_map = {
    KaivoTyyppi.puhdistamo: JkrKeraysvalineTyyppi.PIENPUHDISTAMO,
    KaivoTyyppi.sako: JkrKeraysvalineTyyppi.SAKO,
    KaivoTyyppi.umpi: JkrKeraysvalineTyyppi.UMPI,
}


class NokiaTranslator:
    def __init__(self, siirtotiedosto: NokiaSiirtotiedosto, tiedontuottajatunnus: str):
        self._source = siirtotiedosto
        self._tiedontuottaja_tunnus = tiedontuottajatunnus
        self._asiakastunnus_to_tunnus = {}

    def as_jkr_data(self, alkupvm: Union[None, date], loppupvm: Union[None, date]):
        data = JkrData()

        data = self._add_meta(data, alkupvm, loppupvm)
        data = self._append_asiakkaat(data)
        data = self._append_sopimukset_keraysvalineet_kuljetukset(
            data, alkupvm, loppupvm
        )

        return data

    def tunnus_from_asiakasnumero(self, asiakasnro: str) -> Tunnus:
        return Tunnus(self._tiedontuottaja_tunnus, asiakasnro)

    def _add_meta(self, data: JkrData, alkupvm, loppupvm):
        data.alkupvm = alkupvm
        data.loppupvm = loppupvm

        return data

    def _append_asiakkaat(self, data: JkrData):
        for row in self._source.asiakastiedot:
            tunnus = self.tunnus_from_asiakasnumero(row.asiakasnumero)
            self._asiakastunnus_to_tunnus[row.asiakas_tunnus] = tunnus
            haltija = create_haltija(row)
            yhteyshenkilo = create_yhteyshenkilo(row)
            asiakas = JkrAsiakas(
                asiakasnumero=tunnus,
                voimassa=Interval(None, None),
                ulkoinen_asiakastieto=row,
                kiinteistot=row.kiinteistotunnukset,
                haltija=haltija,
                yhteyshenkilo=yhteyshenkilo,
            )

            data.asiakkaat[tunnus] = asiakas
        return data

    def _append_sopimukset_keraysvalineet_kuljetukset(
        self, data: JkrData, alkupvm: date, loppupvm: date
    ):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            for row in self._source.kuljetustiedot:
                tunnus = self._asiakastunnus_to_tunnus.get(row.asiakas_tunnus, None)
                if not tunnus:
                    logger.warning(
                        "Ohitetaan kuljetustapahtuma. Ei asiakasta: "
                        f"'{row.asiakas_tunnus}'"
                    )
                    continue

                sopimus = next(
                    (
                        s
                        for s in data.asiakkaat[tunnus].sopimukset
                        if s.jatelaji == jatelaji_map[row.jatelaji]
                    ),
                    None,
                )
                if not sopimus:
                    sopimus = TyhjennysSopimus(
                        sopimustyyppi=SopimusTyyppi.tyhjennyssopimus,
                        jatelaji=jatelaji_map[row.jatelaji],
                        alkupvm=alkupvm,
                        loppupvm=loppupvm,
                    )
                    data.asiakkaat[tunnus].sopimukset.append(sopimus)

                keraysvaline = next(
                    (
                        k
                        for k in sopimus.keraysvalineet
                        if k.tyyppi == kaivotyyppi_map[row.kaivon_tyyppi]
                    ),
                    None,
                )
                if not keraysvaline:
                    keraysvaline = Keraysvaline(
                        maara=1, tyyppi=kaivotyyppi_map[row.kaivon_tyyppi]
                    )
                    sopimus.keraysvalineet.append(keraysvaline)

                data.asiakkaat[tunnus].tyhjennystapahtumat.append(
                    Tyhjennystapahtuma(
                        loppupvm=row.pvm,
                        jatelaji=jatelaji_map[row.jatelaji],
                        tyhjennyskerrat=1,
                        tilavuus=row.tilavuus * 1000,
                    )
                )

            return data
