from engine.seat.alloc import (
    acquire,
    release,
    seat_batch,
    usage,
)


def test_first_acquire_succeeds():
    store = {}
    policy = {"total": 1, "lease_seconds": 60, "device": "phone"}

    first = acquire(store, "t1", "alice", 0, policy)

    assert first["ok"] is True
    assert first["seat"] == 1
    assert first["lease_until"] == 60

    second = acquire(store, "t1", "bob", 0, policy)
    assert second["ok"] is False


def test_same_account_reuses_seat():
    store = {}
    policy = {"total": 2, "lease_seconds": 60, "device": "phone"}

    first = acquire(store, "t1", "alice", 0, policy)
    again = acquire(store, "t1", "alice", 1, policy)

    assert first["ok"] is True
    assert again["ok"] is True
    assert again["seat"] == first["seat"]
    assert usage(store, "t1")["used"] == 1


def test_release_frees_seat_immediately():
    store = {}
    policy = {"total": 1, "lease_seconds": 60, "device": "phone"}

    first = acquire(store, "t1", "alice", 0, policy)
    released = release(store, "t1", first["seat"], 1)

    assert released["ok"] is True
    assert usage(store, "t1")["used"] == 0

    replacement = acquire(store, "t1", "bob", 2, policy)
    assert replacement["ok"] is True


def test_usage_counts_accounts():
    store = {}
    policy = {"total": 2, "lease_seconds": 60}

    first = acquire(store, "t1", "alice", 0, {**policy, "device": "phone"})
    second = acquire(store, "t1", "alice", 1, {**policy, "device": "tablet"})

    assert first["ok"] is True
    assert second["ok"] is True
    assert usage(store, "t1") == {
        "tenant": "t1",
        "used": 1,
        "free": 1,
        "total": 2,
    }


def test_batch_tenants_are_independent():
    result = seat_batch(
        [
            {"tenant": "t1", "account": "alice"},
            {"tenant": "t2", "account": "bob"},
        ],
        {"total": 1, "lease_seconds": 60},
        0,
    )

    assert result["ok"] is True
    assert len(result["granted"]) == 2
    assert usage(result["store"], "t1")["used"] == 1
    assert usage(result["store"], "t2")["used"] == 1