from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

from jkrimporter import __version__
from jkrimporter.providers.db.dbprovider import DbProvider
from jkrimporter.providers.db.services.tiedontuottaja import (
    get_tiedontuottaja,
    insert_tiedontuottaja,
    list_tiedontuottajat,
    remove_tiedontuottaja,
    rename_tiedontuottaja
 )
from jkrimporter.providers.pjh.pjhprovider import PjhTranslator
from jkrimporter.providers.pjh.siirtotiedosto import PjhSiirtotiedosto
from jkrimporter.providers.nokia.nokiaprovider import NokiaTranslator
from jkrimporter.providers.nokia.siirtotiedosto import NokiaSiirtotiedosto
from jkrimporter.providers.lahti.lahtiprovider import LahtiTranslator
from jkrimporter.providers.lahti.siirtotiedosto import LahtiSiirtotiedosto
from jkrimporter.utils.date import parse_date_string


@dataclass
class Provider:
    Translator: type
    Siirtotiedosto: type


PROVIDERS = {
    # we may also add other providers using the *same* formats
    "PJH": Provider(Translator=PjhTranslator, Siirtotiedosto=PjhSiirtotiedosto),
    "HKO": Provider(Translator=NokiaTranslator, Siirtotiedosto=NokiaSiirtotiedosto),
    "LSJ": Provider(Translator=LahtiTranslator, Siirtotiedosto=LahtiSiirtotiedosto)
}


def version_callback(value: bool):
    if value:
        print(__version__)
        raise typer.Exit()


def main_callback(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Kertoo lataustyökalun versionumeron.",
    ),
):
    ...


app = typer.Typer(callback=main_callback)

provider_app = typer.Typer()
app.add_typer(
    provider_app, name="tiedontuottaja", help="Muokkaa ja tarkastele tiedontuottajia."
)


@app.command("import", help="Import transportation data to JKR.")
def import_data(
    siirtotiedosto: Path = typer.Argument(..., help="Siirtotiedoston kansio"),
    tiedontuottajatunnus: str = typer.Argument(
        ..., help="Tiedon toimittajan tunnus. Esim. 'PJH', 'HKO', 'LSJ'"
    ),
    ala_luo: bool = typer.Option(
        False,
        "--ala_luo",
        help="Älä luo uusia kohteita tästä datasta."
    ),
    ala_paivita: bool = typer.Option(
        False,
        "--ala_paivita",
        help="Älä päivitä yhteystietoja tai kohteen voimassaoloaikaa tästä datasta.",
    ),
    alkupvm: str = typer.Argument(None, help="Importoitavan datan alkupvm"),
    loppupvm: str = typer.Argument(None, help="Importoitavan datan loppupvm"),
):
    tiedontuottaja = get_tiedontuottaja(tiedontuottajatunnus)
    if not tiedontuottaja:
        typer.echo(
            f"Tiedontuottajaa {tiedontuottaja} ei löydy järjestelmästä. Lisää komennolla `jkr tiedontuottaja add`"
        )
        raise typer.Exit()
    provider = PROVIDERS[tiedontuottajatunnus]
    data = provider.Siirtotiedosto(siirtotiedosto)

    if alkupvm:
        alkupvm = parse_date_string(alkupvm)
    if loppupvm:
        alkupvm = parse_date_string(loppupvm)
    if tiedontuottajatunnus == "HKO":
        ala_paivita = True
        if not alkupvm or not loppupvm:
            typer.echo("Nokian importin yhteydessä päivämäärät ovat pakolliset")
            raise typer.Exit()

    translator = provider.Translator(data, tiedontuottajatunnus)
    jkr_data = translator.as_jkr_data(alkupvm, loppupvm)
    print('writing to db...')
    db = DbProvider()
    db.write(jkr_data, tiedontuottajatunnus, ala_luo, ala_paivita)

    print("VALMIS!")


@app.command("create_dvv_kohteet", help="Create kohteet from DVV data in database.")
def create_dvv_kohteet(
    alkupvm: Optional[str] = typer.Argument(None, help="Importoitavan datan alkupvm"),
    loppupvm: Optional[str] = typer.Argument(None, help="Importoitavan datan loppupvm"),
    perusmaksutiedosto: Optional[Path] = typer.Argument(
        None, help="Perusmaksurekisteritiedosto"
    ),
):
    db = DbProvider()
    # support all combinations of known and unknown alku- and loppupvm
    if alkupvm == "None":
        alkupvm = None
    if loppupvm == "None":
        loppupvm = None
    db.write_dvv_kohteet(alkupvm, loppupvm, perusmaksutiedosto)

    print("VALMIS!")


@provider_app.command("add", help="Lisää uusi tiedontuottaja järjestelmään.")
def tiedontuottaja_add_new(
    tunnus: str = typer.Argument(..., help="Tiedontuottajan tunnus. Esim. 'PJH'"),
    name: str = typer.Argument(..., help="Tiedontuottajan nimi."),
):
    insert_tiedontuottaja(tunnus.upper(), name)


@provider_app.command("rename", help="Muuta tiedontuottajan nimi järjestelmässä.")
def tiedontuottaja_rename(
    tunnus: str = typer.Argument(..., help="Tiedontuottajan tunnus. Esim. 'PJH'"),
    name: str = typer.Argument(..., help="Tiedontuottajan uusi nimi."),
):
    rename_tiedontuottaja(tunnus.upper(), name)


@provider_app.command("remove", help="Poista tiedontuottaja järjestelmästä.")
def tiedontuottaja_remove(
    tunnus: str = typer.Argument(..., help="Tiedontuottajan tunnus. Esim. 'PJH'")
):
    remove_tiedontuottaja(tunnus.upper())


@provider_app.command("list", help="Listaa järjestelmästä löytyvät tiedontuottajat.")
def tiedontuottaja_list():
    for tiedontuottaja in list_tiedontuottajat():
        print(f"{tiedontuottaja.tunnus}\t{tiedontuottaja.nimi}")


if __name__ == "__main__":
    app()
