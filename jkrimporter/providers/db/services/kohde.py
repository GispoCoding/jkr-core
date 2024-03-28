import datetime
import re
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache
from typing import TYPE_CHECKING

from openpyxl import load_workbook
from psycopg2.extras import DateRange
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy import func as sqlalchemyFunc
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm.decl_api import DeclarativeMeta

from jkrimporter.model import Asiakas, JkrIlmoitukset, LopetusIlmoitus, Yhteystieto

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
from .buildings import DISTANCE_LIMIT, minimum_distance_of_buildings

if TYPE_CHECKING:
    from pathlib import Path
    from typing import (
        Callable,
        DefaultDict,
        Dict,
        FrozenSet,
        Hashable,
        List,
        NamedTuple,
        Optional,
        Set,
        Tuple,
        Union,
    )

    from sqlalchemy.orm import Session
    from sqlalchemy.sql.selectable import Select

    from jkrimporter.model import Asiakas, JkrIlmoitukset, Tunnus

    class Kohdetiedot(NamedTuple):
        kohde: Kohde
        rakennukset: FrozenSet[KohteenRakennukset]
        lisarakennukset: FrozenSet[KohteenRakennukset]
        asukkaat: FrozenSet[KohteenOsapuolet]
        omistajat: FrozenSet[KohteenOsapuolet]

    class Rakennustiedot(NamedTuple):
        rakennus: Rakennus
        vanhimmat: FrozenSet[RakennuksenVanhimmat]
        omistajat: FrozenSet[RakennuksenOmistajat]
        osoitteet: FrozenSet[Osoite]


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


def find_kohde_by_prt(
    session: "Session", asiakas: "Union[Asiakas, JkrIlmoitukset, LopetusIlmoitus]"
) -> "Union[Kohde, None]":
    if isinstance(asiakas, JkrIlmoitukset):
        return _find_kohde_by_asiakastiedot(
            session, Rakennus.prt.in_(asiakas.sijainti_prt), asiakas
        )
    elif isinstance(asiakas, Asiakas):
        return _find_kohde_by_asiakastiedot(
            session, Rakennus.prt.in_(asiakas.rakennukset), asiakas
        )
    elif isinstance(asiakas, LopetusIlmoitus):
        return _find_kohde_by_ilmoitustiedot(
            session, Rakennus.prt.in_(asiakas.prt), asiakas
        )
    else:
        raise ValueError("Invalid asiakas type")


def find_kohteet_by_prt(
        session: "Session",
        asiakas: "JkrIlmoitukset"
) -> "Tuple[List[Kohde], List[str]]":
    found_kohteet = []
    not_found_prts = []

    for kompostoija in asiakas.kompostoijat:
        kompostoija_info = f"Nimi: {kompostoija.nimi}, Rakennus: {kompostoija.rakennus}"
        print(f"Kompostoija: {kompostoija_info}")
        query = (
            select(Kohde.id, Osapuoli.nimi)
            .join(Kohde.rakennus_collection)
            .join(KohteenOsapuolet, isouter=True)
            .join(Osapuoli, isouter=True)
            .join(Osoite, isouter=True)
            .where(
                Kohde.voimassaolo.overlaps(
                    DateRange(
                        asiakas.voimassa.lower or datetime.date.min,
                        asiakas.voimassa.upper or datetime.date.max,
                    )
                ),
                Rakennus.prt.in_(kompostoija.rakennus)
            )
            .distinct()
        )

        try:
            kohteet = session.execute(query).all()
        except NoResultFound:
            print("Ei löytynyt kohdetta, prt: {kompostoija.rakennus}")
            not_found_prts.append(kompostoija.rakennus)
            continue

        kompostoija_nimi = clean_asoy_name(kompostoija.nimi)
        if len(kohteet) > 1:
            names_by_kohde_id = defaultdict(set)
            for kohde_id, db_osapuoli_name in kohteet:
                names_by_kohde_id[kohde_id].add(db_osapuoli_name)
            for kohde_id, db_osapuoli_names in names_by_kohde_id.items():
                for db_osapuoli_name in db_osapuoli_names:
                    if db_osapuoli_name is not None:
                        db_osapuoli_name = clean_asoy_name(db_osapuoli_name)
                        print(kompostoija_nimi)
                        print(db_osapuoli_name)
                        if (
                            match_name(kompostoija_nimi, db_osapuoli_name)
                        ):
                            print(f"{db_osapuoli_name} match")
                            kohde = session.get(Kohde, kohde_id)
                            print("Adding kohde to list")
                            found_kohteet.append(kohde)
        elif len(kohteet) == 1:
            kohde_id = kohteet[0][0]
            kohde = session.get(Kohde, kohde_id)
            found_kohteet.append(kohde)
        else:
            print("Ei löytynyt kohdetta, prt: {kompostoija.rakennus}")
            not_found_prts.append(kompostoija.rakennus)

    return found_kohteet, not_found_prts


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


def _find_kohde_by_ilmoitustiedot(
        session: "Session",
        filter,
        ilmoitus: "LopetusIlmoitus"
) -> "Union[Kohde, None]":
    print(f"LopetusIlmoitus.nimi: {ilmoitus.nimi}")
    query = (
        select(Kohde.id, Osapuoli.nimi)
        .join(Kohde.rakennus_collection)
        .join(KohteenOsapuolet, isouter=True)
        .join(Osapuoli, isouter=True)
        .join(Osoite, isouter=True)
        .where(
            Kohde.voimassaolo.overlaps(
                DateRange(
                    ilmoitus.Vastausaika
                )
            ),
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
        print(
            "Found multiple kohteet with the same address. Checking owners/inhabitants..."
        )
        vastuuhenkilo_nimi = clean_asoy_name(ilmoitus.nimi)

        for kohde_id, db_osapuoli_names in names_by_kohde_id.items():
            for db_osapuoli_name in db_osapuoli_names:
                if db_osapuoli_name is not None:
                    db_osapuoli_name = clean_asoy_name(db_osapuoli_name)
                    print(vastuuhenkilo_nimi)
                    print(db_osapuoli_name)
                    if (
                        match_name(vastuuhenkilo_nimi, db_osapuoli_name)
                    ):
                        print(f"{db_osapuoli_name} match")
                        kohde = session.get(Kohde, kohde_id)
                        print("returning kohde")
                        return kohde
    elif len(names_by_kohde_id) == 1:
        return session.get(Kohde, next(iter(names_by_kohde_id.keys())))

    return None


def _find_kohde_by_asiakastiedot(
    session: "Session",
    filter,
    asiakas: "Union[Asiakas, JkrIlmoitukset]"
) -> "Union[Kohde, None]":
    if isinstance(asiakas, JkrIlmoitukset):
        query = (
            select(Kohde.id, Osapuoli.nimi)
            .join(Kohde.rakennus_collection)
            .join(KohteenOsapuolet, isouter=True)
            .join(Osapuoli, isouter=True)
            .join(Osoite, isouter=True)
            .where(
                Kohde.voimassaolo.overlaps(
                    DateRange(
                        asiakas.voimassa.lower or datetime.date.min,
                        asiakas.voimassa.upper or datetime.date.max,
                    )
                ),
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
            print(
                "Found multiple kohteet with the same address. Checking owners/inhabitants..."
            )
            vastuuhenkilo_nimi = clean_asoy_name(asiakas.vastuuhenkilo.nimi)
            kompostoija_nimet = [
                clean_asoy_name(kompostoija.nimi)
                for kompostoija in asiakas.kompostoijat
            ]

            for kohde_id, db_osapuoli_names in names_by_kohde_id.items():
                for db_osapuoli_name in db_osapuoli_names:
                    if db_osapuoli_name is not None:
                        db_osapuoli_name = clean_asoy_name(db_osapuoli_name)
                        print(vastuuhenkilo_nimi)
                        print(db_osapuoli_name)
                        if any(
                            match_name(vastuuhenkilo_nimi, db_osapuoli_name) or
                            match_name(kompostoija_nimi, db_osapuoli_name)
                            for kompostoija_nimi in kompostoija_nimet
                        ):
                            print(f"{db_osapuoli_name} match")
                            kohde = session.get(Kohde, kohde_id)
                            print("returning kohde")
                            return kohde
        elif len(names_by_kohde_id) == 1:
            return session.get(Kohde, next(iter(names_by_kohde_id.keys())))

        return None

    # The same kohde may be client at multiple urakoitsijat and have multiple customer
    # ids. Do *not* filter by missing/existing customer id.
    elif isinstance(asiakas, Asiakas):
        query = (
            select(Kohde.id, Osapuoli.nimi)
            .join(Kohde.rakennus_collection)
            .join(KohteenOsapuolet, isouter=True)
            .join(Osapuoli, isouter=True)
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
                    if db_osapuoli_name is not None:
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
    ...


def get_dvv_rakennustiedot_without_kohde(
    session: "Session",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
) -> "Dict[int, Rakennustiedot]":
    # Fastest to load everything to memory first.
    if loppupvm is None:
        rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(poimintapvm == Kohde.alkupvm)
        )
    else:
        rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(Kohde.voimassaolo.overlaps(DateRange(poimintapvm, loppupvm)))
        )
    # Import *all* buildings, also those without inhabitants, owners and/or addresses
    rows = session.execute(
        select(Rakennus, RakennuksenVanhimmat, RakennuksenOmistajat, Osoite)
        .join_from(Rakennus, RakennuksenVanhimmat, isouter=True)
        .join_from(Rakennus, RakennuksenOmistajat, isouter=True)
        .join(Rakennus.osoite_collection, isouter=True)
        .filter(~Rakennus.id.in_(rakennus_id_with_current_kohde))
        # Do not import rakennus that have been removed from DVV data
        .filter(
            or_(
                Rakennus.kaytostapoisto_pvm.is_(None),
                Rakennus.kaytostapoisto_pvm > poimintapvm,
            )
        )
    ).all()
    rakennustiedot_by_id = dict()
    for row in rows:
        rakennus_id = row[0].id
        if rakennus_id not in rakennustiedot_by_id:
            rakennustiedot_by_id[rakennus_id] = (row[0], set(), set(), set())
        if row[1]:
            rakennustiedot_by_id[rakennus_id][1].add(row[1])
        if row[2]:
            rakennustiedot_by_id[rakennus_id][2].add(row[2])
        if row[3]:
            rakennustiedot_by_id[rakennus_id][3].add(row[3])
    # Freeze the rakennustiedot sets in the end.
    # This way, rakennustiedot may be used in sets etc.
    for id, (rakennus, vanhimmat, omistajat, osoitteet) in rakennustiedot_by_id.items():
        rakennustiedot_by_id[id] = (
            rakennus,
            frozenset(vanhimmat),
            frozenset(omistajat),
            frozenset(osoitteet),
        )
    return rakennustiedot_by_id


def _is_sauna(rakennus: "Rakennus"):
    """
    Saunas are a bit of a special case. They are significant buildings *only* if they
    are not added as auxiliary buildings. Therefore, we have to check if a building
    is a sauna *in addition* to checking significance.

    This is because saunas are often missing owner data (and they should belong to the
    main building), but we also want saunas to be created as separate kohde if no
    main building was found.
    """
    return (
        rakennus.rakennuksenkayttotarkoitus
        == codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.SAUNA]
    )


def _is_significant_building(rakennustiedot: "Rakennustiedot"):
    """
    Determines which buildings should always be contained in a kohde, either separate
    or combined with other buildings. This function requires rakennustiedot, because
    any building with a permanent inhabitant (no matter what the building type) is
    always significant. Also buildings without owners may be significant.

    Saunas should always be contained in a kohde, but they should never have a separate
    kohde if they can be joined to other buildings on the same kiinteistö. Their
    significance must therefore be checked separately depending on the context.
    """
    rakennus = rakennustiedot[0]
    asukkaat = rakennustiedot[1]
    return asukkaat or rakennus.rakennuksenkayttotarkoitus in (
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.YKSITTAISTALO
        ],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.PARITALO],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.MUU_PIENTALO
        ],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.RIVITALO],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.LUHTITALO],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.KETJUTALO],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.KERROSTALO
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.VAPAA_AJANASUNTO
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.MUU_ASUNTOLA
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.VANHAINKOTI
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.LASTENKOTI
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.KEHITYSVAMMAHOITOLAITOS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.MUU_HUOLTOLAITOS
        ],
        codes.rakennuksenkayttotarkoitukset[RakennuksenKayttotarkoitusTyyppi.PAIVAKOTI],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.MUU_SOSIAALITOIMEN_RAKENNUS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.YLEISSIVISTAVA_OPPILAITOS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.AMMATILLINEN_OPPILAITOS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.KORKEAKOULU
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.TUTKIMUSLAITOS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.OPETUSRAKENNUS
        ],
        codes.rakennuksenkayttotarkoitukset[
            RakennuksenKayttotarkoitusTyyppi.MUU_OPETUSRAKENNUS
        ],
    )


def _cluster_rakennustiedot(
    rakennustiedot_to_cluster: "Set[Rakennustiedot]",
    distance_limit: int,
    existing_cluster: "Optional[Set[Rakennustiedot]]" = None,
) -> "List[Set[Rakennustiedot]]":
    """
    Return rakennustiedot clustered so that all buildings within distance_limit
    to any other building are joined to the same cluster.

    Optionally, we may specify an existing cluster that may not be split. This is
    meant for cases in which we know the buildings should belong together (e.g.
    because of perusmaksu, or grouping additional buildings to cluster).
    """
    clusters = []
    # start cluster from first remaining building (or existing cluster, if
    # provided).
    cluster = existing_cluster.copy() if existing_cluster else None
    while rakennustiedot_to_cluster:
        if not cluster:
            cluster = set([rakennustiedot_to_cluster.pop()])

        other_rakennustiedot_to_cluster = rakennustiedot_to_cluster.copy()
        while other_rakennustiedot_to_cluster:
            for other_rakennustiedot in other_rakennustiedot_to_cluster:
                if (
                    minimum_distance_of_buildings(
                        [rakennustiedot[0] for rakennustiedot in cluster]
                        + [other_rakennustiedot[0]]
                    )
                    < distance_limit
                ):
                    # found another building. add found building to cluster
                    # and start the loop anew.
                    cluster.add(other_rakennustiedot)
                    break
            else:
                # cluster finished! other_rakennustiedot_to_cluster did not
                # contain any buildings within set distance of cluster.
                break
            # found another building. remove found building from set to process
            # and start the loop anew.
            other_rakennustiedot_to_cluster.remove(other_rakennustiedot)
        # cluster finished! removing clustered buildings and starting the loop anew.
        clusters.append(cluster)
        rakennustiedot_to_cluster -= cluster
        cluster = None
    return clusters


def _add_auxiliary_buildings(
    dvv_rakennustiedot: "Dict[int, Rakennustiedot]",
    building_sets: "List[Set[Rakennustiedot]]",
) -> "List[Set[Rakennustiedot]]":
    """
    For each building set, adds any auxiliary building(s) on the same kiinteistö(t)
    having the same cluster, owner(s) (or missing owners!) and address(es) as any of
    the existing buildings.
    """
    dvv_rakennustiedot_by_kiinteistotunnus: "dict[int, Set[Rakennustiedot]]" = (
        defaultdict(set)
    )
    for rakennus, vanhimmat, omistajat, osoitteet in dvv_rakennustiedot.values():
        dvv_rakennustiedot_by_kiinteistotunnus[rakennus.kiinteistotunnus].add(
            (rakennus, vanhimmat, omistajat, osoitteet)
        )
    sets_to_return: "List[Set[Rakennustiedot]]" = []
    for building_set in building_sets:
        set_to_return = building_set.copy()
        print(
            f"- Etsitään lisärakennuksia rakennuksille {[tiedot[0].prt for tiedot in building_set]} -"
        )
        for building, elders, owners, addresses in building_set:
            kiinteistotunnus = building.kiinteistotunnus
            owner_ids = {owner.osapuoli_id for owner in owners}
            # Some buildings have missing kiinteistötunnus. Cannot join them =)
            if kiinteistotunnus:
                kiinteiston_lisarakennukset: "Set[Rakennustiedot]" = {
                    rakennustiedot
                    for rakennustiedot in dvv_rakennustiedot_by_kiinteistotunnus[  # noqa
                        kiinteistotunnus
                    ]
                    # Saunas will also be added here if they are close enough. On
                    # the *same* cluster, they are not significant. On the *same*
                    # cluster, they should be considered auxiliary, no matter the
                    # owner. They often have missing owner data.
                    if not _is_significant_building(rakennustiedot)
                }
                # Only add those auxiliary buildings that are close enough to the
                # main cluster. Discard the remaining ones, they are not significant
                # and are too far to be added. We must cluster again, because saunas
                # may be linked to the main building via other saunas. Therefore,
                # just calculating distance to the building set would result in some
                # saunas being left out!
                clustered_lisarakennukset = _cluster_rakennustiedot(
                    kiinteiston_lisarakennukset, DISTANCE_LIMIT, building_set
                )
                common_cluster = clustered_lisarakennukset[0] if clustered_lisarakennukset else set()

                if common_cluster:
                    print(
                        f"Samalla kiinteistöllä lähekkäin rakennukset: {[tiedot[0].prt for tiedot in common_cluster]}"
                    )

                # only add lisärakennukset having at least one common owner and address,
                # or no owner and common address
                rakennustiedot_to_add = {
                    (rakennus, vanhimmat, omistajat, osoitteet)
                    for rakennus, vanhimmat, omistajat, osoitteet in common_cluster
                    if (
                        not omistajat
                        or (  # owner ids must match if present!
                            {omistaja.osapuoli_id for omistaja in omistajat} & owner_ids
                        )
                        # saunas are the only buildings whose owners do not matter
                        or _is_sauna(rakennus)
                    )
                    # To get all saunas included in kohteet, we must allow saunas to be added
                    # without checking their address. Otherwise, saunas on large kiinteistöt
                    # might be left out of kohde.
                    and (_is_sauna(rakennus) or _match_addresses(osoitteet, addresses))
                }
                # If the addresses or owners differ, remaining objects on the same
                # kiinteistö will create separate kohteet in the last import stage,
                # if they are significant
                if common_cluster:
                    print(
                        f"Näistä saunoja TAI samalla osoitteella ja omistajalla {[tiedot[0].prt for tiedot in rakennustiedot_to_add]}"
                    )

                # Only add auxiliary buildings to one kohde, remove them from
                # future processing. They are mapped to the first owner and address
                # found.
                dvv_rakennustiedot_by_kiinteistotunnus[
                    kiinteistotunnus
                ] -= rakennustiedot_to_add
                set_to_return |= rakennustiedot_to_add
        print(
            f"Lisärakennusten lisäämisen jälkeen: {[tiedot[0].prt for tiedot in set_to_return]}"
        )
        sets_to_return.append(set_to_return)
    return sets_to_return


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


def parse_alkupvm_for_kohde(
    session: "Session",
    rakennus_ids: "List[int]",
    poimintapvm: "Optional[datetime.date]",
):
    latest_omistaja_change = (
        session.query(sqlalchemyFunc.max(RakennuksenOmistajat.omistuksen_alkupvm))
        .filter(RakennuksenOmistajat.rakennus_id.in_(rakennus_ids))
        .scalar()
    )
    latest_vanhin_change = session.query(
        sqlalchemyFunc.max(RakennuksenVanhimmat.alkupvm)
    ).filter(RakennuksenVanhimmat.rakennus_id.in_(rakennus_ids)).scalar()

    if latest_omistaja_change is None and latest_vanhin_change is None:
        return poimintapvm
    if latest_omistaja_change is None and latest_vanhin_change is not None:
        return latest_vanhin_change
    if latest_vanhin_change is None and latest_omistaja_change is not None:
        return latest_omistaja_change
    return max(latest_omistaja_change, latest_vanhin_change)


def create_new_kohde_from_buildings(
    session: "Session",
    rakennus_ids: "List[int]",
    asukkaat: "Set[Osapuoli]",
    omistajat: "Set[Osapuoli]",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Create combined kohde for the given list of building ids. Omistaja or asukas
    will be used for kohde name. Omistajat will be added as yhteystiedot.
    """
    if omistajat:
        # prefer companies over private owners when naming combined objects
        asoy_asiakkaat = {osapuoli for osapuoli in omistajat if is_asoy(osapuoli.nimi)}
        company_asiakkaat = {
            osapuoli for osapuoli in omistajat if is_company(osapuoli.nimi)
        }
        yhteiso_asiakkaat = {
            osapuoli for osapuoli in omistajat if is_yhteiso(osapuoli.nimi)
        }
        if asoy_asiakkaat:
            asiakas = min(asoy_asiakkaat, key=lambda x: x.nimi)
        elif company_asiakkaat:
            asiakas = min(company_asiakkaat, key=lambda x: x.nimi)
        elif yhteiso_asiakkaat:
            asiakas = min(yhteiso_asiakkaat, key=lambda x: x.nimi)
        elif asukkaat:
            asiakas = min(asukkaat, key=lambda x: x.nimi)
        else:
            asiakas = min(omistajat, key=lambda x: x.nimi)
    else:
        if asukkaat:
            asiakas = min(asukkaat, key=lambda x: x.nimi)
        else:
            asiakas = None
    if asiakas:
        kohde_display_name = form_display_name(
            Yhteystieto(
                asiakas.nimi,
                asiakas.katuosoite,
                asiakas.ytunnus,
                asiakas.henkilotunnus,
            )
        )
    else:
        kohde_display_name = "Tuntematon"
    alkupvm = parse_alkupvm_for_kohde(session, rakennus_ids, poimintapvm)
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
    # save all asukkaat as asiakas
    for osapuoli in asukkaat:
        asiakas = KohteenOsapuolet(
            osapuoli_id=osapuoli.id,
            kohde_id=kohde.id,
            osapuolenrooli=codes.osapuolenroolit[OsapuolenrooliTyyppi.VANHIN_ASUKAS]
        )
        session.add(asiakas)
    # save all omistajat as yhteystiedot, even if they also live in the building.
    # This is needed so the kohde can be identified by owner at later import.
    for osapuoli in omistajat:
        yhteystieto = KohteenOsapuolet(
            osapuoli_id=osapuoli.id,
            kohde_id=kohde.id,
            osapuolenrooli=codes.osapuolenroolit[
                OsapuolenrooliTyyppi.OMISTAJA
            ],
        )
        session.add(yhteystieto)
    return kohde


def old_kohde_id_for_buildings(
    session: "Session", rakennus_ids: "List[int]", poimintapvm: "datetime.date"
):
    kohde_query = (
        select(Kohde.id)
        .join(KohteenRakennukset)
        .filter(
            KohteenRakennukset.rakennus_id.in_(rakennus_ids),
            Kohde.loppupvm == poimintapvm - timedelta(days=1),
        )
    )
    old_kohde_id = session.execute(kohde_query).scalar()
    return old_kohde_id


def set_old_kohde_loppupvm(session: "Session", kohde_id: int, loppupvm: "datetime.date"):
    session.execute(
        update(Kohde)
        .where(Kohde.id == kohde_id)
        .values(loppupvm=loppupvm)
    )
    session.commit()


def update_or_create_kohde_from_buildings(
    session: "Session",
    dvv_rakennustiedot: "Dict[int, Rakennustiedot]",
    rakennukset: "Set[Rakennustiedot]",
    asukkaat: "Set[Osapuoli]",
    omistajat: "Set[Osapuoli]",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Check the database for existing kohde with the same inhabitants, owners and
    significant building_ids. Create new kohde if not found.

    Input building ids, as well as existing database building ids, are checked
    so that non-significant building ids are discarded.

    Therefore, the existing kohde may have more *or* less auxiliary buildings than the
    incoming kohde. In that case, we will use existing kohde, but add or remove the
    buildings that have changed.

    In case of paritalo, we don't know which owner owned which part of the building.
    Therefore, we will have to create new kohteet for both halves when somebody sells
    their half.
    """
    rakennus_ids = {rakennustiedot[0].id for rakennustiedot in rakennukset}
    # Incoming building list may have extra auxiliary buildings
    significant_buildings = set(
        filter(lambda x: _is_significant_building(x), rakennukset)
    )
    significant_building_ids = {
        rakennustiedot[0].id for rakennustiedot in significant_buildings
    }
    asukas_ids = {osapuoli.id for osapuoli in asukkaat}
    omistaja_ids = {osapuoli.id for osapuoli in omistajat}
    osapuoli_ids = asukas_ids | omistaja_ids
    print(
        f"Etsitään kohdetta, jossa rakennukset {rakennus_ids}, asukkaat {asukas_ids} ja omistajat {omistaja_ids}"
    )
    # List all kohde buildings here, check significance later. We may need to add and remove
    # auxiliary buildings if kohde is found.
    kohde_query = (
        select(Kohde, KohteenRakennukset, KohteenOsapuolet)
        .join(KohteenRakennukset)
        .join(KohteenOsapuolet)
        .filter(KohteenRakennukset.rakennus_id.in_(rakennus_ids))
        .filter(KohteenOsapuolet.osapuoli_id.in_(osapuoli_ids))
    )
    potential_kohdetiedot: "List[(Kohde, KohteenRakennukset, KohteenOsapuolet)]" = (
        session.execute(kohde_query).all()
    )

    # List significant buildings, auxiliary buildings, inhabitants and owners for each kohde
    kohdetiedot_by_kohde: "dict[int, Kohdetiedot]" = {}
    for kohde, rakennus, osapuoli in potential_kohdetiedot:
        if kohde.id not in kohdetiedot_by_kohde:
            kohdetiedot_by_kohde[kohde.id] = (kohde, set(), set(), set(), set())
        # Separate significant and auxiliary buildings
        if _is_significant_building(dvv_rakennustiedot[rakennus.rakennus_id]):
            kohdetiedot_by_kohde[kohde.id][1].add(rakennus)
        else:
            kohdetiedot_by_kohde[kohde.id][2].add(rakennus)
        # separate asukkaat from omistajat
        if (
            osapuoli.osapuolenrooli_id
            == codes.osapuolenroolit[OsapuolenrooliTyyppi.VANHIN_ASUKAS].id
        ):
            kohdetiedot_by_kohde[kohde.id][3].add(osapuoli)
        if (
            osapuoli.osapuolenrooli_id
            == codes.osapuolenroolit[OsapuolenrooliTyyppi.OMISTAJA].id
        ):
            kohdetiedot_by_kohde[kohde.id][4].add(osapuoli)
    for kohdetiedot in kohdetiedot_by_kohde.values():
        kohde = kohdetiedot[0]
        kohteen_rakennus_ids = set(rakennus.rakennus_id for rakennus in kohdetiedot[1])
        # If kohde has no significant buildings, it is a lonely sauna or a group of
        # forlorn, lonely saunas. In this case, we just compare the owners of the
        # kohde which had the same rakennus_id(s).
        kohteen_lisarakennus_ids = set(
            rakennus.rakennus_id for rakennus in kohdetiedot[2]
        )
        kohteen_lisarakennukset = set(rakennus for rakennus in kohdetiedot[2])
        kohteen_asukas_ids = set(osapuoli.osapuoli_id for osapuoli in kohdetiedot[3])
        kohteen_omistaja_ids = set(osapuoli.osapuoli_id for osapuoli in kohdetiedot[4])
        print("Tutkitaan kohteen merkitseviä rakennuksia:")
        # use kohde if rakennukset and osapuolet are same
        if (
            kohteen_rakennus_ids == significant_building_ids
            and kohteen_asukas_ids == asukas_ids
            and kohteen_omistaja_ids == omistaja_ids
        ):
            print("Rakennukset, asukkaat ja omistajat samat!")
            break
        # discard kohde if rakennukset are missing
        if significant_building_ids < kohteen_rakennus_ids:
            print("Merkitseviä rakennuksia puuttuu")
            continue
        # use kohde if rakennukset are added
        if (
            kohteen_rakennus_ids < significant_building_ids
            and kohteen_asukas_ids == asukas_ids
            and kohteen_omistaja_ids == omistaja_ids
        ):
            print("Merkitsevät rakennukset, asukkaat ja omistajat samat!")
            break
        # discard kohde if owners or inhabitants are different
        if kohteen_asukas_ids != asukas_ids or kohteen_omistaja_ids != omistaja_ids:
            print("Asukkaat tai omistajat eri")
            continue
        # All combinations are checked above.
    else:
        print("Sopivaa kohdetta ei löydy, luodaan uusi kohde.")
        new_kohde = create_new_kohde_from_buildings(
            session, rakennus_ids, asukkaat, omistajat, poimintapvm, loppupvm
        )
        if new_kohde and poimintapvm:
            old_kohde_id = old_kohde_id_for_buildings(session, rakennus_ids, poimintapvm)
            if old_kohde_id:
                print(f"Löytyi päättyvä kohde {old_kohde_id}, asetetaan loppupäivämäärä.")
                set_old_kohde_loppupvm(session, old_kohde_id, new_kohde.alkupvm - timedelta(days=1))
        return new_kohde

    # Return existing kohde when found
    print("Olemassaoleva kohde löytynyt.")
    for kohteen_rakennus in kohteen_lisarakennukset:
        print("Tarkistetaan kohteen lisärakennus")
        print(kohteen_rakennus.rakennus_id)
        if kohteen_rakennus.rakennus_id not in rakennus_ids:
            print("Ei löydy enää, poistetaan kohteelta")
            session.delete(kohteen_rakennus)
    # Add new auxiliary buildings
    for rakennus_id in rakennus_ids - significant_building_ids:
        print("Tarkistetaan lisärakennus")
        print(rakennus_id)
        if rakennus_id not in kohteen_lisarakennus_ids:
            print("Ei löydy vielä, lisätään kohteelle")
            kohteen_rakennus = KohteenRakennukset(
                rakennus_id=rakennus_id, kohde_id=kohde.id
            )
            session.add(kohteen_rakennus)

    # Update kohde to be valid for the whole import period
    if not poimintapvm or (kohde.alkupvm and poimintapvm < kohde.alkupvm):
        kohde.alkupvm = poimintapvm

    # Reset end date if using poimintapvm (kohde is still active)
    if poimintapvm and kohde.loppupvm == poimintapvm - timedelta(days=1):
        kohde.loppupvm = None

    return kohde


def get_or_create_kohteet_from_vanhimmat(
    session: "Session",
    ids: "Select",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Create one kohde for each RakennuksenVanhimmat osapuoli id provided by
    the select query.
    """
    dvv_rakennustiedot = get_dvv_rakennustiedot_without_kohde(
        session, poimintapvm, loppupvm
    )
    # iterate vanhimmat to create kohde with the right name, client and building
    vanhimmat_osapuolet_query = (
        select(RakennuksenVanhimmat, Osapuoli)
        .join(RakennuksenVanhimmat.osapuoli)
        .filter(RakennuksenVanhimmat.osapuoli_id.in_(ids))
    )
    vanhimmat_osapuolet = session.execute(vanhimmat_osapuolet_query).all()
    print(
        f"Löydetty {len(vanhimmat_osapuolet)} vanhinta asukasta ilman voimassaolevaa kohdetta"
    )
    kohteet = []
    for (vanhin, osapuoli) in vanhimmat_osapuolet:
        # We have to check if we are interested in just this rakennus of vanhin
        if vanhin.rakennus_id in dvv_rakennustiedot:
            # The oldest inhabitant is the customer. Also save owners as backup
            # contacts.
            omistajat_query = (
                select(RakennuksenOmistajat, Osapuoli)
                .join(RakennuksenOmistajat.osapuoli)
                .where(RakennuksenOmistajat.rakennus_id == vanhin.rakennus_id)
            )
            omistajat = {row[1] for row in session.execute(omistajat_query).all()}
            # The correct kohde is found by checking the inhabitant in each half. In case
            # of paritalo, we don't know which owner owned which part of the building.
            # Therefore, we will have to create new kohteet for both halves when somebody
            # sells their half.
            kohde = update_or_create_kohde_from_buildings(
                session,
                dvv_rakennustiedot,
                {dvv_rakennustiedot[vanhin.rakennus_id]},
                {osapuoli},
                omistajat,
                poimintapvm,
                loppupvm,
            )
            kohteet.append(kohde)
    return kohteet


def get_or_create_kohteet_from_rakennustiedot(
    session: "Session",
    dvv_rakennustiedot: "Dict[int, Rakennustiedot]",
    building_sets: "List[Set[Rakennustiedot]]",
    owners_by_rakennus_id: "DefaultDict[int, Set[Osapuoli]]",
    inhabitants_by_rakennus_id: "DefaultDict[int, Set[Osapuoli]]",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Create separate kohde for each building set provided in building sets, adding
    auxiliary buildings to each kohde.
    """
    # merge empty buildings with same address and owner on kiinteistö(t) to the main
    # building(s), if they are close enough.
    building_sets = _add_auxiliary_buildings(dvv_rakennustiedot, building_sets)
    kohteet = []
    for building_set in building_sets:
        owners = set().union(
            *[
                owners_by_rakennus_id[rakennustiedot[0].id]
                for rakennustiedot in building_set
            ]
        )
        inhabitants = set().union(
            *[
                inhabitants_by_rakennus_id[rakennustiedot[0].id]
                for rakennustiedot in building_set
            ]
        )

        kohde = update_or_create_kohde_from_buildings(
            session,
            dvv_rakennustiedot,
            building_set,
            inhabitants,
            owners,
            poimintapvm,
            loppupvm,
        )
        kohteet.append(kohde)
    return kohteet


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
            if _match_address(osoite, address):
                print("Yhteinen osoite löytyi.")
                return True
    print("Ei yhteistä osoitetta.")
    return False


def get_or_create_kohteet_from_kiinteistot(
    session: "Session",
    kiinteistotunnukset: "Select",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Create at least one kohde from each kiinteistotunnus provided by the select query,
    if the kiinteistotunnus has any buildings without existing kohde for the provided
    time period. Only create separate kohde from *significant* buildings.
    *Non-significant* buildings may be added to kohde or left out, depending on owner,
    address and distance.

    1) First, separate buildings in each kiinteistö into clusters depending on distance.
    Some kiinteistöt are huge and may contain inhabited buildings/saunas far from
    each other. Their addresses may be wrong, so we have to separate by distance.

    2) If the same cluster has significant buildings with multiple owners,
    first kohde will contain all buildings owned by the owner with the most buildings.
    Then, create second kohde from remaining significant buildings owned by the owner
    with the second largest number of buildings, etc., until all significant buildings
    have kohde named after their owner. If there are significant buildings without
    any owners, they will get their own kohde.

    3) Finally, if the buildings have different addresses, separate buildings to
    those having the same address.
    """
    print("Ladataan kiinteistötunnukset...")
    kiinteistotunnukset = [
        result[0] for result in session.execute(kiinteistotunnukset).all()
    ]
    print(f"{len(kiinteistotunnukset)} tuotavaa kiinteistötunnusta löydetty.")
    print("Ladataan rakennukset...")
    # Fastest to load everything to memory first.
    # Cannot filter buildings to load here by type. *Any* buildings that have an
    # inhabitant should be imported wholesale.
    #
    # We must only filter out buildings with existing kohde here, since the same
    # kiinteistö might have buildings both with and without kohde, in case some
    # buildings have been imported in previous steps.
    dvv_rakennustiedot = get_dvv_rakennustiedot_without_kohde(
        session, poimintapvm, loppupvm
    )
    print(
        f"Löydetty {len(dvv_rakennustiedot)} DVV-rakennusta ilman voimassaolevaa kohdetta"
    )

    rakennustiedot_by_kiinteistotunnus: Dict[int, Set[Rakennustiedot]] = defaultdict(
        set
    )
    for rakennus, vanhimmat, omistajat, osoitteet in dvv_rakennustiedot.values():
        rakennustiedot_by_kiinteistotunnus[rakennus.kiinteistotunnus].add(
            (rakennus, vanhimmat, omistajat, osoitteet)
        )

    print("Ladataan omistajat...")
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

    print("Ladataan vanhimmat asukkaat...")
    rakennus_inhabitants = session.execute(
        select(RakennuksenVanhimmat.rakennus_id, Osapuoli).join(
            Osapuoli, RakennuksenVanhimmat.osapuoli_id == Osapuoli.id
        ).where(RakennuksenVanhimmat.loppupvm.is_(None))
    ).all()
    inhabitants_by_rakennus_id = defaultdict(set)
    for (rakennus_id, inhabitant) in rakennus_inhabitants:
        inhabitants_by_rakennus_id[rakennus_id].add(inhabitant)

    print("Ladataan osoitteet...")
    rakennus_addresses = session.execute(
        select(Rakennus.id, Osoite).join(Osoite, Rakennus.id == Osoite.rakennus_id)
    ).all()
    addresses_by_rakennus_id: "Dict[int, Set[Osoite]]" = defaultdict(set)
    for (rakennus_id, address) in rakennus_addresses:
        addresses_by_rakennus_id[rakennus_id].add(address)

    building_sets: List[Set[Rakennustiedot]] = []
    print("Käydään läpi kiinteistötunnukset...")
    for kiinteistotunnus in kiinteistotunnukset:
        print(f"--- Kiinteistö {kiinteistotunnus} ---")
        # NOTE: kiinteistotunnus may also be None. In this case, all buildings without
        # kiinteistotunnus will be imported separated by distance, owner (if present)
        # and address only.
        rakennustiedot_to_add = rakennustiedot_by_kiinteistotunnus[kiinteistotunnus]

        # 1) Split buildings to clusters *before* removing saunas and other auxiliary
        # buildings. We want sauna clusters to form their own kohteet.
        #
        # Area is clearly not a good measure of a cluster. We want to get rid of
        # narrow strip kiinteistöt from before isojako, while retaining large
        # local clusters of buildings. Therefore, we must look at largest minimum
        # distance between closest buildings.
        clustered_rakennustiedot = _cluster_rakennustiedot(
            rakennustiedot_to_add, DISTANCE_LIMIT
        )

        for rakennustiedot_by_cluster in clustered_rakennustiedot:
            # Only use significant buildings to create new kohde.
            ids_by_cluster = {
                rakennustiedot[0].id
                for rakennustiedot in rakennustiedot_by_cluster
                if _is_significant_building(rakennustiedot)
            }
            sauna_ids_by_cluster = {
                rakennustiedot[0].id
                for rakennustiedot in rakennustiedot_by_cluster
                if _is_sauna(rakennustiedot[0])
            }
            # Saunas are a special case. On each kiinteistö, their owners/addresses should
            # not matter if there are more significant buildings. If kiinteistotunnus is
            # not known, we will have to create separate kohteet for saunas. Let's add
            # saunas to other kohteet in any other case.
            if not ids_by_cluster or not kiinteistotunnus:
                ids_by_cluster |= sauna_ids_by_cluster
            print("Löydetty rakennusryhmä:")
            print(ids_by_cluster)

            # 2) Split buildings by owner
            while ids_by_cluster:
                owners = set().union(
                    *[dvv_rakennustiedot[id][2] for id in ids_by_cluster]
                )
                # Only use significant buildings. Add auxiliary buildings to each building
                # set that is found.
                building_ids_by_owner = {
                    owner: ids_by_cluster & rakennus_ids_by_owner_id[owner.osapuoli_id]
                    for owner in owners
                }
                # Start from the owner with the most buildings
                owners_by_buildings = sorted(
                    building_ids_by_owner.items(),
                    key=lambda item: len(item[1]),
                    reverse=True,
                )
                if owners_by_buildings:
                    first_owner, building_ids_owned = owners_by_buildings[0]
                else:
                    # We have no owners left! Remaining are significant buildings with
                    # missing owners. Let's just add them all together.
                    ids_without_owner = ids_by_cluster - set(
                        owners_by_rakennus_id.keys()
                    )
                    building_ids_owned = ids_without_owner

                print("Saman omistajan rakennukset:")
                print(building_ids_owned)

                # 3) split buildings further by address
                while building_ids_owned:
                    # All significant buildings at same address should be together.
                    building_ids_by_address = defaultdict(set)
                    for building in building_ids_owned:
                        for address in addresses_by_rakennus_id[building]:
                            street = address.katu_id
                            number = address.osoitenumero
                            building_ids_by_address[(street, number)].add(building)
                    # Start from the address with the most buildings
                    addresses_by_buildings = sorted(
                        building_ids_by_address.items(),
                        key=lambda item: len(item[1]),
                        reverse=True,
                    )
                    (street, number), building_ids_at_address = addresses_by_buildings[
                        0
                    ]
                    print(f"Kadun {street} osoitteessa {number}:")
                    print(f"Yhdistetään rakennukset {building_ids_at_address}")
                    rakennustiedot_at_address = {
                        dvv_rakennustiedot[id] for id in building_ids_at_address
                    }
                    building_sets.append(rakennustiedot_at_address)
                    # do not import the building again from second address
                    building_ids_owned -= building_ids_at_address
                    ids_by_cluster -= building_ids_at_address

    print(" ")
    print(
        "--- Kiinteistötunnukset käyty läpi. Lisätään piharakennukset/saunat ja luodaan kohteet. ---"
    )
    kohteet = get_or_create_kohteet_from_rakennustiedot(
        session,
        dvv_rakennustiedot,
        building_sets,
        owners_by_rakennus_id,
        inhabitants_by_rakennus_id,
        poimintapvm,
        loppupvm,
    )
    return kohteet


def get_or_create_single_asunto_kohteet(
    session: "Session",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
) -> "List(Kohde)":
    """
    Get or create kohteet from all yksittäistalot that do not have kohde on the
    specified time interval. Also consider yksittäistalot that do not have an
    inhabitant.

    If there are multiple inhabited buildings on a kiinteistö, it will not be imported
    here.
    """
    # Do not import any rakennus with existing kohteet
    if loppupvm is None:
        rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .filter(
                Rakennus.rakennuksenkayttotarkoitus
                == codes.rakennuksenkayttotarkoitukset[
                    RakennuksenKayttotarkoitusTyyppi.YKSITTAISTALO
                ]
            )
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(poimintapvm == Kohde.alkupvm)
        )
    else:
        rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .filter(
                Rakennus.rakennuksenkayttotarkoitus
                == codes.rakennuksenkayttotarkoitukset[
                    RakennuksenKayttotarkoitusTyyppi.YKSITTAISTALO
                ]
            )
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(Kohde.voimassaolo.overlaps(DateRange(poimintapvm, loppupvm)))
        )
    rakennus_id_without_kohde = (
        select(Rakennus.id)
        .filter(
            Rakennus.rakennuksenkayttotarkoitus
            == codes.rakennuksenkayttotarkoitukset[
                RakennuksenKayttotarkoitusTyyppi.YKSITTAISTALO
            ]
        )
        .filter(~Rakennus.id.in_(rakennus_id_with_current_kohde))
        # Do not import rakennus that have been removed from DVV data
        .filter(
            or_(
                Rakennus.kaytostapoisto_pvm.is_(None),
                Rakennus.kaytostapoisto_pvm > poimintapvm,
            )
        )
    )
    single_asunto_kiinteistotunnus = (
        select(
            Rakennus.kiinteistotunnus,
            sqlalchemyFunc.count(Rakennus.id),
        )
        .filter(Rakennus.id.in_(rakennus_id_without_kohde))
        .group_by(Rakennus.kiinteistotunnus)
        .having(sqlalchemyFunc.count(Rakennus.id) == 1)
    )

    print(" ")
    print("----- LUODAAN YKSITTÄISTALOKOHTEET -----")
    return get_or_create_kohteet_from_kiinteistot(
        session, single_asunto_kiinteistotunnus, poimintapvm, loppupvm
    )


def get_or_create_paritalo_kohteet(
    session: "Session",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
) -> "List(Kohde)":
    """
    Create kohteet from all paritalo buildings that do not have kohde for the
    specified date range.
    """
    # Each paritalo can belong to a maximum of two kohde. Create both in this
    # step. If there is only one inhabitant (i.e. another flat is empty, flats
    # are combined, etc.), the building already has one kohde from previous
    # step and needs not be imported here.
    if loppupvm is None:
        paritalo_rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .filter(
                Rakennus.rakennuksenkayttotarkoitus
                == codes.rakennuksenkayttotarkoitukset[
                    RakennuksenKayttotarkoitusTyyppi.PARITALO
                ]
            )
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(poimintapvm == Kohde.alkupvm)
        )
    else:
        paritalo_rakennus_id_with_current_kohde = (
            select(Rakennus.id)
            .filter(
                Rakennus.rakennuksenkayttotarkoitus
                == codes.rakennuksenkayttotarkoitukset[
                    RakennuksenKayttotarkoitusTyyppi.PARITALO
                ]
            )
            .join(KohteenRakennukset)
            .join(Kohde)
            .filter(Kohde.voimassaolo.overlaps(DateRange(poimintapvm, loppupvm)))
        )
    paritalo_rakennus_id_without_kohde = (
        select(Rakennus.id).filter(
            Rakennus.rakennuksenkayttotarkoitus
            == codes.rakennuksenkayttotarkoitukset[
                RakennuksenKayttotarkoitusTyyppi.PARITALO
            ]
        )
        # do not import any rakennus with existing kohteet
        .filter(~Rakennus.id.in_(paritalo_rakennus_id_with_current_kohde))
        # Do not import rakennus that have been removed from DVV data
        .filter(
            or_(
                Rakennus.kaytostapoisto_pvm.is_(None),
                Rakennus.kaytostapoisto_pvm > poimintapvm,
            )
        )
    )
    vanhimmat_ids = select(RakennuksenVanhimmat.osapuoli_id).filter(
        RakennuksenVanhimmat.rakennus_id.in_(paritalo_rakennus_id_without_kohde)
    )
    print(" ")
    print("----- CREATING PARITALOKOHTEET ----")
    return get_or_create_kohteet_from_vanhimmat(
        session, vanhimmat_ids, poimintapvm, loppupvm
    )


def get_or_create_multiple_and_uninhabited_kohteet(
    session: "Session",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
) -> "List(Kohde)":
    """
    Create kohteet from all kiinteistötunnus that have buildings without kohde for the
    specified date range.
    """

    rakennus_id_with_current_kohde = (
        select(Rakennus.id)
        .join(KohteenRakennukset)
        .join(Kohde)
        .filter(Kohde.voimassaolo.overlaps(DateRange(poimintapvm, loppupvm)))
    )
    kiinteistotunnus_without_kohde = (
        select(Rakennus.kiinteistotunnus)
        .filter(~Rakennus.id.in_(rakennus_id_with_current_kohde))
        # Do not import rakennus that have been removed from DVV data
        .filter(
            or_(
                Rakennus.kaytostapoisto_pvm.is_(None),
                Rakennus.kaytostapoisto_pvm > poimintapvm,
            )
        ).group_by(Rakennus.kiinteistotunnus)
    )
    print(" ")
    print("----- LUODAAN JÄLJELLÄ OLEVAT KOHTEET -----")
    return get_or_create_kohteet_from_kiinteistot(
        session, kiinteistotunnus_without_kohde, poimintapvm, loppupvm
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
    )


def create_perusmaksurekisteri_kohteet(
    session: "Session",
    perusmaksutiedosto: "Path",
    poimintapvm: "Optional[datetime.date]",
    loppupvm: "Optional[datetime.date]",
):
    """
    Create kohteet combining all dvv buildings that have the same asiakasnumero in
    perusmaksurekisteri and have the desired type. No need to import anything from
    perusmaksurekisteri, so we don't want a complete provider for the file.

    Since perusmaksurekisteri may be missing sauna and talousrakennus, add them from
    each kiinteistö, matching owner and address.
    """
    print(" ")
    print("----- LUODAAN PERUSMAKSUKOHTEET -----")
    perusmaksut = load_workbook(filename=perusmaksutiedosto)
    sheet = perusmaksut["Tietopyyntö asiakasrekisteristä"]
    # some asiakasnumero occur multiple times for the same prt
    buildings_to_combine = defaultdict(lambda: {"prt": set()})
    for index, row in enumerate(sheet.values):
        # skip header
        if index == 0:
            continue
        asiakasnumero = str(row[2])
        prt = str(row[3])
        buildings_to_combine[asiakasnumero]["prt"].add(prt)
    print(f"Löydetty {len(buildings_to_combine)} perusmaksuasiakasta")

    print("Ladataan rakennukset...")
    # Fastest to load everything to memory first.
    # Do not import any rakennus with existing kohteet. Kerrostalokohteet
    # are eternal, so they cannot ever be imported again once imported here.
    dvv_rakennustiedot = get_dvv_rakennustiedot_without_kohde(
        session, poimintapvm, loppupvm
    )
    print(
        f"Löydetty {len(dvv_rakennustiedot)} DVV-rakennusta ilman voimassaolevaa kohdetta"
    )

    dvv_rakennustiedot_by_prt: "Dict[int, Rakennustiedot]" = {}
    for rakennus, vanhimmat, omistajat, osoitteet in dvv_rakennustiedot.values():
        dvv_rakennustiedot_by_prt[rakennus.prt] = (
            rakennus,
            vanhimmat,
            omistajat,
            osoitteet,
        )

    print("Käydään läpi perusmaksuasiakkaat...")

    building_sets: List[Set[Rakennustiedot]] = []
    for kohde_datum in buildings_to_combine.values():
        kohde_prt = kohde_datum["prt"]
        print(f"Perusmaksuasiakkaalla PRT {kohde_prt}")
        building_set: "Set[Rakennustiedot]" = set()
        for prt in kohde_prt:
            try:
                rakennus, omistajat, asukkaat, osoitteet = dvv_rakennustiedot_by_prt[
                    prt
                ]
            except KeyError:
                print(f"PRT:tä {prt} ei löydy DVV:stä, ei tuoda kohdetta")
                continue
            # NOTE: optionally, add other buildings to building set if already
            # at least one perusmaksu specific building is found, i.e. set is not empty
            if _should_have_perusmaksu_kohde(rakennus):
                print(f"{rakennus.prt} kuuluu perusmaksukohteelle")
                # add all owners and addresses for each rakennus
                building_set.add((rakennus, omistajat, asukkaat, osoitteet))
        if len(building_set) == 0:
            print("Perusmaksuasiakkaalla ei halutuntyyppisiä rakennuksia")
            continue
        print(f"Yhdistetään rakennukset {[tiedot[0].prt for tiedot in building_set]}")
        building_sets.append(building_set)

    print("Ladataan omistajat...")
    rakennus_owners = session.execute(
        select(RakennuksenOmistajat.rakennus_id, Osapuoli).join(
            Osapuoli, RakennuksenOmistajat.osapuoli_id == Osapuoli.id
        )
    ).all()
    owners_by_rakennus_id = defaultdict(set)
    for (rakennus_id, owner) in rakennus_owners:
        owners_by_rakennus_id[rakennus_id].add(owner)

    kohteet = get_or_create_kohteet_from_rakennustiedot(
        session,
        dvv_rakennustiedot,
        building_sets,
        owners_by_rakennus_id,
        # No need to add asukkaat. Asoy kohteet should not be separated or named after
        # inhabitants, and inhabitants should not be contacted.
        defaultdict(set),
        poimintapvm,
        datetime.date(2100, 1, 1),  # Let's set perusmaksu kohde loppupvm to 01.01.2100.
    )
    return kohteet
