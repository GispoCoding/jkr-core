CREATE OR REPLACE FUNCTION jkr.kohteiden_paatokset(kohde_ids integer[], tarkistuspvm date)
RETURNS TABLE(
    Kohde_id integer,
    Kompostoi date,
    "Perusmaksupäätös voimassa" date,
    "Perusmaksupäätös" text,
    "Tyhjennysvälipäätös voimassa" date,
    "Tyhjennysvälipäätös" text,
    "Akp-kohtuullistaminen voimassa" date,
    "Akp-kohtuullistaminen" text,
    "Keskeytys voimassa" date,
    "Keskeytys" text,
    "Erilliskeräysvelvoitteesta poikkeaminen voimassa" date,
    "Erilliskeräysvelvoitteesta poikkeaminen" text
) AS $$
BEGIN
    RETURN QUERY
    WITH rakennukset AS (
        SELECT
            kr.kohde_id,
            kr.rakennus_id
        FROM
            unnest(kohde_ids) AS k_id(kohde_id)
        JOIN
            jkr.kohteen_rakennukset AS kr ON kr.kohde_id = k_id.kohde_id
    ),
    paatokset AS (
        SELECT
            r.kohde_id,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'PERUSMAKSU' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.loppupvm END) AS perusmaksupaatos_voimassa,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'PERUSMAKSU' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.paatosnumero END) AS perusmaksupaatos,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'TYHJENNYSVALI' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.loppupvm END) AS tyhjennysvalipaatos_voimassa,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'TYHJENNYSVALI' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.paatosnumero END) AS tyhjennysvalipaatos,
            CASE WHEN 
                COUNT(*) = SUM(CASE WHEN vp.tapahtumalaji_koodi = 'AKP' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN 1 ELSE 0 END) THEN
                    MAX(CASE WHEN vp.tapahtumalaji_koodi = 'AKP' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.loppupvm END)
                ELSE NULL
            END AS akp_kohtuullistaminen_voimassa,
            CASE WHEN 
                COUNT(*) = SUM(CASE WHEN vp.tapahtumalaji_koodi = 'AKP' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN 1 ELSE 0 END) THEN
                    MAX(CASE WHEN vp.tapahtumalaji_koodi = 'AKP' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.paatosnumero END)
                ELSE NULL
            END AS akp_kohtuullistaminen,
            CASE WHEN 
                COUNT(*) = SUM(CASE WHEN vp.tapahtumalaji_koodi = 'KESKEYTTAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN 1 ELSE 0 END) THEN
                    MAX(CASE WHEN vp.tapahtumalaji_koodi = 'KESKEYTTAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.loppupvm END)
                ELSE NULL
            END AS keskeytys_voimassa,
            CASE WHEN 
                COUNT(*) = SUM(CASE WHEN vp.tapahtumalaji_koodi = 'KESKEYTTAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN 1 ELSE 0 END) THEN
                    MAX(CASE WHEN vp.tapahtumalaji_koodi = 'KESKEYTTAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.paatosnumero END)
                ELSE NULL
            END AS keskeytys,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'ERILLISKERAYKSESTA_POIKKEAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.loppupvm END) AS erilliskerayksesta_poikkeaminen_voimassa,
            MAX(CASE WHEN vp.tapahtumalaji_koodi = 'ERILLISKERAYKSESTA_POIKKEAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1' THEN vp.paatosnumero END) AS erilliskerayksesta_poikkeaminen
        FROM
            rakennukset r
        LEFT JOIN
            jkr.viranomaispaatokset AS vp ON vp.rakennus_id = r.rakennus_id
        WHERE
            (vp.tapahtumalaji_koodi = 'PERUSMAKSU' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1')
            OR (vp.tapahtumalaji_koodi = 'TYHJENNYSVALI' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1')
            OR (vp.tapahtumalaji_koodi = 'AKP' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1')
            OR (vp.tapahtumalaji_koodi = 'KESKEYTTAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1')
            OR (vp.tapahtumalaji_koodi = 'ERILLISKERAYKSESTA_POIKKEAMINEN' AND vp.voimassaolo @> tarkistuspaivamaara AND vp.paatostulos_koodi = '1')
        GROUP BY
            r.kohde_id
    ),
    composting_status AS (
        SELECT DISTINCT ON (k_id.kohde_id)
            k_id.kohde_id,
            kom.loppupvm AS kompostoi
        FROM
            unnest(kohde_ids) AS k_id(kohde_id)
        LEFT JOIN
            jkr.kompostorin_kohteet AS k ON k.kohde_id = k_id.kohde_id
        LEFT JOIN
            jkr.kompostori AS kom ON kom.id = k.kompostori_id
        WHERE
            kom.voimassaolo @> tarkistuspaivamaara
        ORDER BY
            k_id.kohde_id, kom.loppupvm DESC
    )
    SELECT
        k_id.kohde_id,
        cs.kompostoi,
        p.perusmaksupaatos_voimassa AS "Perusmaksupäätös voimassa",
        p.perusmaksupaatos AS "Perusmaksupäätös",
        p.tyhjennysvalipaatos_voimassa AS "Tyhjennysvälipäätös voimassa",
        p.tyhjennysvalipaatos AS "Tyhjennysvälipäätös",
        p.akp_kohtuullistaminen_voimassa AS "Akp-kohtuullistaminen voimassa",
        p.akp_kohtuullistaminen AS "Akp-kohtuullistaminen",
        p.keskeytys_voimassa AS "Keskeytys voimassa",
        p.keskeytys AS "Keskeytys",
        p.erilliskerayksesta_poikkeaminen_voimassa AS "Erilliskeräysvelvoitteesta poikkeaminen voimassa",
        p.erilliskerayksesta_poikkeaminen AS "Erilliskeräysvelvoitteesta poikkeaminen"
    FROM
        unnest(kohde_ids) AS k_id(kohde_id)
    LEFT JOIN
        composting_status cs ON cs.kohde_id = k_id.kohde_id
    LEFT JOIN
        paatokset p ON p.kohde_id = k_id.kohde_id;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION jkr.kohteiden_kuljetukset(kohde_ids integer[], tarkistuspvm date)
RETURNS TABLE(
    Kohde_id integer,
    Muovi date,
    Kartonki date,
    Metalli date,
    Lasi date,
    Biojäte date,
    Monilokero date,
    Sekajate date,
    Akp date
) AS $$
BEGIN
    RETURN QUERY
    WITH valid_kuljetukset AS (
        SELECT
            k.kohde_id,
            CASE WHEN jt.selite = 'Muovi' THEN k.loppupvm END AS muovi,
            CASE WHEN jt.selite = 'Kartonki' THEN k.loppupvm END AS kartonki,
            CASE WHEN jt.selite = 'Metalli' THEN k.loppupvm END AS metalli,
            CASE WHEN jt.selite = 'Lasi' THEN k.loppupvm END AS lasi,
            CASE WHEN jt.selite = 'Biojäte' THEN k.loppupvm END AS biojate,
            CASE WHEN jt.selite = 'Monilokero' THEN k.loppupvm END AS monilokero,
            CASE WHEN jt.selite = 'Sekajäte' THEN k.loppupvm END AS sekajate,
            CASE WHEN jt.selite = 'Muu' THEN k.loppupvm END AS akp
        FROM
            jkr.kuljetus k
        JOIN
            jkr_koodistot.jatetyyppi jt ON k.jatetyyppi_id = jt.id
        WHERE
            k.kohde_id = ANY(kohde_ids)
            AND k.aikavali @> tarkistuspvm
    ),
    aggregated_kuljetukset AS (
        SELECT
            vk.kohde_id,
            MAX(vk.muovi) AS muovi,
            MAX(vk.kartonki) AS kartonki,
            MAX(vk.metalli) AS metalli,
            MAX(vk.lasi) AS lasi,
            MAX(vk.biojate) AS biojate,
            MAX(vk.monilokero) AS monilokero,
            MAX(vk.sekajate) AS sekajate,
            MAX(vk.akp) AS akp
        FROM
            valid_kuljetukset vk
        GROUP BY
            vk.kohde_id
    )
    SELECT
        k_id.kohde_id,
        ak.muovi,
        ak.kartonki,
        ak.metalli,
        ak.lasi,
        ak.biojate,
        ak.monilokero,
        ak.sekajate,
        ak.akp
    FROM
        unnest(kohde_ids) AS k_id(kohde_id)
    LEFT JOIN
        aggregated_kuljetukset ak ON ak.kohde_id = k_id.kohde_id;
END;
$$ LANGUAGE plpgsql;