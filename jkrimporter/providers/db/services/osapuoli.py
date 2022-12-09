from typing import TYPE_CHECKING

from jkrimporter.model import Asiakas

from .. import codes
from ..codes import OsapuolenlajiTyyppi, OsapuolenrooliTyyppi
from ..models import KohteenOsapuolet, Osapuoli
from ..utils import is_asoy


def create_or_update_haltija_osapuoli(
    session, kohde, asiakas: "Asiakas", update_contacts: bool
):
    """
    Luo kohteelle haltijaosapuolen

    TODO: päivitä haltijan/asiakakkaan yhteystiedot (ml. poistaminen) #26
    """

    asiakasrooli = codes.osapuolenroolit[OsapuolenrooliTyyppi.ASIAKAS]

    # Filter osapuoli by the same tiedontuottaja. This way, we don't
    # override data coming from other tiedontuottajat, including DVV.
    tiedontuottaja = asiakas.asiakasnumero.jarjestelma
    # this is any asiakas from the same source
    db_haltijat = [
        kohteen_osapuoli.osapuoli
        for kohteen_osapuoli in kohde.kohteen_osapuolet_collection
        if kohteen_osapuoli.osapuolenrooli == asiakasrooli and
        kohteen_osapuoli.osapuoli.tiedontuottaja_tunnus == tiedontuottaja
    ]

    # this is asiakas with the same name and address
    exists = any(
        db_haltija.nimi == asiakas.haltija.nimi
        and db_haltija.katuosoite == str(asiakas.haltija.osoite)
        for db_haltija in db_haltijat
    )
    if not db_haltijat or (update_contacts and not exists):
        print("Haltija changed or not found in db, creating new haltija!")
        # Haltija has changed. We must create a new osapuoli. The old
        # haltija is still valid for the old data, so we don't want to
        # delete them.
        jatteenhaltija = Osapuoli(
            nimi=asiakas.haltija.nimi,
            katuosoite=str(asiakas.haltija.osoite),
            postinumero=asiakas.haltija.osoite.postinumero,
            postitoimipaikka=asiakas.haltija.osoite.postitoimipaikka,
            ytunnus=asiakas.haltija.ytunnus,
            tiedontuottaja_tunnus=asiakas.asiakasnumero.jarjestelma
        )
        if is_asoy(asiakas.haltija.nimi):
            jatteenhaltija.osapuolenlaji = codes.osapuolenlajit[
                OsapuolenlajiTyyppi.ASOY
            ]

        kohteen_osapuoli = KohteenOsapuolet(
            kohde=kohde, osapuoli=jatteenhaltija, osapuolenrooli=asiakasrooli
        )

        session.add(kohteen_osapuoli)


def create_or_update_yhteystieto_osapuoli(
    session, kohde, asiakas: "Asiakas", update_contacts: bool
):
    yhteystietorooli = codes.osapuolenroolit[OsapuolenrooliTyyppi.YHTEYSTIETO]

    # Filter osapuoli by the same tiedontuottaja. This way, we don't
    # override data coming from other tiedontuottajat, including DVV.
    tiedontuottaja = asiakas.asiakasnumero.jarjestelma
    # this is any yhteyshenkilö from the same source
    db_yhteyshenkilo = next(
        (
            kohteen_osapuoli.osapuoli
            for kohteen_osapuoli in kohde.kohteen_osapuolet_collection
            if kohteen_osapuoli.osapuolenrooli == yhteystietorooli and
            kohteen_osapuoli.osapuoli.tiedontuottaja_tunnus == tiedontuottaja
        ),
        None,
    )
    if db_yhteyshenkilo and update_contacts:
        # Yhteystieto has changed. They are not jätteenhaltija, so we may just
        # update the yhteystieto, overwriting the old yhteyshenkilö.
        if (
            db_yhteyshenkilo.nimi != asiakas.yhteyshenkilo.nimi
            or db_yhteyshenkilo.katuosoite != asiakas.yhteyshenkilo.osoite.katunimi
            or db_yhteyshenkilo.postinumero != asiakas.yhteyshenkilo.osoite.postinumero
            or db_yhteyshenkilo.postitoimipaikka
            != asiakas.yhteyshenkilo.osoite.postitoimipaikka
            or db_yhteyshenkilo.ytunnus != asiakas.yhteyshenkilo.ytunnus
        ):
            print("Yhteystieto changed in data, updating!")
            db_yhteyshenkilo.nimi = asiakas.yhteyshenkilo.nimi
            db_yhteyshenkilo.katuosoite = asiakas.yhteyshenkilo.osoite.katunimi
            db_yhteyshenkilo.postinumero = asiakas.yhteyshenkilo.osoite.postinumero
            db_yhteyshenkilo.postitoimipaikka = (
                asiakas.yhteyshenkilo.osoite.postitoimipaikka
            )
            db_yhteyshenkilo.ytunnus = asiakas.yhteyshenkilo.ytunnus
    else:
        yhteyshenkilo = Osapuoli(
            nimi=asiakas.yhteyshenkilo.nimi,
            katuosoite=asiakas.yhteyshenkilo.osoite.katunimi,
            postinumero=asiakas.yhteyshenkilo.osoite.postinumero,
            postitoimipaikka=asiakas.yhteyshenkilo.osoite.postitoimipaikka,
            kunta=asiakas.yhteyshenkilo.osoite.kunta,
            ytunnus=asiakas.yhteyshenkilo.ytunnus,
            tiedontuottaja_tunnus=asiakas.asiakasnumero.jarjestelma
        )

        kohteen_osapuoli = KohteenOsapuolet(
            kohde=kohde, osapuoli=yhteyshenkilo, osapuolenrooli=yhteystietorooli
        )
        session.add(kohteen_osapuoli)
