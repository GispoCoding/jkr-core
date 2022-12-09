from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import engine
from ..models import Tiedontuottaja


def list_tiedontuottajat():
    with Session(engine) as session:
        result = session.execute(select(Tiedontuottaja).order_by(Tiedontuottaja.tunnus))
        return result.scalars().all()


def insert_tiedontuottaja(tunnus: str, nimi: str):
    with Session(engine) as session:
        new = Tiedontuottaja(tunnus=tunnus, nimi=nimi)
        session.add(new)
        session.commit()


def get_tiedontuottaja(tunnus: str):
    with Session(engine) as session:
        tiedontuottaja = session.get(Tiedontuottaja, tunnus)
        return tiedontuottaja


def remove_tiedontuottaja(tunnus: str):
    with Session(engine) as session:
        tiedontuottaja = session.get(Tiedontuottaja, tunnus)
        session.delete(tiedontuottaja)
        session.commit()


def rename_tiedontuottaja(tunnus: str, nimi: str):
    with Session(engine) as session:
        tiedontuottaja = session.get(Tiedontuottaja, tunnus)
        tiedontuottaja.nimi = nimi
        session.commit()
