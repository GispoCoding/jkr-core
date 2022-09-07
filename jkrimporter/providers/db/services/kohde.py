import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from psycopg2.extras import DateRange
from sqlalchemy import and_
from sqlalchemy import func as sqlalchemyFunc
from sqlalchemy import or_, select
from sqlalchemy.exc import NoResultFound

from .. import codes
from ..codes import KohdeTyyppi, OsapuolenrooliTyyppi
from ..models import (
    Katu,
    Kohde,
    KohteenOsapuolet,
    Kunta,
    Osapuoli,
    Osoite,
    Rakennus,
    UlkoinenAsiakastieto,
)
from ..utils import form_display_name

if TYPE_CHECKING:
    from typing import Union

    from sqlalchemy.orm import Session

    from jkrimporter.model import Asiakas, Tunnus


def is_aluekerays(asiakas: "Asiakas") -> bool:
    return "aluejätepiste" in asiakas.haltija.nimi.lower()


def find_kohde(session: "Session", asiakas: "Asiakas") -> "Union[Kohde, None]":
    kohde = get_kohde_by_asiakasnumero(session, asiakas.asiakasnumero)
    if kohde:
        return kohde

    # kohde = get_kohde_by_address(session, asiakas)
    # if kohde:
    #     return kohde

    return None


def get_ulkoinen_asiakastieto(
    session: "Session", ulkoinen_tunnus: "Tunnus"
) -> "Union[UlkoinenAsiakastieto, None]":
    query = select(UlkoinenAsiakastieto).where(
        UlkoinenAsiakastieto.tiedontuottaja_tunnus == ulkoinen_tunnus.jarjestelma,
        UlkoinenAsiakastieto.ulkoinen_id == ulkoinen_tunnus.tunnus,
    )
    try:
        return session.execute(query).scalar_one()
    except NoResultFound:
        return None


def find_or_create_asiakastieto(
    session: "Session", asiakas: "Asiakas"
) -> UlkoinenAsiakastieto:
    tunnus = asiakas.asiakasnumero

    query = select(UlkoinenAsiakastieto).where(
        UlkoinenAsiakastieto.tiedontuottaja_tunnus == tunnus.jarjestelma,
        UlkoinenAsiakastieto.ulkoinen_id == tunnus.tunnus,
    )
    try:
        db_asiakastieto = session.execute(query).scalar_one()
    except NoResultFound:
        db_asiakastieto = UlkoinenAsiakastieto(
            tiedontuottaja_tunnus="PJH", ulkoinen_id=tunnus.tunnus
        )

    return db_asiakastieto


def update_ulkoinen_asiakastieto(ulkoinen_asiakastieto, asiakas: "Asiakas"):
    if ulkoinen_asiakastieto.ulkoinen_asiakastieto != asiakas.ulkoinen_asiakastieto:
        ulkoinen_asiakastieto.ulkoinen_asiakastieto = asiakas.ulkoinen_asiakastieto


def find_kohde_by_asiakastiedot(
    session: "Session", asiakas: "Asiakas"
) -> "Union[Kohde, None]":

    ulkoinen_asiakastieto_exists = (
        select(1)
        .where(
            UlkoinenAsiakastieto.kohde_id == Kohde.id,
            UlkoinenAsiakastieto.tiedontuottaja_tunnus
            == asiakas.asiakasnumero.jarjestelma,
        )
        .exists()
    )

    filters = []
    if (
        asiakas.haltija.osoite.postitoimipaikka
        and asiakas.haltija.osoite.katunimi
        and asiakas.haltija.osoite.osoitenumero
    ):
        filters.append(
            and_(
                sqlalchemyFunc.lower(Kunta.nimi_fi)
                == asiakas.haltija.osoite.postitoimipaikka.lower(),  # TODO: korjaa kunta <> postitoimipaikka
                or_(
                    sqlalchemyFunc.lower(Katu.katunimi_fi)
                    == asiakas.haltija.osoite.katunimi.lower(),
                    sqlalchemyFunc.lower(Katu.katunimi_sv)
                    == asiakas.haltija.osoite.katunimi.lower(),
                ),
                Osoite.osoitenumero == asiakas.haltija.osoite.osoitenumero,
            )
        )
    if asiakas.rakennukset:
        filters.append(Rakennus.prt.in_(asiakas.rakennukset))
    if asiakas.kiinteistot:
        filters.append(Rakennus.kiinteistotunnus.in_(asiakas.kiinteistot))

    query = (
        select(Kohde.id, Osapuoli.nimi)
        .join(Kohde.rakennus_collection)
        .join(KohteenOsapuolet)
        .join(Osapuoli)
        .join(Osoite, isouter=True)
        .join(Katu, isouter=True)
        .join(Kunta, isouter=True)
        .where(
            ~ulkoinen_asiakastieto_exists,
            Kohde.voimassaolo.overlaps(
                DateRange(
                    asiakas.alkupvm or datetime.date.min,
                    asiakas.loppupvm or datetime.date.max,
                )
            ),
            KohteenOsapuolet.osapuolenrooli
            == codes.osapuolenroolit[OsapuolenrooliTyyppi.ASIAKAS],
            or_(*filters),
        )
        .distinct()
    )

    try:
        kohteet = session.execute(query).all()
    except NoResultFound:
        return None

    haltija_name_parts = set(asiakas.haltija.nimi.lower().split())
    for kohde_id, db_asiakas_name in kohteet:
        db_asiakas_name_parts = set(db_asiakas_name.lower().split())
        if haltija_name_parts.issubset(
            db_asiakas_name_parts
        ) or db_asiakas_name_parts.issubset(haltija_name_parts):
            kohde = session.get(Kohde, kohde_id)
            return kohde

    return None


def update_kohde(kohde: Kohde, asiakas: "Asiakas"):
    if kohde.alkupvm != asiakas.alkupvm:
        kohde.alkupvm = asiakas.alkupvm
    if kohde.loppupvm != asiakas.loppupvm:
        kohde.loppupvm = asiakas.loppupvm


def get_kohde_by_asiakasnumero(
    session: "Session", tunnus: "Tunnus"
) -> "Union[Kohde, None]":
    query = (
        select(Kohde)
        .join(UlkoinenAsiakastieto)
        .where(
            UlkoinenAsiakastieto.tiedontuottaja_tunnus == tunnus.jarjestelma,
            UlkoinenAsiakastieto.ulkoinen_id == tunnus.tunnus,
        )
    )
    try:
        kohde = session.execute(query).scalar_one()
    except NoResultFound:
        kohde = None

    return kohde


@lru_cache(maxsize=32)
def get_or_create_pseudokohde(session: "Session", nimi: str, kohdetyyppi) -> Kohde:
    kohdetyyppi = codes.kohdetyypit[kohdetyyppi]
    query = select(Kohde).where(Kohde.nimi == nimi, Kohde.kohdetyyppi == kohdetyyppi)
    try:
        kohde = session.execute(query).scalar_one()
    except NoResultFound:
        kohde = Kohde(nimi=nimi, kohdetyyppi=kohdetyyppi)
        session.add(kohde)

    return kohde


def get_kohde_by_address(
    session: "Session", asiakas: "Asiakas"
) -> "Union[Kohde, None]":
    # TODO: etsi kohde etsimällä rakennukset käyttäen find_buildings_for_kohde
    # funktiota. Valitse näistä oikea kohde.
    ...


def add_ulkoinen_asiakastieto_for_kohde(
    session: "Session", kohde: Kohde, asiakas: "Asiakas"
):
    asiakastieto = UlkoinenAsiakastieto(
        tiedontuottaja_tunnus=asiakas.asiakasnumero.jarjestelma,
        ulkoinen_id=asiakas.asiakasnumero.tunnus,
        ulkoinen_asiakastieto=asiakas.ulkoinen_asiakastieto,
        kohde=kohde,
    )

    session.add(asiakastieto)

    return asiakastieto


def create_new_kohde(session: "Session", asiakas: "Asiakas"):
    kohdetyyppi = codes.kohdetyypit[
        KohdeTyyppi.ALUEKERAYS if is_aluekerays(asiakas) else KohdeTyyppi.KIINTEISTO
    ]
    kohde_display_name = form_display_name(asiakas.haltija)
    kohde = Kohde(
        nimi=kohde_display_name,
        kohdetyyppi=kohdetyyppi,
        alkupvm=asiakas.alkupvm,
        loppupvm=asiakas.loppupvm,
    )

    return kohde
