import datetime
import logging
from typing import TYPE_CHECKING

from jkrimporter.model import KimppaSopimus, SopimusTyyppi
from jkrimporter.model import Tyhjennysvali as JkrTyhjennysvali
from jkrimporter.providers.db.models import Keskeytys

from .. import codes
from ..codes import KohdeTyyppi
from ..models import Keraysvaline, Sopimus, Tiedontuottaja, Tyhjennysvali
from .kohde import get_kohde_by_asiakasnumero, get_or_create_pseudokohde

if TYPE_CHECKING:
    from typing import List, Union

    from sqlalchemy.orm import Session

    from jkrimporter.model import Asiakas
    from jkrimporter.model import Keraysvaline as JkrKeraysvaline
    from jkrimporter.model import Keskeytys as JkrKeskeytys
    from jkrimporter.model import TyhjennysSopimus


logger = logging.getLogger(__name__)


def overlap(a, b):
    """Tests if"""
    a1 = a.alkupvm or datetime.date.min
    a2 = a.loppupvm or datetime.date.max
    b1 = b.alkupvm or datetime.date.min
    b2 = b.loppupvm or datetime.date.max

    return a1 <= b2 and b1 <= a2


def merge_alkupvm(db_sopimus, jkr_sopimus):
    if db_sopimus.alkupvm != jkr_sopimus.alkupvm:
        merged_alkupvm = (
            min(db_sopimus.alkupvm, jkr_sopimus.alkupvm)
            if db_sopimus.alkupvm and jkr_sopimus.alkupvm
            else None
        )
        db_sopimus.alkupvm = merged_alkupvm


def merge_loppupvm(db_sopimus, jkr_sopimus):
    if db_sopimus.loppupvm != jkr_sopimus.loppupvm:
        merged_loppupvm = (
            max(db_sopimus.loppupvm, jkr_sopimus.loppupvm)
            if db_sopimus.loppupvm and jkr_sopimus.loppupvm
            else None
        )
        db_sopimus.loppupvm = merged_loppupvm


def create_or_update_sopimus(
    session: "Session",
    kohde,
    tiedontuottaja: Tiedontuottaja,
    jkr_sopimus: "Union[TyhjennysSopimus, KimppaSopimus]",
) -> Sopimus:
    sopimustyyppi = codes.sopimustyypit[jkr_sopimus.sopimustyyppi]

    if jkr_sopimus.jatelaji:
        jatetyyppi = codes.jatetyypit[jkr_sopimus.jatelaji]
        if not jatetyyppi:
            logger.warning(
                f"Skipping sopimus. Jätetyyppi '{jkr_sopimus.jatelaji.value}' unknown"
            )
            return None
    else:
        jatetyyppi = None

    if isinstance(jkr_sopimus, KimppaSopimus):
        if jkr_sopimus.sopimustyyppi == SopimusTyyppi.aluekerayssopimus:
            kimppaisanta = get_or_create_pseudokohde(
                session, "Aluekeräys (Pseudo)", KohdeTyyppi.ALUEKERAYS
            )
        elif jkr_sopimus.sopimustyyppi == SopimusTyyppi.putkikerayssopimus:
            kimppaisanta = get_or_create_pseudokohde(
                session,
                f"{jkr_sopimus.isannan_asiakasnumero.tunnus} (Pseudo)",
                KohdeTyyppi.PUTKIKERAYS,
            )
        else:
            kimppaisanta = get_kohde_by_asiakasnumero(
                session, jkr_sopimus.isannan_asiakasnumero
            )
    else:
        kimppaisanta = None
    print('got jkr sopimus')
    print(jkr_sopimus)

    # TODO: potentially we have to separate sopimukset for the same
    # jatetyyppi even if they overlap. So we might need to check a
    # separate field, i.e. the sopimus id or the customer id, to make
    # sure that we are combining the right sopimus, not different ones?
    db_sopimus = next(
        (
            sopimus
            for sopimus in kohde.sopimus_collection
            if sopimus.sopimustyyppi == sopimustyyppi
            and sopimus.kimppaisanta_kohde == kimppaisanta
            and sopimus.jatetyyppi == jatetyyppi
            and overlap(sopimus, jkr_sopimus)
        ),
        None,
    )
    if db_sopimus:
        print('found db sopimus')
        print(db_sopimus)
        merge_alkupvm(db_sopimus, jkr_sopimus)
        merge_loppupvm(db_sopimus, jkr_sopimus)
        print('updated sopimus')
    else:
        db_sopimus = Sopimus(
            kohde=kohde,
            sopimustyyppi=sopimustyyppi,
            jatetyyppi=jatetyyppi,
            alkupvm=jkr_sopimus.alkupvm,
            loppupvm=jkr_sopimus.loppupvm,
            tiedontuottaja=tiedontuottaja,
            kimppaisanta_kohde=kimppaisanta,
        )
        session.add(db_sopimus)
        print("created new sopimus")

    return db_sopimus


def update_kesteytykset(model, keskeytykset: "List[JkrKeskeytys]"):
    for jkr_keskeytys in keskeytykset:
        db_keskeytys = next(
            (
                keskeytys
                for keskeytys in model.keskeytys_collection
                if keskeytys.alkupvm == jkr_keskeytys.alkupvm
                and keskeytys.loppupvm == jkr_keskeytys.loppupvm
            ),
            None,
        )
        if db_keskeytys:
            db_keskeytys.selite = jkr_keskeytys.selite
        else:
            db_keskeytys = Keskeytys(
                alkupvm=jkr_keskeytys.alkupvm,
                loppupvm=jkr_keskeytys.loppupvm,
                selite=jkr_keskeytys.selite,
            )

            model.keskeytys_collection.append(db_keskeytys)


def update_keraysvalineet(
    db_sopimus,
    keraysvalineet: "List[JkrKeraysvaline]",
    raportointi_loppupvm: datetime.date,
):
    print("updating keraysvaline")
    print(keraysvalineet)
    print(raportointi_loppupvm)
    for keraysvaline in keraysvalineet:
        db_keraysvaline = next(
            (
                keraysvaline
                for db_keraysvaline in db_sopimus.keraysvaline_collection
                if keraysvaline.tilavuus == db_keraysvaline.tilavuus
                and db_keraysvaline.maara == keraysvaline.maara
            ),
            None,
        )
        if db_keraysvaline:
            print("väline in db")
            db_keraysvaline.pvm = raportointi_loppupvm
        else:
            print("creating new väline")
            db_keraysvaline = Keraysvaline(
                pvm=raportointi_loppupvm,
                tilavuus=keraysvaline.tilavuus,
                maara=keraysvaline.maara,
                keraysvalinetyyppi=codes.keraysvalinetyypit.get(
                    keraysvaline.tyyppi, None
                ),
            )

            db_sopimus.keraysvaline_collection.append(db_keraysvaline)


def update_tyhjennysvalit(
    session, asiakas: "Asiakas", db_sopimus, sopimus: "TyhjennysSopimus"
):
    for db_tyhjennysvali in db_sopimus.tyhjennysvali_collection:
        if (
            JkrTyhjennysvali(
                alkuvko=db_tyhjennysvali.alkuvko,
                loppuvko=db_tyhjennysvali.loppuvko,
                tyhjennysvali=db_tyhjennysvali.tyhjennysvali,
            )
            not in sopimus.tyhjennysvalit
        ):
            if db_tyhjennysvali in session.new:
                logger.warning(
                    "Tyhjennysvälit sekaisin asiakkaalla: "
                    f"{asiakas.asiakasnumero.tunnus} ({sopimus.jatelaji.value})"
                )
            else:
                session.delete(db_tyhjennysvali)

    for jkr_tyhjennysvali in sopimus.tyhjennysvalit:
        print('got tyhjennysväli')
        print(jkr_tyhjennysvali)
        exists = any(
            db_tyhjennysvali.alkuvko == jkr_tyhjennysvali.alkuvko
            and db_tyhjennysvali.loppuvko == jkr_tyhjennysvali.loppuvko
            and db_tyhjennysvali.tyhjennysvali == jkr_tyhjennysvali.tyhjennysvali
            for db_tyhjennysvali in db_sopimus.tyhjennysvali_collection
        )
        if not exists:
            db_tyhjennysvali = Tyhjennysvali(
                alkuvko=jkr_tyhjennysvali.alkuvko,
                loppuvko=jkr_tyhjennysvali.loppuvko,
                tyhjennysvali=jkr_tyhjennysvali.tyhjennysvali,
            )

            db_sopimus.tyhjennysvali_collection.append(db_tyhjennysvali)


def update_sopimukset_for_kohde(
    session,
    kohde,
    asiakas: "Asiakas",
    # sopimukset: "List[Union[TyhjennysSopimus, KimppaSopimus]]",
    raportointi_loppupvm,
    urakoitsija: Tiedontuottaja,
):
    for sopimus in asiakas.sopimukset:
        db_sopimus = create_or_update_sopimus(session, kohde, urakoitsija, sopimus)
        if db_sopimus:
            update_kesteytykset(db_sopimus, sopimus.keskeytykset)

            # Why wouldn't we save the data on each sopimus? Yeah it is duplicated.
            # But it is there in the data already, and the qgis users wants to
            # see the data in the form.
            # if not isinstance(sopimus, KimppaSopimus):
            update_keraysvalineet(
                db_sopimus,
                sopimus.keraysvalineet,
                raportointi_loppupvm
                if raportointi_loppupvm
                else asiakas.voimassa.upper,
            )
            update_tyhjennysvalit(session, asiakas, db_sopimus, sopimus)
