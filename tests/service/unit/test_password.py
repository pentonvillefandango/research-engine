import re

import pytest
from research_engine.keys.password import check_format, hash_password, verify_password

PATTERN = re.compile(r"^scrypt:16384:8:1:[A-Za-z0-9_-]{22}:[A-Za-z0-9_-]{43}$")


def test_hash_format_and_random_salt() -> None:
    a, b = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert PATTERN.match(a) and PATTERN.match(b)
    assert a != b


def test_verify() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong horse battery", h)
    assert not verify_password("", h)
    assert not verify_password("correct horse battery", "scrypt:1:2")
    assert not verify_password("correct horse battery", "")


@pytest.mark.parametrize(
    "bad", ["x", "scrypt:16384:8:1:abc", "bcrypt:1:1:1:a:b", "scrypt:a:8:1:x:y"]
)
def test_check_format_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="GUI_PASSWORD_HASH"):
        check_format(bad)


def test_verify_false_when_scrypt_exceeds_memory_limit() -> None:
    # Well-formed (passes check_format) but n=2**20, r=8 needs ~1 GiB > the 64 MiB maxmem.
    good = hash_password("correct horse battery").split(":")
    big = ":".join(["scrypt", str(2**20), "8", "1", good[4], good[5]])
    assert check_format(big)[0] == 2**20
    assert verify_password("correct horse battery", big) is False
