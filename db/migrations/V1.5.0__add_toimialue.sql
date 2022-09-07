CREATE TABLE jkr.toimialue (
	id integer NOT NULL GENERATED BY DEFAULT AS IDENTITY ,
	nimi text,
	geom geometry(MULTIPOLYGON, 3067) NOT NULL,
	CONSTRAINT toimialue_pk PRIMARY KEY (id)
);
ALTER TABLE jkr.toimialue OWNER TO jkr_admin;
