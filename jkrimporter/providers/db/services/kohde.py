import datetime
import re
from collections import defaultdict
from functools import lru_cache
from typing import TYPE_CHECKING

from openpyxl import load_workbook
from psycopg2.extras import DateRange
from sqlalchemy import and_, exists
from sqlalchemy import func as sqlalchemyFunc
from sqlalchemy import or_, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm.decl_api import DeclarativeMeta

from jkrimporter.model import Yhteystieto

from .. import codes
from ..codes import KohdeTyyppi, OsapuolenrooliTyyppi, RakennuksenKayttotarkoitusTyyppi
from ..models import (
    Katu,
    Kohde,
    KohteenOsapuolet,
    KohteenRakennukset,
    Osapuoli,
    Osoite,
    RakennuksenOmistajat,
    RakennuksenVanhimmat,
    Rakennus,
    UlkoinenAsiakastieto,
)
from ..utils import clean_asoy_name, form_display_name, is_asoy, is_company, is_yhteiso

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

    from sqlalchemy.orm import Session
    from sqlalchemy.sql.selectable import Select

    from jkrimporter.model import Asiakas, Tunnus

    Rakennustiedot = Tuple[Rakennus, FrozenSet[Osapuoli], FrozenSet[Osoite]]


separators = [r"\s\&\s", r"\sja\s"]
separator_regex = r"|".join(separators)


def match_name(first: str, second: str) -> bool:
    """
    Returns true if one name is subset of the other, i.e. the same name without extra
    parts. Consider typical separators combining names.
    """
    first = re.split(separator_regex, first)
    second = re.split(separator_regex, second)
    if len(first) > 1 or len(second) > 1:
        # TODO: if names have common surname, paste it to the name that is missing
        # surname. Will take some detective work tho.
        return any(
            match_name(first_name, second_name)
            for first_name in first
            for second_name in second
        )
    first_parts = set(first[0].lower().split())
    second_parts = set(second[0].lower().split())
    return first_parts.issubset(second_parts) or second_parts.issubset(first_parts)


def is_aluekerays(asiakas: "Asiakas") -> bool:
    return "aluejätepiste" in asiakas.haltija.nimi.lower()


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


def update_ulkoinen_asiakastieto(ulkoinen_asiakastieto, asiakas: "Asiakas"):
    if ulkoinen_asiakastieto.ulkoinen_asiakastieto != asiakas.ulkoinen_asiakastieto:
        ulkoinen_asiakastieto.ulkoinen_asiakastieto = asiakas.ulkoinen_asiakastieto


def find_kohde_by_prt(session: "Session", asiakas: "Asiakas") -> "Union[Kohde, None]":
    print(f"asiakas has prts {asiakas.rakennukset}")
    return _find_kohde_by_asiakastiedot(
        session, Rakennus.prt.in_(asiakas.rakennukset), asiakas
    )


def find_kohde_by_kiinteisto(
    session: "Session", asiakas: "Asiakas"
) -> "Union[Kohde, None]":
    print(f"asiakas has kiinteistöt {asiakas.kiinteistot}")
    return _find_kohde_by_asiakastiedot(
        session, Rakennus.kiinteistotunnus.in_(asiakas.kiinteistot), asiakas
    )


def find_kohde_by_address(
    session: "Session", asiakas: "Asiakas"
) -> "Union[Kohde, None]":
    print("matching by address:")
    # The osoitenumero may contain dash. In that case, the buildings may be
    # listed as separate in DVV data.
    if (
        asiakas.haltija.osoite.osoitenumero
        and "-" in asiakas.haltija.osoite.osoitenumero
    ):
        osoitenumerot = asiakas.haltija.osoite.osoitenumero.split("-", maxsplit=1)
    else:
        osoitenumerot = [asiakas.haltija.osoite.osoitenumero]

    print(osoitenumerot)
    # The address parser parses Metsätie 33 A so that 33 is osoitenumero and A is
    # huoneistotunnus. While the parsing is correct, it may very well also mean (and
    # in many cases it means) osoitenumero 33a and empty huoneistonumero.
    potential_osoitenumero_suffix = (
        asiakas.haltija.osoite.huoneistotunnus.lower()
        if asiakas.haltija.osoite.huoneistotunnus
        else ""
    )
    if potential_osoitenumero_suffix:
        osoitenumerot_with_suffix = [
            osoitenumero + potential_osoitenumero_suffix
            for osoitenumero in osoitenumerot
        ]
    else:
        osoitenumerot_with_suffix = []
    print(osoitenumerot_with_suffix)

    # - Do *NOT* find Sokeritopankatu 18 *AND* Sokeritopankatu 18a by
    # Sokeritopankatu 18 A. Looks like Sokeritopankatu 18, 18 A and 18 B are *all*
    # separate.
    # - Do *NOT* find Mukkulankatu 51 *AND* Mukkulankatu 51b by Mukkulankatu 51.
    # - Find Mukkulankatu 51 by Mukkulankatu 51 B.
    # - Only find Mukkulankatu 51 by Mukkulankatu 51.
    # - Only find Mukkulankatu 51b by Mukkulankatu 51b.
    if osoitenumerot_with_suffix:
        osoitenumero_filter = or_(
            # first, check if the letter is actually part of osoitenumero
            Osoite.osoitenumero.in_(osoitenumerot_with_suffix),
            # if the letter is not found in osoitenumero, it is huoneistotunnus
            and_(
                ~exists(Osoite.osoitenumero.in_(osoitenumerot_with_suffix)),
                Osoite.osoitenumero.in_(osoitenumerot),
            ),
        )
    else:
        osoitenumero_filter = Osoite.osoitenumero.in_(osoitenumerot)
    print(osoitenumero_filter)
    # TODO: Do we need to check the kohde has vanhemmat with the correct
    # huoneistotunnus?
    # We do it when adding buildings to a new kohde in buildings.py.

    filter = and_(
        sqlalchemyFunc.lower(Osoite.posti_numero) == asiakas.haltija.osoite.postinumero,
        or_(
            sqlalchemyFunc.lower(Katu.katunimi_fi)
            == asiakas.haltija.osoite.katunimi.lower(),
            sqlalchemyFunc.lower(Katu.katunimi_sv)
            == asiakas.haltija.osoite.katunimi.lower(),
        ),
        osoitenumero_filter,
    )
    print(filter)

    return _find_kohde_by_asiakastiedot(session, filter, asiakas)


def _find_kohde_by_asiakastiedot(
    session: "Session", filter, asiakas: "Asiakas"
) -> "Union[Kohde, None]":
    # The same kohde may be client at multiple urakoitsijat and have multiple customer
    # ids. Do *not* filter by missing/existing customer id.
    print(filter)
    query = (
        select(Kohde.id, Osapuoli.nimi)
        .join(Kohde.rakennus_collection)
        .join(KohteenOsapuolet)
        .join(Osapuoli)
        .join(Osoite, isouter=True)
        .join(Katu, isouter=True)
        .where(
            Kohde.voimassaolo.overlaps(
                DateRange(
                    asiakas.voimassa.lower or datetime.date.min,
                    asiakas.voimassa.upper or datetime.date.max,
                )
            ),
            # Any yhteystieto will do. The bill might not always go
            # to the oldest person. It might be the owner.
            # KohteenOsapuolet.osapuolenrooli
            # == codes.osapuolenroolit[OsapuolenrooliTyyppi.ASIAKAS],
            filter,
        )
        .distinct()
    )
    print(query)

    try:
        kohteet = session.execute(query).all()
    except NoResultFound:
        return None
    print(kohteet)

    names_by_kohde_id = defaultdict(set)
    for kohde_id, db_osapuoli_name in kohteet:
        names_by_kohde_id[kohde_id].add(db_osapuoli_name)
    if len(names_by_kohde_id) > 1:
        # The address has multiple kohteet for the same date period.
        # We may have
        # 1) multiple perusmaksut for the same building (not paritalo),
        # 2) paritalo,
        # 3) multiple buildings in the same address (not paritalo),
        # 4) multiple people moving in or out of the building in the same time period.
        # Since we have no customer id here, we just have to check if the name is
        # actually an osapuoli of an existing kohde or not. If not, we will return
        # None and create a new kohde later. If an osapuoli exists, the new kohde may
        # have been created from a kuljetus already.
        print(
            "Found multiple kohteet with the same address. Checking owners/inhabitants..."
        )
        haltija_nimi = clean_asoy_name(asiakas.haltija.nimi)
        yhteystieto_nimi = clean_asoy_name(asiakas.yhteyshenkilo.nimi)
        for kohde_id, db_osapuoli_names in names_by_kohde_id.items():
            for db_osapuoli_name in db_osapuoli_names:
                db_osapuoli_name = clean_asoy_name(db_osapuoli_name)
                print(haltija_nimi)
                print(db_osapuoli_name)
                if match_name(haltija_nimi, db_osapuoli_name) or match_name(
                    yhteystieto_nimi, db_osapuoli_name
                ):
                    print(f"{db_osapuoli_name} match")
                    kohde = session.get(Kohde, kohde_id)
                    print("returning kohde")
                    return kohde
    elif len(names_by_kohde_id) == 1:
        return session.get(Kohde, next(iter(names_by_kohde_id.keys())))

    return None


def update_kohde(kohde: Kohde, asiakas: "Asiakas"):
    if kohde.alkupvm != asiakas.voimassa.lower:
        kohde.alkupvm = asiakas.voimassa.lower
    if kohde.loppupvm != asiakas.voimassa.upper:
        kohde.loppupvm = asiakas.voimassa.upper


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
        alkupvm=asiakas.voimassa.lower,
        loppupvm=asiakas.voimassa.upper,
    )

    return kohde


def create_new_kohde_from_buildings(
    session: "Session",
    rakennus_ids: "List[int]",
    asiakkaat: "Set[Osapuoli]",
    yhteystiedot: "Set[Osapuoli]",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
):
    """
    Create combined kohde for the given list of building ids. Asiakkaat will be
    used for kohde name and osapuoli. Yhteystiedot will be added as additional contacts
    for kohde, i.e. they are additional inhabitants and/or owners.
    """
    # prefer companies over private owners when naming combined objects
    asoy_asiakkaat = {osapuoli for osapuoli in asiakkaat if is_asoy(osapuoli.nimi)}
    company_asiakkaat = {
        osapuoli for osapuoli in asiakkaat if is_company(osapuoli.nimi)
    }
    yhteiso_asiakkaat = {
        osapuoli for osapuoli in asiakkaat if is_yhteiso(osapuoli.nimi)
    }
    if asoy_asiakkaat:
        asiakas = next(iter(asoy_asiakkaat))
    elif company_asiakkaat:
        asiakas = next(iter(company_asiakkaat))
    elif yhteiso_asiakkaat:
        asiakas = next(iter(yhteiso_asiakkaat))
    else:
        asiakas = next(iter(asiakkaat))
    kohde_display_name = form_display_name(
        Yhteystieto(
            asiakas.nimi,
            asiakas.katuosoite,
            asiakas.ytunnus,
            asiakas.henkilotunnus,
        )
    )
    kohde = Kohde(
        nimi=kohde_display_name,
        kohdetyyppi=codes.kohdetyypit[KohdeTyyppi.KIINTEISTO],
        alkupvm=alkupvm,
        loppupvm=loppupvm,
    )
    session.add(kohde)
    # we need to get the id for the kohde from db
    session.flush()

    # create dependent objects
    for rakennus_id in rakennus_ids:
        kohteen_rakennus = KohteenRakennukset(
            rakennus_id=rakennus_id, kohde_id=kohde.id
        )
        session.add(kohteen_rakennus)
    for osapuoli in asiakkaat:
        asiakas = KohteenOsapuolet(
            osapuoli_id=osapuoli.id,
            kohde_id=kohde.id,
            osapuolenrooli=codes.osapuolenroolit[OsapuolenrooliTyyppi.ASIAKAS],
        )
        session.add(asiakas)
    # yhteystiedot should not duplicate asiakas data
    for osapuoli in yhteystiedot - asiakkaat:
        yhteystieto = KohteenOsapuolet(
            osapuoli_id=osapuoli.id,
            kohde_id=kohde.id,
            osapuolenrooli=codes.osapuolenroolit[OsapuolenrooliTyyppi.YHTEYSTIETO],
        )
        session.add(yhteystieto)
    return kohde


def create_kohteet_from_vanhimmat(
    session: "Session",
    ids: "Select",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
):
    """
    Create one kohde for each RakennuksenVanhimmat osapuoli id provided by
    the select query.
    """
    # iterate vanhimmat to create kohde with the right name, client and building
    vanhimmat_osapuolet_query = (
        select(RakennuksenVanhimmat, Osapuoli)
        .join(RakennuksenVanhimmat.osapuoli)
        .filter(RakennuksenVanhimmat.osapuoli_id.in_(ids))
    )
    vanhimmat_osapuolet = session.execute(vanhimmat_osapuolet_query).all()
    print(f"Found {len(vanhimmat_osapuolet)} vanhimmat without kohde")
    kohteet = []
    for (vanhin, osapuoli) in vanhimmat_osapuolet:
        # The oldest inhabitant is the customer. Also save owners as backup
        # contacts.
        omistajat_query = (
            select(RakennuksenOmistajat, Osapuoli)
            .join(RakennuksenOmistajat.osapuoli)
            .where(RakennuksenOmistajat.rakennus_id == vanhin.rakennus_id)
        )
        omistajat = {row[1] for row in session.execute(omistajat_query).all()}
        print(f"Building {vanhin.rakennus_id} has owners {omistajat}")
        kohde = create_new_kohde_from_buildings(
            session, [vanhin.rakennus_id], {osapuoli}, omistajat, alkupvm, loppupvm
        )
        kohteet.append(kohde)
    return kohteet


# def _split_buildings_by_customer_and_address(
#     session: "Session",
#     rakennus_ids: "List[Set[int]]",
#     customer_table: "DeclarativeMeta" = RakennuksenOmistajat,
# ) -> "List[Set[int]]":
#     """
#     Returns building sets split into groups of same customer and address.
#     Customer_table is the table to use for customer id. By default, we assume
#     RakennuksenOmistajat to be the customer. The customer may also be
#     RakennuksenVanhimmat.

#     If the same kiinteistotunnus has buildings with multiple customers,
#     first kohde will contain all buildings owned by the owner with the most buildings.

#     Then, create second kohde from remaining buildings owned by the owner with the
#     second largest number of buildings, etc., until all buildings have kohde named
#     after their owner.

#     Finally, if the buildings have different addresses, separate buildings to
#     those having the same address.
#     """


def _match_address(first: "Osoite", second: "Osoite"):
    """
    Determines whether two buildings are at the same address. Osoite is
    the many to many table between Rakennus and Katu.
    """
    return first.katu_id == second.katu_id and first.osoitenumero == second.osoitenumero


def _match_addresses(first: "Set[Osoite]", second: "Set[Osoite]"):
    """
    Determines whether address sets contain any common addresses.
    """
    for osoite in first:
        for address in second:
            print("comparing {osoite} to {address}")
            if _match_address(osoite, address):
                print("found common address")
                return True
    print("no common addresses!")
    return False


def _is_auxiliary_building(rakennus: "Rakennus"):
    """
    Determines which buildings should always be merged in same kohde with the main
    building.
    """
    return rakennus.rakennuksenkayttotarkoitus in (
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.SAUNA],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.TALOUSRAKENNUS
        ],
    )


def _add_auxiliary_buildings(
    dvv_rakennustiedot: "List[Rakennustiedot]",
    building_sets: "List[Set[Rakennustiedot]]",
) -> "List[Set[Rakennustiedot]]":
    """
    For each building set, adds any auxiliary building(s) on the same kiinteistö(t)
    having the same owner(s) and address(es) as any of the existing buildings.
    """
    dvv_rakennustiedot_by_kiinteistotunnus: "dict[int, Set[Rakennustiedot]]" = (
        defaultdict(set)
    )
    for rakennus, osapuolet, osoitteet in dvv_rakennustiedot:
        dvv_rakennustiedot_by_kiinteistotunnus[rakennus.kiinteistotunnus].add(
            (rakennus, frozenset(osapuolet), frozenset(osoitteet))
        )
    print(f"Found {len(dvv_rakennustiedot_by_kiinteistotunnus)} DVV kiinteistöt")
    sets_to_return: "List[Set[Rakennustiedot]]" = []
    for building_set in building_sets:
        set_to_return = building_set.copy()
        print("---")
        print(f"Adding to building set {set_to_return}")
        for building, owners, addresses in building_set:
            print(f"has owners {owners}")
            print(f"has addresses {addresses}")
            kiinteistotunnus = building.kiinteistotunnus
            # Some buildings have missing kiinteistötunnus. Cannot join them =)
            if kiinteistotunnus:
                print(f"has kiinteistötunnus {kiinteistotunnus}")
                kiinteiston_lisarakennukset: "Set[Rakennustiedot]" = {
                    (rakennus, osapuolet, osoitteet)
                    for rakennus, osapuolet, osoitteet in dvv_rakennustiedot_by_kiinteistotunnus[  # noqa
                        kiinteistotunnus
                    ]
                    if _is_auxiliary_building(rakennus)
                }
                print(f"has lisärakennukset {kiinteiston_lisarakennukset}")

                # only add lisärakennukset having at least one common owner and address
                rakennustiedot_to_add = {
                    (rakennus, osapuolet, osoitteet)
                    for rakennus, osapuolet, osoitteet in kiinteiston_lisarakennukset
                    if osapuolet & owners and _match_addresses(osoitteet, addresses)
                }
                # If the addresses or owners differ, remaining objects on the same
                # kiinteistö will create separate kohteet in the last import stage.
                print(f"owners own only {rakennustiedot_to_add} at same address")

                # Only add auxiliary buildings to one kohde, remove them from
                # future processing. They are mapped to the first owner and address
                # found.
                dvv_rakennustiedot_by_kiinteistotunnus[
                    kiinteistotunnus
                ] -= rakennustiedot_to_add
                print(
                    f"Remaining rakennustiedot {dvv_rakennustiedot_by_kiinteistotunnus[kiinteistotunnus]}"
                )
                set_to_return |= rakennustiedot_to_add
        print(f"Final building set {set_to_return}")
        sets_to_return.append(set_to_return)
    return sets_to_return


def create_kohteet_from_kiinteisto(
    session: "Session",
    kiinteistotunnukset: "Select",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
    customer_table: "Optional[DeclarativeMeta]",
):
    """
    Create at least one kohde from each kiinteistotunnus provided by the select query.
    Customer_table is the optional additional table to use for customer id. By default,
    each kohde will have all owners as its customer. In this case, owners will be added
    as additional contacts.

    If the same kiinteistotunnus has buildings with multiple owners,
    first kohde will contain all buildings owned by the owner with the most buildings.

    Then, create second kohde from remaining buildings owned by the owner with the
    second largest number of buildings, etc., until all buildings have kohde named
    after their owner.

    Finally, if the buildings have different addresses, separate buildings to
    those having the same address.
    """
    print("Loading kiinteistötunnukset...")
    kiinteistotunnukset = [
        result[0] for result in session.execute(kiinteistotunnukset).all()
    ]
    print(f"Found {len(kiinteistotunnukset)} kiinteistötunnukset to import")
    print("Loading rakennukset...")
    # Fastest to load everything to memory first.
    rakennus_ids = session.execute(
        select(Rakennus.kiinteistotunnus, Rakennus.id)
        # Cannot filter buildings to load here by type. *Any* buildings that have an
        # inhabitant should be imported wholesale.
        #
        # We must only filter out buildings with existing kohde here, since the same
        # kiinteistö might have buildings both with and without kohde, in case some
        # buildings have been imported in previous steps.
        .filter(~Rakennus.kohde_collection.any())
    ).all()
    rakennus_ids_by_kiinteistotunnus = defaultdict(set)
    for (tunnus, rakennus_id) in rakennus_ids:
        rakennus_ids_by_kiinteistotunnus[tunnus].add(rakennus_id)

    print("Loading omistajat...")
    rakennus_owners = session.execute(
        select(RakennuksenOmistajat.rakennus_id, Osapuoli).join(
            Osapuoli, RakennuksenOmistajat.osapuoli_id == Osapuoli.id
        )
    ).all()
    owners_by_rakennus_id = defaultdict(set)
    rakennus_ids_by_owner_id = defaultdict(set)
    for (rakennus_id, owner) in rakennus_owners:
        owners_by_rakennus_id[rakennus_id].add(owner)
        rakennus_ids_by_owner_id[owner.id].add(rakennus_id)

    if customer_table:
        print("Vanhimmat table provided, loading vanhimmat...")
        rakennus_customers = session.execute(
            select(customer_table.rakennus_id, Osapuoli).join(
                Osapuoli, customer_table.osapuoli_id == Osapuoli.id
            )
        ).all()
        customers_by_rakennus_id = defaultdict(set)
        for (rakennus_id, customer) in rakennus_customers:
            customers_by_rakennus_id[rakennus_id].add(customer)

    print("Loading addresses...")
    rakennus_addresses = session.execute(
        select(Rakennus.id, Osoite).join(Osoite, Rakennus.id == Osoite.rakennus_id)
    ).all()
    addresses_by_rakennus_id: "Dict[int, Set[Osoite]]" = defaultdict(set)
    for (rakennus_id, address) in rakennus_addresses:
        addresses_by_rakennus_id[rakennus_id].add(address)

    kohteet = []
    print("Processing kiinteistötunnukset...")
    for kiinteistotunnus in kiinteistotunnukset:
        print(f"----- Processing kiinteistötunnus {kiinteistotunnus} -----")
        if not kiinteistotunnus:
            # some buildings have missing kiinteistötunnus. cannot import them here.
            continue
        ids_to_add = rakennus_ids_by_kiinteistotunnus[kiinteistotunnus]
        # buildings without owner can never be imported as separate kohde
        ids_without_owner = ids_to_add - set(owners_by_rakennus_id.keys())
        ids_to_add -= ids_without_owner
        while ids_to_add:
            print("---")
            owners = set().union(*[owners_by_rakennus_id[id] for id in ids_to_add])
            # list all buildings on *this* kiinteistö per owner. No way of knowing
            # if the important building (inhabited or interesting one) is even
            # an auxiliary building, so we cannot filter them out. Therefore, no
            # need to filter and add auxiliaries back later.
            buildings_by_owner = {
                owner: ids_to_add & rakennus_ids_by_owner_id[owner.id]
                for owner in owners
            }
            # Start from the owner with the most buildings
            owners_by_buildings = sorted(
                buildings_by_owner.items(),
                key=lambda item: len(item[1]),
                reverse=True,
            )
            first_owner, buildings_owned = owners_by_buildings[0]
            # buildings with no owner will be joined with the largest owner.
            buildings_owned |= ids_without_owner
            ids_without_owner = set()

            # split buildings further by address
            while buildings_owned:
                buildings_by_address = defaultdict(set)
                for building in buildings_owned:
                    for address in addresses_by_rakennus_id[building]:
                        street = address.katu_id
                        number = address.osoitenumero
                        buildings_by_address[(street, number)].add(building)
                # Start from the address with the most buildings
                addresses_by_buildings = sorted(
                    buildings_by_address.items(),
                    key=lambda item: len(item[1]),
                    reverse=True,
                )
                (street, number), buildings_at_address = addresses_by_buildings[0]
                print(f"Processing street {street} osoitenumero {number}")
                print(f"with buildings {buildings_at_address}")
                # Add all owners as contact info
                owners = {
                    owner
                    for owner, buildings in buildings_by_owner.items()
                    if buildings & buildings_at_address
                }
                print(f"Address has owners {owners}")

                # Customer may be the owner or the inhabitant.
                customer = None
                if customer_table:
                    # Try to find the inhabitant. If not found, they most likely
                    # reside in buildings not owned by the major owner or in
                    # a separate address.
                    for rakennus_id in buildings_at_address:
                        customers = customers_by_rakennus_id[rakennus_id]
                        if customers:
                            customer = customers.pop()
                            print(f"found customer {customer}")
                            break
                # If inhabitant was not found, he does not inhabit any of the
                # buildings in this address owned by this owner. Therefore, the
                # bill should go to the owner.
                if not customer:
                    print("Customer is owner")
                    customer = first_owner
                print(f"Creating kohde {customer}: {buildings_at_address}")
                kohde = create_new_kohde_from_buildings(
                    session,
                    list(buildings_at_address),
                    {customer},
                    owners,
                    alkupvm,
                    loppupvm,
                )
                # do not import the building again from second address
                buildings_owned -= buildings_at_address
                ids_to_add -= buildings_at_address
                kohteet.append(kohde)
    return kohteet


def create_single_asunto_kohteet(
    session: "Session",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
) -> "List(Kohde)":
    """
    Create kohteet from all yksittäistalot that do not have kohde. Also consider
    yksittäistalot that do not have an inhabitant.

    If there are multiple inhabited buildings on a kiinteistö, it will not be imported
    here.
    """
    rakennus_id_without_kohde = (
        select(Rakennus.id).filter(
            Rakennus.rakennuksenkayttotarkoitus
            == codes.rakennuksenkayttotarkoitukset[
                RakennuksenKayttotarkoitusTyyppi.YKSITTAISTALO
            ]
        )
        # do not import any rakennus with existing kohteet
        .filter(~Rakennus.kohde_collection.any())
    )
    # Oldest inhabitant should get the bill only if there are no multiple
    # yksittäistalo on the same kiinteistö.
    single_asunto_kiinteistotunnus = (
        select(
            Rakennus.kiinteistotunnus,
            sqlalchemyFunc.count(Rakennus.id),
        )
        .filter(Rakennus.id.in_(rakennus_id_without_kohde))
        .group_by(Rakennus.kiinteistotunnus)
        .having(sqlalchemyFunc.count(Rakennus.id) == 1)
    )

    print("Creating single house kohteet...")
    # merge empty buildings on kiinteistö to the main building only if they have the
    # same owner. Separate owners will get separate kohde, OR
    # TODO: optionally, do not create separate kohteet after all??
    return create_kohteet_from_kiinteisto(
        session, single_asunto_kiinteistotunnus, alkupvm, loppupvm, RakennuksenVanhimmat
    )


def create_paritalo_kohteet(
    session: "Session",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
) -> "List(Kohde)":
    """
    Create kohteet from all paritalo buildings that do not have kohde.
    """
    # Each paritalo can belong to a maximum of two kohde. Create both in this
    # step. If there is only one inhabitant (i.e. another flat is empty, flats
    # are combined, etc.), the building already has one kohde from previous
    # step and needs not be imported here.
    paritalo_rakennus_id_without_kohde = (
        select(Rakennus.id).filter(
            Rakennus.rakennuksenkayttotarkoitus
            == codes.rakennuksenkayttotarkoitukset[
                RakennuksenKayttotarkoitusTyyppi.PARITALO
            ]
        )
        # do not import any rakennus with existing kohteet
        .filter(~Rakennus.kohde_collection.any())
    )
    vanhimmat_ids = select(RakennuksenVanhimmat.osapuoli_id).filter(
        RakennuksenVanhimmat.rakennus_id.in_(paritalo_rakennus_id_without_kohde)
    )
    print("Creating paritalokohteet...")
    # TODO: instead, merge all empty buildings on kiinteistö to the main building
    # Where to merge? We have two identical owners. Better not merge at all?
    return create_kohteet_from_vanhimmat(session, vanhimmat_ids, alkupvm, loppupvm)


def create_multiple_and_uninhabited_kohteet(
    session: "Session", alkupvm: "Optional[str]", loppupvm: "Optional[str]"
) -> "List(Kohde)":
    """
    Create kohteet from all kiinteistötunnus that do not have kohde.
    """
    # One kiinteistö and omistaja -> one kohde.
    # - Name after largest omistaja if there are multiple.
    # - Separate buildings in kiinteistö if omistajas differ.
    # - Separate buildings in kiinteistö if osoitteet differ (prevent huge kiinteistöt)

    kiinteistotunnus_without_kohde = (
        select(Rakennus.kiinteistotunnus)
        # TODO: filter building types here. in that case, must also include all
        # buildings with any inhabitants.
        #
        # 1) yhden asunnon talot (tyhjillään)
        # 2) kunnan palvelutoiminta (pitkä lista)
        # 3) vapaa-ajanasunnot (ei perusmaksua)
        # do not import any rakennus with existing kohteet
        .filter(~Rakennus.kohde_collection.any()).group_by(Rakennus.kiinteistotunnus)
    )
    print("Creating remaining kohteet...")
    # Merge all empty buildings with same owner to the main building.
    # Separate owners will get separate kohde, OR
    # TODO: optionally, do not create separate kohteet after all??
    return create_kohteet_from_kiinteisto(
        session, kiinteistotunnus_without_kohde, alkupvm, loppupvm, None
    )


def _should_have_perusmaksu_kohde(rakennus: "Rakennus"):
    """
    Determines which buildings should be combined using perusmaksu data.
    """
    return rakennus.rakennuksenkayttotarkoitus in (
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.KERROSTALO
        ],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.RIVITALO],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.LUHTITALO],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.KETJUTALO],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.VAPAA_AJANASUNTO
        ],
    )


def create_perusmaksurekisteri_kohteet(
    session: "Session",
    perusmaksutiedosto: "Path",
    alkupvm: "Optional[str]",
    loppupvm: "Optional[str]",
):
    """
    Create kohteet combining all dvv buildings that have the same asiakasnumero in
    perusmaksurekisteri and have the desired type. No need to import anything from
    perusmaksurekisteri, so we don't want a complete provider for the file.

    Since perusmaksurekisteri may be missing sauna and talousrakennus, add them from
    each kiinteistö, matching owner and address.
    """
    perusmaksut = load_workbook(filename=perusmaksutiedosto)
    sheet = perusmaksut["Tietopyyntö asiakasrekisteristä"]
    # some asiakasnumero occur multiple times for the same prt
    buildings_to_combine = defaultdict(lambda: {"prt": set()})
    for index, row in enumerate(sheet.values):
        # skip header
        if index == 0:
            continue
        asiakasnumero = str(row[1])
        prt = str(row[3])
        # asiakkaan_nimi = row[6].value
        # yhteyshenkilon_nimi = row[7].value
        # katuosoite = row[8].value
        # postitoimipaikka = row[9].value
        buildings_to_combine[asiakasnumero]["prt"].add(prt)
    print(f"Found {len(buildings_to_combine)} perusmaksu clients")

    print("Loading rakennukset...")
    # Fastest to load everything to memory first.
    dvv_rakennustiedot: "List[(Rakennus, Osapuoli, Osoite)]" = session.execute(
        select(Rakennus, Osapuoli, Osoite)
        .join(Rakennus.osapuoli_collection)
        .join(Rakennus.osoite_collection)
        # do not import any rakennus with existing kohteet
        .filter(~Rakennus.kohde_collection.any())
    ).all()
    # add all owners for each rakennus
    dvv_rakennustiedot_by_prt: "dict[int, Rakennustiedot]" = {}
    for rakennus, osapuoli, osoite in dvv_rakennustiedot:
        if rakennus.prt not in dvv_rakennustiedot_by_prt:
            dvv_rakennustiedot_by_prt[rakennus.prt] = (rakennus, set(), set())
        dvv_rakennustiedot_by_prt[rakennus.prt][1].add(osapuoli)
        dvv_rakennustiedot_by_prt[rakennus.prt][2].add(osoite)

    print(f"Found {len(dvv_rakennustiedot_by_prt)} DVV buildings without kohde")
    print("Checking perusmaksu clients...")

    building_sets = []
    for kohde_datum in buildings_to_combine.values():
        kohde_prt = kohde_datum["prt"]
        print(f"Got perusmaksu prt numbers {kohde_prt}")
        building_set: "Set[Rakennustiedot]" = set()
        for prt in kohde_prt:
            try:
                rakennus, osapuolet, osoitteet = dvv_rakennustiedot_by_prt[prt]
            except KeyError:
                print(f"PRT {prt} not found in DVV data, skipping building")
                continue
            if _should_have_perusmaksu_kohde(rakennus):
                print(f"found perusmaksukohde {rakennus.prt}")
                # add all owners and addresses for each rakennus
                building_set.add((rakennus, frozenset(osapuolet), frozenset(osoitteet)))
        if len(building_set) == 0:
            print("No DVV buildings of desired type found, skipping asiakas")
            continue
        print(f"Combining buildings {building_set}")
        building_sets.append(building_set)

    kohteet = []
    # merge empty buildings with same address and owner on kiinteistö(t) to the main
    # building
    building_sets = _add_auxiliary_buildings(
        dvv_rakennustiedot_by_prt.values(), building_sets
    )
    for building_set in building_sets:
        rakennus_ids = [rakennustiedot[0].id for rakennustiedot in building_set]
        print(f"Creating from buildings {rakennus_ids}")
        # Add all owners as potential customers for perusmaksuasiakkaat.
        # We have no idea who should pay, and the buildings do not always
        # have common owners at all.
        owner_sets = [rakennustiedot[1] for rakennustiedot in building_set]
        owners = set().union(*owner_sets)
        print(f"Having owners {owners}")
        kohde = create_new_kohde_from_buildings(
            session, rakennus_ids, owners, owners, alkupvm, loppupvm
        )
        kohteet.append(kohde)
    return kohteet
