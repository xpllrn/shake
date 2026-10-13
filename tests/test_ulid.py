"""Unit tests for ULID generator."""


from shake.ulid import generate_ulid, is_valid_ulid


def test_ulid_generation() -> None:
    u = generate_ulid()
    assert len(u) == 26
    assert is_valid_ulid(u) is True


def test_ulid_monotonic_order() -> None:
    t1 = 1000000
    t2 = 1000050
    u1 = generate_ulid(t1)
    u2 = generate_ulid(t2)
    assert u1 < u2
