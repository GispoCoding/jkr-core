
-- The view needs to be updated when osapuoli table gets extra fields.
--
-- Also, seems like osapuoli table has been changed unbenkownst to this
-- view, so cannot just rename the fields anymore. Will have to drop the
-- view and create a new one.
drop view jkr.v_kohteen_osapuolet;
create view jkr.v_kohteen_osapuolet as
select
  ko.kohde_id,
  ko.osapuolenrooli_id,
  op.*
from
  jkr.kohteen_osapuolet ko
  join jkr.osapuoli op
    ON op.id = ko.osapuoli_id
;
