import datetime
import logging
from typing import List, Optional, TypeVar, Union

from pydantic import root_validator, validator

from jkrimporter.utils.date import parse_date_string

logger = logging.getLogger(__name__)


def split_by_comma(value: Union[str, None]) -> List[str]:
    if value is None:
        return []
    return value.split(",") if value else []


def trim_ytunnus(value: Union[str, None]) -> Union[str, None]:
    if value is None:
        return value

    ytunnus = value.strip()

    if ytunnus in ("-", "000"):
        ytunnus = None

    return ytunnus


def normalize_date(v: Union[datetime.date, str, None]) -> Union[datetime.date, None]:
    if isinstance(v, str):
        if v:
            return parse_date_string(v)
        else:
            return None

    return v


def date_pre_validator(*attributes):
    return validator(*attributes, allow_reuse=True, pre=True)(normalize_date)


T = TypeVar("T")


def empty_to_none(v: Union[T, None]) -> Optional[T]:
    if not v:
        return None

    return v


def int_validator(*attributes):
    return validator(*attributes, allow_reuse=True, pre=True)(empty_to_none)


def check_alkupvm_lt_loppupvm(cls, values):
    alku, loppu = values.get("alkupvm"), values.get("loppupvm")
    if alku is not None and loppu is not None and loppu < alku:
        raise ValueError("alkupvm must be less than loppupvm")
    return values


def date_range_root_validator():
    return root_validator(allow_reuse=True)(check_alkupvm_lt_loppupvm)
