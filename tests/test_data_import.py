import subprocess
from datetime import datetime

import pytest
from sqlalchemy import and_, create_engine, distinct, func, or_, select
from sqlalchemy.orm import Session

from jkrimporter import conf
from jkrimporter.providers.db.codes import init_code_objects
from jkrimporter.providers.db.database import json_dumps
from jkrimporter.providers.db.dbprovider import import_dvv_kohteet
from jkrimporter.providers.db.models import (
    Kohde,
    KohteenOsapuolet,
    Osapuolenrooli,
    Osapuoli,
)


@pytest.fixture(scope="module", autouse=True)
def engine():
    engine = create_engine(
        "postgresql://{username}:{password}@{host}:{port}/{dbname}".format(
            **conf.dbconf
        ),
        future=True,
        json_serializer=json_dumps
    )
    return engine


def test_osapuolenrooli(engine):
    osapuolenroolit = [(1, 'Omistaja'),
                       (2, 'Vanhin asukas'),
                       (11, 'Tilaaja sekajäte'),
                       (12, 'Tilaaja biojäte'),
                       (13, 'Tilaaja muovipakkaus'),
                       (14, 'Tilaaja kartonkipakkaus'),
                       (15, 'Tilaaja lasipakkaus'),
                       (16, 'Tilaaja metalli'),
                       (17, 'Tilaaja monilokero'),
                       (18, 'Tilaaja liete'),
                       (111, 'Kimppaisäntä sekajäte'),
                       (112, 'Kimppaisäntä biojäte'),
                       (113, 'Kimppaisäntä muovipakkaus'),
                       (114, 'Kimppaisäntä kartonkipakkaus'),
                       (115, 'Kimppaisäntä lasipakkaus'),
                       (116, 'Kimppaisäntä metalli'),
                       (211, 'Kimppaosakas sekajäte'),
                       (212, 'Kimppaosakas biojäte'),
                       (213, 'Kimppaosakas muovipakkaus'),
                       (214, 'Kimppaosakas kartonkipakkaus'),
                       (215, 'Kimppaosakas lasipakkaus'),
                       (216, 'Kimppaosakas metalli'),
                       (311, 'Yhteyshenkilö kompostointi')]
    session = Session(engine)
    result = session.execute(select([Osapuolenrooli.id, Osapuolenrooli.selite]))
    assert [tuple(row) for row in result] == osapuolenroolit


def test_import_dvv_kohteet(engine, datadir):
    try:
        with Session(engine) as session:
            init_code_objects(session)
            import_dvv_kohteet(session,
                               poimintapvm=datetime.strptime("28.1.2022", "%d.%m.%Y").date(),
                               perusmaksutiedosto=datadir / "perusmaksurekisteri.xlsx")
    except Exception as e:
        print(f"Creating kohteet failed: {e}")

    # Kohteiden lkm
    assert session.query(func.count(Kohde.id)).scalar() == 6

    # Perusmaksurekisteristä luodulla kohteella Asunto Oy Kahden Laulumuisto on loppupäivämäärä
    kohde_nimi_filter = Kohde.nimi == 'Asunto Oy Kahden Laulumuisto'
    loppu_pvm_filter = Kohde.loppupvm == func.to_date('2100-01-01', 'YYYY-MM-DD')
    kohde_id = session.query(Kohde.id).filter(kohde_nimi_filter).filter(loppu_pvm_filter).scalar()
    assert kohde_id is not None

    # Muilla kohteilla ei loppupäivämäärää
    loppu_pvm_filter = Kohde.loppupvm != None
    assert session.query(func.count(Kohde.id)).filter(loppu_pvm_filter).scalar() == 1

    # Kaikilla kohteilla vähintään yksi omistaja
    kohteet = select(Kohde.id)
    kohteet_having_omistaja = \
        select([distinct(KohteenOsapuolet.kohde_id)]).where(KohteenOsapuolet.osapuolenrooli_id == 1)
    assert [row[0] for row in session.execute(kohteet)] == \
        [row[0] for row in session.execute(kohteet_having_omistaja)]

    # Kohteissa nimeltä Forsström, Kemp ja Lindroth vain yksi asuttu huoneisto, vanhin asukas osapuoleksi
    kohde_nimi_filter = or_(Kohde.nimi == 'Forsström', Kohde.nimi == 'Kemp', Kohde.nimi == 'Lindroth')
    vanhin_asukas_filter = KohteenOsapuolet.osapuolenrooli_id == 2
    kohde_ids = \
        session.execute(select(Kohde.id).where(kohde_nimi_filter)).fetchall()
    vanhin_asukas_id = \
        session.execute(select(KohteenOsapuolet.kohde_id).where(vanhin_asukas_filter)).fetchall()
    assert kohde_ids == vanhin_asukas_id

    # Muissa kohteissa ei vanhinta asukasta osapuolena
    assert session.query(func.count(KohteenOsapuolet.kohde_id)).filter(vanhin_asukas_filter).scalar() == 3


def test_update_dvv_kohteet(engine):
    # Updating the test database created before test fixtures
    update_test_db_command = ".\\scripts\\update_database.bat"
    try:
        subprocess.check_output(update_test_db_command, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print(f"Creating test database failed: {e.output}")

    try:
        with Session(engine) as session:
            init_code_objects(session)
            import_dvv_kohteet(session,
                               poimintapvm=datetime.strptime("31.1.2023", "%d.%m.%Y").date())
    except Exception as e:
        print(f"Updating kohteet failed: {e}")

    # Kohteiden lkm
    assert session.query(func.count(Kohde.id)).scalar() == 9

    # Perusmaksurekisteristä luodun kohteen Asunto Oy Kahden Laulumuisto loppupäivämäärä ei ole muuttunut
    kohde_nimi_filter = Kohde.nimi == 'Asunto Oy Kahden Laulumuisto'
    loppu_pvm_filter = Kohde.loppupvm == func.to_date('2100-01-01', 'YYYY-MM-DD')
    kohde_id = session.query(Kohde.id).filter(kohde_nimi_filter).filter(loppu_pvm_filter).scalar()
    assert kohde_id is not None

    # Päättyneelle kohteelle Kemp (asukas vaihtunut) asetettu loppupäivämäärä oikein
    kohde_nimi_filter = Kohde.nimi == 'Kemp'
    loppu_pvm_filter = Kohde.loppupvm == func.to_date('2023-01-16', 'YYYY-MM-DD')
    kohde_id = session.query(Kohde.id).filter(kohde_nimi_filter).filter(loppu_pvm_filter).scalar()
    assert kohde_id is not None

    # Päättyneelle kohteelle Pohjonen (omistaja vaihtunut) asetettu loppupäivämäärä oikein
    kohde_nimi_filter = Kohde.nimi == 'Pohjonen'
    loppu_pvm_filter = Kohde.loppupvm == func.to_date('2023-01-22', 'YYYY-MM-DD')
    kohde_id = session.query(Kohde.id).filter(kohde_nimi_filter).filter(loppu_pvm_filter).scalar()
    assert kohde_id is not None

    # Muilla kohteilla ei loppupäivämäärää
    loppu_pvm_filter = Kohde.loppupvm != None
    assert session.query(func.count(Kohde.id)).filter(loppu_pvm_filter).scalar() == 3

    # Uudessa kohteessa Kyykoski osapuolina Granström (omistaja) ja Kyykoski (uusi asukas)
    kohde_filter = and_(Kohde.nimi == 'Kyykoski', Kohde.alkupvm == '2023-01-17')
    kohde_id = session.execute(select(Kohde.id).where(kohde_filter)).fetchone()[0]
    osapuoli_filter = or_(Osapuoli.nimi.like('Granström%'), Osapuoli.nimi.like('Kyykoski%'))
    osapuoli_ids = \
        session.query(Osapuoli.id).filter(osapuoli_filter).order_by(Osapuoli.id)
    kohteen_osapuolet_ids = \
        session.query(KohteenOsapuolet.osapuoli_id).filter(KohteenOsapuolet.kohde_id == kohde_id).order_by(KohteenOsapuolet.osapuoli_id)
    assert [r1.id for r1 in osapuoli_ids] == [r2.osapuoli_id for r2 in kohteen_osapuolet_ids]

    # Uudella kohteella Tuntematon ei ole osapuolia
    kohde_nimi_filter = Kohde.nimi == "Tuntematon"
    kohde_id = session.query(Kohde.id).filter(kohde_nimi_filter).scalar()
    assert (
        session.query(func.count(KohteenOsapuolet.osapuoli_id))
        .filter(KohteenOsapuolet.kohde_id == kohde_id)
        .scalar()
        == 0
    )
