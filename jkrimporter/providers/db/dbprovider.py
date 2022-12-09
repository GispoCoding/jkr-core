import datetime
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from sqlalchemy.orm import Session

from jkrimporter.model import Asiakas, JkrData
from jkrimporter.model import Tyhjennystapahtuma as JkrTyhjennystapahtuma
from jkrimporter.utils.intervals import IntervalCounter
from jkrimporter.utils.progress import Progress

from . import codes
from .codes import init_code_objects
from .database import engine
from .models import Kohde, Kuljetus, Tiedontuottaja
from .services.buildings import counts as building_counts
from .services.buildings import (
    find_building_candidates_for_kohde,
    find_buildings_for_kohde,
)
from .services.kohde import (
    add_ulkoinen_asiakastieto_for_kohde,
    create_multiple_and_uninhabited_kohteet,
    create_new_kohde,
    create_paritalo_kohteet,
    create_perusmaksurekisteri_kohteet,
    create_single_asunto_kohteet,
    find_kohde_by_address,
    find_kohde_by_kiinteisto,
    find_kohde_by_prt,
    get_ulkoinen_asiakastieto,
    update_kohde,
    update_ulkoinen_asiakastieto,
)
from .services.osapuoli import (
    create_or_update_haltija_osapuoli,
    create_or_update_yhteystieto_osapuoli,
)
from .services.sopimus import update_sopimukset_for_kohde

logger = logging.getLogger(__name__)


def count(jkr_data: JkrData):
    prt_counts: Dict[str, IntervalCounter] = defaultdict(IntervalCounter)
    kitu_counts: Dict[str, IntervalCounter] = defaultdict(IntervalCounter)
    address_counts: Dict[str, IntervalCounter] = defaultdict(IntervalCounter)

    # TODO: So we have duplicate asiakkaat in the same address for the same interval.
    # So what? Jukka will have biojäte, sekajäte, energia. So will Aija.
    for asiakas in jkr_data.asiakkaat.values():
        for prt in asiakas.rakennukset:
            prt_counts[prt].append(asiakas.voimassa)
        for kitu in asiakas.kiinteistot:
            kitu_counts[kitu].append(asiakas.voimassa)
        # this just saves all the buildings with the same address string together
        addr = asiakas.haltija.osoite.osoite_rakennus()
        if addr:
            address_counts[addr].append(asiakas.voimassa)

    return prt_counts, kitu_counts, address_counts


def insert_kuljetukset(
    session,
    kohde,
    tyhjennystapahtumat: List[JkrTyhjennystapahtuma],
    raportointi_alkupvm: Optional[datetime.date],
    raportointi_loppupvm: Optional[datetime.date],
    urakoitsija: Tiedontuottaja,
):
    for tyhjennys in tyhjennystapahtumat:
        print("importing tyhjennys")
        print(tyhjennys)
        if not tyhjennys.alkupvm:
            # In many cases, only one date is known for tyhjennys. Looks like
            # in those cases the date is marked as the end date.
            alkupvm = tyhjennys.loppupvm or raportointi_alkupvm
        else:
            alkupvm = tyhjennys.alkupvm
        loppupvm = tyhjennys.loppupvm or raportointi_loppupvm

        jatetyyppi = codes.jatetyypit[tyhjennys.jatelaji]
        if not jatetyyppi:
            logger.warning(
                f"Ohitetaan tyhjennystapahtuma. Jätetyyppi "
                f"'{tyhjennys.jatelaji}' unknown"
            )
            continue

        # TODO: here we have the assumption that there is only one kuljetus
        # for a given period and jatetyyppi. It may be false, there may even
        # be more than one kuljetus for a period and jatetyyppi with the *same*
        # urakoitsija, with the same *or* different customer ids.
        # TODO: If the customer id is the same, just let it be. If the customer id
        # is different, we should create a new kohde.
        exists = any(
            k.jatetyyppi == jatetyyppi
            and k.alkupvm == alkupvm
            and k.loppupvm == loppupvm
            for k in kohde.kuljetus_collection
        )

        if not exists:
            db_kuljetus = Kuljetus(
                kohde=kohde,
                jatetyyppi=jatetyyppi,
                alkupvm=alkupvm,
                loppupvm=loppupvm,
                tyhjennyskerrat=tyhjennys.tyhjennyskerrat,
                massa=tyhjennys.massa,
                tilavuus=tyhjennys.tilavuus,
                tiedontuottaja=urakoitsija,
            )
            session.add(db_kuljetus)


def find_and_update_kohde(
    session: "Session",
    asiakas: "Asiakas",
    do_create: bool,
    do_update: bool,
    prt_counts: Dict[str, int],
    kitu_counts: Dict[str, int],
    address_counts: Dict[str, int],
) -> Union[Kohde, None]:

    kohde = None
    ulkoinen_asiakastieto = get_ulkoinen_asiakastieto(session, asiakas.asiakasnumero)
    if ulkoinen_asiakastieto:
        print("Kohde found by customer id.")
        update_ulkoinen_asiakastieto(ulkoinen_asiakastieto, asiakas)

        kohde = ulkoinen_asiakastieto.kohde
        if do_update:
            update_kohde(kohde, asiakas)
    else:
        # TODO: Add check_name parameter here. If
        # 1) Jätelaji is sekajäte,
        # 2) Customer id is not found (we have a new customer) AND
        # 3) The location already has a kohde for sekajäte with another customer id
        #    AND the same time period,
        # 4) The other customer id has a *different name*?,
        # we should create a new kohde.
        print("Customer id not found. Searching for kohde by customer data...")
        if asiakas.rakennukset:
            kohde = find_kohde_by_prt(session, asiakas)
        if not kohde and asiakas.kiinteistot:
            kohde = find_kohde_by_kiinteisto(session, asiakas)
        if (
            not kohde
            and asiakas.haltija.osoite.postinumero
            and asiakas.haltija.osoite.katunimi
        ):
            kohde = find_kohde_by_address(session, asiakas)
        if kohde and do_update:
            update_kohde(kohde, asiakas)
        elif do_create:
            # this creates kohde without buildings
            print("Kohde not found, creating new one...")
            kohde = create_new_kohde(session, asiakas)
        if kohde:
            add_ulkoinen_asiakastieto_for_kohde(session, kohde, asiakas)
        else:
            print("Could not find kohde.")

    if do_create and not kohde.rakennus_collection:
        print("New kohde created. Looking for buildings...")
        buildings = find_buildings_for_kohde(
            session, asiakas, prt_counts, kitu_counts, address_counts
        )
        if buildings:
            kohde.rakennus_collection = buildings

        elif not kohde.ehdokasrakennus_collection:
            building_candidates = find_building_candidates_for_kohde(session, asiakas)
            if building_candidates:
                kohde.ehdokasrakennus_collection = building_candidates

    return kohde


def import_asiakastiedot(
    session: Session,
    asiakas: Asiakas,
    alkupvm: Optional[datetime.date],
    loppupvm: Optional[datetime.date],
    urakoitsija: Tiedontuottaja,
    do_create: bool,
    do_update: bool,
    prt_counts: Dict[str, IntervalCounter],
    kitu_counts: Dict[str, IntervalCounter],
    address_counts: Dict[str, IntervalCounter],
):

    kohde = find_and_update_kohde(
        session, asiakas, do_create, do_update, prt_counts, kitu_counts, address_counts
    )
    if not kohde:
        print(f"Could not find kohde for asiakas {asiakas}, skipping...")
        return

    # Update osapuolet from the same tiedontuottaja. These functions will not
    # touch data from other tiedontuottajat.
    # TODO: set osapuoli type based on type of kuljetus
    create_or_update_haltija_osapuoli(session, kohde, asiakas, do_update)
    # TODO: do not create yhteystieto on Lahti request
    create_or_update_yhteystieto_osapuoli(session, kohde, asiakas, do_update)

    update_sopimukset_for_kohde(session, kohde, asiakas, loppupvm, urakoitsija)
    insert_kuljetukset(
        session,
        kohde,
        asiakas.tyhjennystapahtumat,
        alkupvm,
        loppupvm,
        urakoitsija,
    )

    session.commit()


def import_dvv_kohteet(
    session: Session,
    alkupvm: Optional[str],
    loppupvm: Optional[str],
    perusmaksutiedosto: Optional[Path],
):
    # 1) Yhden asunnon talot (asutut): DVV:n tiedoissa kiinteistöllä yksi rakennus ja
    # asukas.
    # 2) Yhden asunnon talot (tyhjillään tai asuttu): DVV:n tiedoissa kiinteistön
    # rakennuksilla sama omistaja. Voi olla yksi tai monta rakennusta.Yhdessä
    # rakennuksessa voi olla asukkaita.
    # - Asiakas on vanhin asukas. Tuodaan myös kaikki omistajat yhteystiedoiksi.
    # - Kiinteistön muut rakennukset asumattomia (esim. lomarakennukset, saunat),
    #  joten ne liitetään, jos sama omistaja ja osoite.
    # - Kiinteistön asumattomista muun omistajan tai osoitteen rakennuksista
    # tehdään erilliset kohteet omistajan ja osoitteen mukaan.
    # - Kohdetta ei tuoda, jos samalla kiinteistöllä muita asuttuja rakennuksia.
    single_asunto_kohteet = create_single_asunto_kohteet(session, alkupvm, loppupvm)
    session.commit()
    print(f"Imported {len(single_asunto_kohteet)} single kohteet")

    # Perusmaksurekisteri may combine buildings and kiinteistöt to a single kohde.
    # 3) Kerros ja rivitalot: Perusmaksurekisterin aineistosta asiakasnumero. Voi olla
    # yksi tai monta rakennusta.
    # 7) Vapaa-ajanasunnot: kaikki samat omistajat. Perusmaksurekisterin aineistosta
    # asiakasnumero. Voi olla yksi tai monta rakennusta.
    # - Kohteeseen yhdistetään rakennukset kiinteistöistä riippumatta.
    # - Asiakkaiksi tallennetaan kaikki kohteen rakennusten omistajat.
    # - Saunat ja talousrakennukset liitetään, jos sama kiinteistö, omistaja ja osoite
    # kuin jollakin rakennuksista.
    # - Kiinteistö(je)n muita rakennuksia ei liitetä, sillä niissä voi olla asukkaita,
    # joilla erilliset sopimukset.
    if perusmaksutiedosto:
        perusmaksukohteet = create_perusmaksurekisteri_kohteet(
            session, perusmaksutiedosto, alkupvm, loppupvm
        )
    session.commit()
    print(f"Imported {len(perusmaksukohteet)} kohteet with perusmaksu data")

    # 4) Paritalot: molemmille huoneistoille omat kohteet
    # Does it matter this is imported after 7? -No, because paritalot will not
    # interact with 7.
    # - Asiakas on kumpikin vanhin asukas erikseen. Tuodaan myös kaikki omistajat yhteystiedoiksi.
    # - Kiinteistöllä kaksi kohdetta joilla sama rakennus, muita rakennuksia ei liitetä.
    # TODO: add all buildings on kiinteistö?
    paritalo_kohteet = create_paritalo_kohteet(session, alkupvm, loppupvm)
    session.commit()
    print(f"Imported {len(paritalo_kohteet)} paritalokohteet")

    # Remaining buildings will be combined by owner and kiinteistö.
    # TODO: limit imported types

    # 5) Muut rakennukset, joissa huoneistotieto eli asukas: DVV:n tiedoissa
    # kiinteistöllä yksi rakennus ja asukas. Voi olla 1 rakennus.
    # TODO: näihin vielä asukas omistajan sijaan asiakkaaksi. TODO: makes no sense.
    # Siinä tapauksessa vahtimestari saa koko koulun jätehuollon laskut, ja koulua
    # ei tuodakaan omistajan nimellä.

    # Does it matter this is imported after 7? - Ei. Näitä on *yksi* vapaa-ajanasuntojen
    # kanssa samalla kiinteistöllä *koko alueella*, siinäkin useampi asukas.
    # Tällä kiinteistöllä on yhden asunnon talo,
    # muu pientalo, vapaa-ajanasunto ja autotalli. Autotalli eri osoitteessa, joten siitä
    # joka tapauksessa oma kohde. Kaikilla samat omistajat. Vapaa-ajanasunto tuotu ensin.
    # Yhden asunnon talo ja muu pientalo tuodaan lopuksi, koska kummassakin asukkaita.

    # 6) Muut asumisen rakennukset (asuntola, palvelutalo): käyttötarkoitus + omistaja
    # + kiinteistö
    # Does it matter this is imported after 7? - Ei, koska käyttötarkoituksen mukaan
    # rajataan kuitenkin erilliset kohteet.

    # 8) Koulut: käyttötarkoitus + omistaja + sijaintikiinteistö
    # 9) Muut rakennukset, joissa huoneisto: sama kiinteistö, sama omistaja.
    # TODO: näihin omistaja asiakkaaksi. Voiko tehdä yhdessä 5:n kanssa?
    # Does it matter if this is imported at the same time as 6 & 8? Voi tehdä, jos
    # halutaan alkuperäinen järjestys, eli kouluille asiakkaaksi ainoa asukas eikä omistaja.
    # Useamman asukkaan kohteille asiakkaaksi omistaja.

    # - Asiakas on suurin omistaja.
    # - Kiinteistön rakennukset yhdistetään omistajan ja osoitteen mukaan.
    # TODO: limit added buildings on kiinteistö?
    # - Kiinteistön asumattomista muun omistajan tai osoitteen rakennuksista
    # tehdään erilliset kohteet omistajan ja osoitteen mukaan.
    multiple_and_uninhabited_kohteet = create_multiple_and_uninhabited_kohteet(
        session, alkupvm, loppupvm
    )
    session.commit()
    print(f"Imported {len(multiple_and_uninhabited_kohteet)} remaining kohteet")


class DbProvider:
    def write(
        self,
        jkr_data: JkrData,
        tiedontuottaja_lyhenne: str,
        ala_luo: bool,
        ala_paivita: bool,
    ):
        try:
            print(len(jkr_data.asiakkaat))
            progress = Progress(len(jkr_data.asiakkaat))

            prt_counts, kitu_counts, address_counts = count(jkr_data)
            # print(prt_counts)
            # print(kitu_counts)
            # print(address_counts)
            with Session(engine) as session:
                init_code_objects(session)

                tiedoston_tuottaja = session.get(Tiedontuottaja, tiedontuottaja_lyhenne)

                # The same tiedontuottaja may contain data from multiple
                # urakoitsijat. Create all urakoitsijat in the db first.
                print("Importoidaan urakoitsijat")
                urakoitsijat: Set[str] = set()
                for asiakas in jkr_data.asiakkaat.values():
                    if asiakas.asiakasnumero.jarjestelma not in urakoitsijat:
                        print(f"found urakoitsija {asiakas.asiakasnumero.jarjestelma}")
                        urakoitsijat.add(asiakas.asiakasnumero.jarjestelma)
                tiedontuottajat: Dict[str, Tiedontuottaja] = {}
                for urakoitsija_tunnus in urakoitsijat:
                    print("checking or adding urakoitsija")
                    tiedontuottaja = session.get(Tiedontuottaja, urakoitsija_tunnus)
                    print(tiedontuottaja)
                    if not tiedontuottaja:
                        print("not found, adding")
                        tiedontuottaja = Tiedontuottaja(
                            # let's create the urakoitsijat using only y-tunnus for now.
                            # We can create tiedontuottaja-nimi maps later.
                            tunnus=urakoitsija_tunnus,
                            nimi=urakoitsija_tunnus,
                        )
                        session.add(tiedontuottaja)
                    tiedontuottajat[urakoitsija_tunnus] = tiedontuottaja
                session.commit()

                print("Importoidaan asiakastiedot")
                for asiakas in jkr_data.asiakkaat.values():
                    print("---------")
                    print("importing")
                    print(asiakas)
                    progress.tick()

                    # Asiakastieto may come from different urakoitsija than the
                    # immediate tiedontuottaja. In such a case, the asiakas
                    # information takes precedence.
                    urakoitsija_tunnus = asiakas.asiakasnumero.jarjestelma

                    import_asiakastiedot(
                        session,
                        asiakas,
                        jkr_data.alkupvm,
                        jkr_data.loppupvm,
                        tiedontuottajat[urakoitsija_tunnus],
                        not ala_luo,
                        not ala_paivita,
                        prt_counts,
                        kitu_counts,
                        address_counts,
                    )
                session.commit()
                progress.complete()

        except Exception as e:
            logger.exception(e)
        finally:
            logger.debug(building_counts)

    def write_dvv_kohteet(
        self,
        alkupvm: Optional[str],
        loppupvm: Optional[str],
        perusmaksutiedosto: Optional[Path],
    ):
        """
        This method creates kohteet from dvv data existing in the database.

        Optionally, a perusmaksurekisteri xlsx file may be provided to
        combine dvv buildings with the same customer id.
        """
        try:
            with Session(engine) as session:
                init_code_objects(session)
                print("Luodaan kohteet")
                import_dvv_kohteet(session, alkupvm, loppupvm, perusmaksutiedosto)

        except Exception as e:
            logger.exception(e)
        finally:
            logger.debug(building_counts)
