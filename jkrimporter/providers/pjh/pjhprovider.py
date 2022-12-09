import datetime
import logging
from datetime import date
from typing import TYPE_CHECKING, Union

from addrparser import AddressParser

from jkrimporter.model import Asiakas as JkrAsiakas
from jkrimporter.model import Jatelaji as JkrJatelaji
from jkrimporter.model import JkrData
from jkrimporter.model import Keraysvaline as JkrKeraysvaline
from jkrimporter.model import KeraysvalineTyyppi as JkrKeraysvalineTyyppi
from jkrimporter.model import Keskeytys as JkrKeskeytys
from jkrimporter.model import (
    KimppaSopimus,
    Osoite,
    SopimusTyyppi,
    Tunnus,
    TyhjennysSopimus,
)
from jkrimporter.model import Tyhjennystapahtuma as JkrTyhjennystapahtuma
from jkrimporter.model import Tyhjennysvali as JkrTyhjennysvali
from jkrimporter.model import Yhteystieto
from jkrimporter.utils.intervals import Interval
from jkrimporter.utils.osoite import osoite_from_parsed_address

from .siirtotiedosto import PjhSiirtotiedosto

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
    kohteen_osoite = Osoite(
        postinumero=row.kohde_postinumero,
        postitoimipaikka=row.kohde_postitoimipaikka,
    )
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
        nimi=row.haltija_nimi,
        ytunnus=row.haltija_ytunnus,
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
        nimi=row.yhteyshenkilo_nimi,
        osoite=yhteyshenkilon_osoite,
    )
    return yhteyshenkilo


class PjhTranslator:
    def __init__(self, pjhsiirtotiedosto: PjhSiirtotiedosto, tiedontuottajatunnus: str):
        self._source = pjhsiirtotiedosto

    def as_jkr_data(self, alkupvm: Union[None, date], loppupvm: Union[None, date]):
        data = JkrData()
        data = self._add_meta(data)
        data = self._append_asiakkaat(data)
        data = self._append_sopimukset(data)
        data = self._append_kimpat(data)
        data = self._append_keraysvalineet(data)

        data = self._append_keskeytykset(data)
        data = self._append_tyhjennykset(data)

        return data

    def _add_meta(self, jkr_data: JkrData):
        for row in self._source.meta:
            jkr_data.alkupvm = row.alkupvm
            jkr_data.loppupvm = row.loppupvm

        return jkr_data

    def tunnus_from_asiakasnumero(self, asiakasnro: str) -> Tunnus:
        return Tunnus(self._tiedontuottaja_tunnus, asiakasnro)

    def _append_asiakkaat(self, jkr_data: JkrData):
        for row in self._source.asiakastiedot:
            tunnus = self.tunnus_from_asiakasnumero(row.asiakasnumero)

            haltija = create_haltija(row)
            yhteyshenkilo = create_yhteyshenkilo(row)

            asiakas = JkrAsiakas(
                asiakasnumero=tunnus,
                ulkoinen_asiakastieto=row,
                voimassa=Interval(row.alkupvm, row.loppupvm),
                kiinteistot=row.kiinteistotunnukset,
                rakennukset=row.rakennustunnukset,
                haltija=haltija,
                yhteyshenkilo=yhteyshenkilo,
            )
            if tunnus in jkr_data.asiakkaat:
                old = jkr_data.asiakkaat[tunnus]
                if old.haltija.ytunnus and not asiakas.haltija.ytunnus:
                    asiakas = old

            jkr_data.asiakkaat[tunnus] = asiakas

        return jkr_data

    def _append_sopimukset(self, jkr_data: JkrData):
        for tyhjennysvali_row in self._source.tyhjennysvalit:
            tunnus = self.tunnus_from_asiakasnumero(tyhjennysvali_row.asiakasnumero)
            if tunnus not in jkr_data.asiakkaat:
                logger.warning(
                    "Ohitetaan sopimus. Ei asiakasta: "
                    f"'{tyhjennysvali_row.asiakasnumero}'"
                )
                continue

            if tyhjennysvali_row.jatelaji:
                try:
                    jatelaji = JkrJatelaji(tyhjennysvali_row.jatelaji)
                except ValueError:
                    jatelaji = JkrJatelaji.muu
            else:
                jatelaji = None

            sopimus = TyhjennysSopimus(
                sopimustyyppi=SopimusTyyppi.tyhjennyssopimus,
                jatelaji=jatelaji,
                alkupvm=tyhjennysvali_row.alkupvm,
                loppupvm=tyhjennysvali_row.loppupvm,
            )

            other_sopimukset = [
                sopimus
                for sopimus in jkr_data.asiakkaat[tunnus].sopimukset
                if sopimus.sopimustyyppi == SopimusTyyppi.tyhjennyssopimus
                and sopimus.jatelaji == jatelaji
            ]
            for other in other_sopimukset:
                if overlap(sopimus, other):
                    other.alkupvm = (
                        min(sopimus.alkupvm, other.alkupvm)
                        if sopimus.alkupvm and other.alkupvm
                        else None
                    )
                    other.loppupvm = (
                        max(sopimus.loppupvm, other.loppupvm)
                        if sopimus.loppupvm and other.loppupvm
                        else None
                    )
                    sopimus = other
                    break
            else:
                jkr_data.asiakkaat[tunnus].sopimukset.append(sopimus)

            sopimus.tyhjennysvalit.append(
                JkrTyhjennysvali(
                    alkuvko=tyhjennysvali_row.alkuvko,
                    loppuvko=tyhjennysvali_row.loppuvko,
                    tyhjennysvali=tyhjennysvali_row.tyhjennysvali,
                )
            )

        return jkr_data

    def _append_tyhjennykset(self, jkr_data: JkrData):
        for row in self._source.tyhennystapahtumat:
            tunnus = self.tunnus_from_asiakasnumero(row.asiakasnumero)
            try:
                jatelaji = JkrJatelaji(row.jatelaji)
            except ValueError:
                jatelaji = JkrJatelaji.muu

            try:
                asiakas = jkr_data.asiakkaat[tunnus]
            except KeyError:
                logger.warning(
                    f"Ohitetaan tyhjennystapahtuma. Ei asiakasta {row.asiakasnumero}"
                )
                continue

            tyhjennystapahtuma = JkrTyhjennystapahtuma(
                jatelaji=jatelaji,
                loppupvm=row.pvm,
                tyhjennyskerrat=row.tyhjennyskerrat,
                massa=row.massa,
                tilavuus=row.tilavuus,
            )

            asiakas.tyhjennystapahtumat.append(tyhjennystapahtuma)

        return jkr_data

    def _append_keraysvalineet(self, jkr_data: JkrData):
        for row in self._source.keraysvalineet:
            tunnus = self.tunnus_from_asiakasnumero(row.asiakasnumero)
            if tunnus not in jkr_data.asiakkaat:
                logger.warning(
                    "Ohitetaan keräysväline. Ei asiakasta: " f"'{row.asiakasnumero}'"
                )
                continue

            sopimus = self._find_current_tyhjennyssopimus(
                jkr_data, tunnus, row.jatelaji, jkr_data.loppupvm
            )
            if not sopimus and row.jatelaji in (
                JkrJatelaji.liete,
                JkrJatelaji.mustaliete,
                JkrJatelaji.harmaaliete,
            ):
                sopimus = TyhjennysSopimus(
                    sopimustyyppi=SopimusTyyppi.tyhjennyssopimus,
                    jatelaji=JkrJatelaji(row.jatelaji),
                )
                jkr_data.asiakkaat[tunnus].sopimukset.append(sopimus)
            elif not sopimus:
                logger.warning(
                    f"Ohitetaan keräysväline. Asiakkaalla '{row.asiakasnumero}' "
                    f"ei tyhjennyssopimusta '{row.jatelaji}'"
                )
                continue
            try:
                keraysvalinetyyppi = JkrKeraysvalineTyyppi(row.tyyppi)
            except ValueError:
                keraysvalinetyyppi = None
            sopimus.keraysvalineet.append(
                JkrKeraysvaline(
                    tilavuus=row.tilavuus,
                    maara=row.maara,
                    tyyppi=keraysvalinetyyppi,
                )
            )

        return jkr_data

    def _append_keskeytykset(self, jkr_data: JkrData):
        for row in self._source.keskeytykset:
            tunnus = self.tunnus_from_asiakasnumero(row.asiakasnumero)
            if tunnus not in jkr_data.asiakkaat:
                logger.warning(
                    "Ohitetaan keskeytys. Ei asiakasta: " f"'{row.asiakasnumero}'"
                )
                continue

            sopimus = self._find_current_tyhjennyssopimus(
                jkr_data, tunnus, row.jatelaji, jkr_data.loppupvm
            )
            if not sopimus:
                logger.warning(
                    f"Ohitetaan Keskeytys. Asiakkaalla '{row.asiakasnumero}' "
                    f"ei {jkr_data.loppupvm:%d.%m.%Y} voimassa olevaa '{row.jatelaji}' "
                    "tyhjennyssopimusta."
                )
                continue

            sopimus.keskeytykset.append(
                JkrKeskeytys(
                    alkupvm=row.alkupvm, loppupvm=row.loppupvm, selite=row.selite
                )
            )

        return jkr_data

    def _append_kimpat(self, jkr_data: JkrData):
        for kimppa_row in self._source.kimpat:
            tunnus = self.tunnus_from_asiakasnumero(kimppa_row.asiakasnumero)
            if tunnus not in jkr_data.asiakkaat:
                logger.warning(
                    "Ohitetaan kimppasopimus. Ei asiakasta: "
                    f"{kimppa_row.asiakasnumero}"
                )
                continue

            if "putkikeräys" in kimppa_row.kimppaisanta.lower():
                sopimustyyppi = SopimusTyyppi.putkikerayssopimus
            elif kimppa_row.kimppaisanta == "Vuosimaksuasiakas":
                sopimustyyppi = SopimusTyyppi.aluekerayssopimus
            else:
                sopimustyyppi = SopimusTyyppi.kimppasopimus

            kimppaisanta = self.tunnus_from_asiakasnumero(kimppa_row.kimppaisanta)
            if (
                sopimustyyppi == SopimusTyyppi.kimppasopimus
                and kimppaisanta not in jkr_data.asiakkaat
            ):
                logger.warning(
                    "Ohitetaan kimppasopimus. Tiedostossa ei kimppaisäntäasiakasta: "
                    f"'{kimppa_row.kimppaisanta}'"
                )
                continue

            if kimppa_row.jatelaji:
                try:
                    jatelaji = JkrJatelaji(kimppa_row.jatelaji)
                except ValueError:
                    jatelaji = JkrJatelaji.muu
            else:
                jatelaji = None

            sopimus = KimppaSopimus(
                sopimustyyppi=sopimustyyppi,
                jatelaji=jatelaji,
                alkupvm=kimppa_row.alkupvm,
                loppupvm=kimppa_row.loppupvm,
                isannan_asiakasnumero=kimppaisanta,
            )
            jkr_data.asiakkaat[tunnus].sopimukset.append(sopimus)

        return jkr_data

    def _find_current_tyhjennyssopimus(
        self, jkr_data: JkrData, tunnus: Tunnus, jatelaji, date
    ):
        try:
            jatelaji = JkrJatelaji(jatelaji)
        except ValueError:
            return None

        return next(
            (
                sopimus
                for sopimus in jkr_data.asiakkaat[tunnus].sopimukset
                if (
                    isinstance(sopimus, TyhjennysSopimus)
                    and sopimus.jatelaji == jatelaji
                    and (sopimus.alkupvm is None or sopimus.alkupvm <= date)
                    and (sopimus.loppupvm is None or date <= sopimus.loppupvm)
                )
            ),
            None,
        )
