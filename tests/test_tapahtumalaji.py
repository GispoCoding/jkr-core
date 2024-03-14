import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jkrimporter import conf
from jkrimporter.providers.db.database import json_dumps
from jkrimporter.providers.db.models import Tapahtumalaji


@pytest.fixture(scope="module", autouse=True)
def engine():
    engine = create_engine(
        "postgresql://{username}:{password}@{host}:{port}/{dbname}".format(
            **conf.dbtestconf
        ),
        future=True,
        json_serializer=json_dumps,
    )
    return engine


def test_tapahtumalajit(engine):
    tapahtumalajit = [
        ("PERUSMAKSU", "Perusmaksu"),
        ("AKP", "AKP"),
        ("TYHJENNYSVALI", "Tyhjennysväli"),
        ("KESKEYTTAMINEN", "Keskeyttäminen"),
        ("ERILLISKERAYKSESTA_POIKKEAMINEN", "Erilliskeräyksestä poikkeaminen"),
        ("MUU", "Muu poikkeaminen"),
    ]
    session = Session(engine)
    result = session.execute(select([Tapahtumalaji.koodi, Tapahtumalaji.selite]))
    assert [tuple(row) for row in result] == tapahtumalajit
