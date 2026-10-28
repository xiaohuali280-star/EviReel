"""E1 pure-logic tests (no database, storage or network)."""
import base64
import hashlib

import pytest

from vam.platform.identity import hash_password, verify_password
from vam.platform.uploads import MIN_PART, MiB, composite_etag, part_length, plan_parts, _safe_name, _valid_md5_b64


def test_part_plan_respects_s3_limits():
    assert plan_parts(1, 8 * MiB) == (8 * MiB, 1)
    assert plan_parts(8 * MiB, 8 * MiB) == (8 * MiB, 1)
    assert plan_parts(8 * MiB + 1, 8 * MiB) == (8 * MiB, 2)
    assert plan_parts(10 * MiB, 1) == (MIN_PART, 2)                       # never below 5 MiB
    size = 100 * 1024 * MiB                                               # 100 GiB still fits 10,000 parts
    part, count = plan_parts(size, 8 * MiB)
    assert count <= 10000 and part % MiB == 0 and part * count >= size
    with pytest.raises(ValueError):
        plan_parts(0, 8 * MiB)


def test_part_lengths_cover_the_file_exactly():
    size = 23 * MiB + 17
    part, count = plan_parts(size, 5 * MiB)
    assert sum(part_length(size, part, count, n) for n in range(1, count + 1)) == size
    assert part_length(size, part, count, count) == size - part * (count - 1)
    with pytest.raises(ValueError):
        part_length(size, part, count, count + 1)


def test_composite_etag_is_not_a_whole_file_md5():
    parts = [b"a" * 10, b"b" * 7]
    md5s = [base64.b64encode(hashlib.md5(p).digest()).decode() for p in parts]
    expected = hashlib.md5(b"".join(hashlib.md5(p).digest() for p in parts)).hexdigest() + "-2"
    assert composite_etag(md5s) == expected
    assert composite_etag(md5s).split("-")[0] != hashlib.md5(b"".join(parts)).hexdigest()


def test_md5_header_validation_and_name_sanitizing():
    assert _valid_md5_b64(base64.b64encode(b"\0" * 16).decode())
    assert not _valid_md5_b64("not-base64-not-base64!!!")
    assert not _valid_md5_b64(base64.b64encode(b"\0" * 15).decode().ljust(24, "="))
    assert _safe_name("C:\\fakepath\\..\\课程.mp4") == "课程.mp4"
    assert _safe_name("a/b/\x00c.mp4") == "c.mp4"


def test_password_hashing_round_trip():
    encoded = hash_password("correct horse battery")
    assert encoded.startswith("scrypt$") and "correct" not in encoded
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("correct horse batterz", encoded)
    assert not verify_password("x", "garbage")
    with pytest.raises(ValueError):
        hash_password("short")
