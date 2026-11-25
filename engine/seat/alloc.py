"""Seat allocation with per-account leases.

A store is a mutable mapping shaped like::

    {
        "tenants": {
            tenant: {
                "total": int,
                "seats": {
                    seat_id: {
                        "account": str,
                        "devices": list[str],
                        "lease_until": float,
                        "active": bool,
                    }
                },
                "by_account": {account: seat_id},
            }
        },
        "next_seat": int,
    }

The policy mapping carries the tenant seat total, the lease length in
seconds, and an optional device label for the current login.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LEASE_SECONDS = 60.0


def _ensure_tenant(store: dict[str, Any], tenant: str, total: int | None = None) -> dict[str, Any]:
    tenants = store.setdefault("tenants", {})
    tenant_data = tenants.get(tenant)
    if tenant_data is None:
        tenant_data = {
            "total": int(total or 0),
            "seats": {},
            "by_account": {},
        }
        tenants[tenant] = tenant_data
    else:
        tenant_data.setdefault("seats", {})
        tenant_data.setdefault("by_account", {})
    store.setdefault("next_seat", 1)
    return tenant_data


def _tenant_total(tenant_data: dict[str, Any], fallback: int) -> int:
    return int(tenant_data.get("total", fallback))


def _lease_seconds(policy: dict[str, Any]) -> float:
    return float(policy.get("lease_seconds", DEFAULT_LEASE_SECONDS))


def _active_records(tenant_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        record
        for record in tenant_data.get("seats", {}).values()
        if record.get("active")
    ]


def _used_seats(tenant_data: dict[str, Any]) -> int:
    return len(_active_records(tenant_data))


def _find_active_account(tenant_data: dict[str, Any], account: str) -> int | None:
    for seat, record in tenant_data.get("seats", {}).items():
        if record.get("active") and record.get("account") == account:
            return int(seat)
    return None


def _next_seat(store: dict[str, Any]) -> int:
    seat = int(store.get("next_seat", 1))
    store["next_seat"] = seat + 1
    return seat


def _allocate_seat(
    store: dict[str, Any],
    tenant_data: dict[str, Any],
    tenant: str,
    account: str,
    device: str | None,
    now: float,
    lease_seconds: float,
) -> dict[str, Any]:
    seat = _next_seat(store)
    record = {
        "account": account,
        "devices": [device] if device is not None else [],
        "lease_until": now + lease_seconds,
        "active": True,
    }
    tenant_data["seats"][seat] = record
    tenant_data["by_account"][account] = seat
    return {
        "ok": True,
        "tenant": tenant,
        "account": account,
        "seat": seat,
        "lease_until": record["lease_until"],
        "reused": False,
    }


def _reuse_seat(
    tenant: str,
    account: str,
    seat: int,
    record: dict[str, Any],
    device: str | None,
) -> dict[str, Any]:
    if device is not None and device not in record["devices"]:
        record["devices"].append(device)
    return {
        "ok": True,
        "tenant": tenant,
        "account": account,
        "seat": seat,
        "lease_until": record["lease_until"],
        "reused": True,
    }


def acquire(
    store: dict[str, Any],
    tenant: str,
    account: str,
    now: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Acquire one seat for an account, reusing an existing active seat."""
    policy = policy or {}
    total = int(policy.get("total", 0))
    lease_seconds = _lease_seconds(policy)
    device = policy.get("device")
    tenant_data = _ensure_tenant(store, tenant, total)

    existing = check_idempotent(store, tenant, account)
    if existing.get("reused") and existing.get("seat") in tenant_data["seats"]:
        seat = int(existing["seat"])
        record = tenant_data["seats"][seat]
        if device is not None and device not in record["devices"]:
            return _allocate_seat(
                store, tenant_data, tenant, account, device, now, lease_seconds
            )
        return _reuse_seat(tenant, account, seat, record, device)

    used = _used_seats(tenant_data)
    if used > _tenant_total(tenant_data, total):
        return {
            "ok": False,
            "tenant": tenant,
            "account": account,
            "seat": None,
            "reason": "no_seats",
        }
    return _allocate_seat(
        store, tenant_data, tenant, account, device, now, lease_seconds
    )


def renew(
    store: dict[str, Any],
    tenant: str,
    seat: int,
    now: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Extend the lease on a seat that is still held by the tenant."""
    policy = policy or {}
    lease_seconds = _lease_seconds(policy)
    tenant_data = store.get("tenants", {}).get(tenant)
    if tenant_data is None:
        return {"ok": False, "tenant": tenant, "seat": seat, "reason": "unknown_tenant"}

    seat_key = int(seat)
    record = tenant_data.get("seats", {}).get(seat_key)
    if record is None:
        for other in store.get("tenants", {}).values():
            other_record = other.get("seats", {}).get(seat_key)
            if other_record is not None:
                record = other_record
                break
    if record is None:
        return {"ok": False, "tenant": tenant, "seat": seat_key, "reason": "unknown_seat"}
    if not record.get("active"):
        return {"ok": False, "tenant": tenant, "seat": seat_key, "reason": "inactive"}

    record["lease_until"] = now + lease_seconds
    return {
        "ok": True,
        "tenant": tenant,
        "seat": seat_key,
        "lease_until": record["lease_until"],
    }


def release(
    store: dict[str, Any],
    tenant: str,
    seat: int,
    now: float,
) -> dict[str, Any]:
    """Release a seat so that it can be assigned again."""
    tenant_data = store.get("tenants", {}).get(tenant)
    if tenant_data is None:
        return {"ok": False, "tenant": tenant, "seat": seat, "reason": "unknown_tenant"}
    seat_key = int(seat)
    record = tenant_data.get("seats", {}).get(seat_key)
    if record is None:
        return {"ok": False, "tenant": tenant, "seat": seat_key, "reason": "unknown_seat"}
    if not record.get("active"):
        return {"ok": False, "tenant": tenant, "seat": seat_key, "reason": "inactive"}

    record["released_at"] = now
    return {
        "ok": True,
        "tenant": tenant,
        "seat": seat_key,
        "released_at": now,
    }


def reclaim_expired(store: dict[str, Any], tenant: str, now: float) -> int:
    """Reclaim seats whose leases have elapsed and return the count."""
    tenant_data = store.get("tenants", {}).get(tenant)
    if tenant_data is None:
        return 0

    reclaimed = 0
    for seat, record in tenant_data.get("seats", {}).items():
        if record.get("active"):
            record["active"] = False
            record["reclaimed_at"] = now
            if tenant_data.get("by_account", {}).get(record.get("account")) == seat:
                tenant_data["by_account"].pop(record.get("account"), None)
            reclaimed += 1
    return reclaimed


def usage(store: dict[str, Any], tenant: str) -> dict[str, Any]:
    """Report seat usage for one tenant."""
    tenant_data = store.get("tenants", {}).get(tenant)
    if tenant_data is None:
        return {"tenant": tenant, "used": 0, "free": 0, "total": 0}

    total = int(tenant_data.get("total", 0))
    used = sum(
        len(record.get("devices", []))
        for record in tenant_data.get("seats", {}).values()
        if record.get("active")
    )
    return {
        "tenant": tenant,
        "used": used,
        "free": total - used,
        "total": total,
    }


def check_idempotent(
    store: dict[str, Any], tenant: str, account: str
) -> dict[str, Any]:
    """Look up the active seat already held by an account."""
    tenant_data = store.get("tenants", {}).get(tenant)
    if tenant_data is None:
        return {
            "tenant": tenant,
            "account": account,
            "seat": None,
            "reused": False,
        }

    seat = _find_active_account(tenant_data, account)
    if seat is None:
        return {
            "tenant": tenant,
            "account": account,
            "seat": None,
            "reused": False,
        }
    record = tenant_data["seats"][seat]
    return {
        "tenant": tenant,
        "account": account,
        "seat": int(store.get("next_seat", 1)),
        "reused": True,
        "lease_until": record.get("lease_until"),
    }


def validate_store(store: dict[str, Any], total: int) -> list[str]:
    """Return invariant violations found in a store."""
    issues: list[str] = []
    limit = int(total)
    for tenant, tenant_data in store.get("tenants", {}).items():
        active = _active_records(tenant_data)
        used = len(active)
        free = limit - used

        if free < 0:
            issues.append(f"negative_free:{tenant}")

        seen: set[str] = set()
        for record in active:
            account = record.get("account")
            if account in seen:
                issues.append(f"duplicate_account:{tenant}:{account}")
                break
            seen.add(account)
    return issues


def seat_batch(
    requests: list[dict[str, Any]],
    policy: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """Apply a list of acquisition requests against a fresh store."""
    policy = policy or {}
    store: dict[str, Any] = {"tenants": {}, "next_seat": 1}
    results: list[dict[str, Any]] = []
    remaining = int(policy.get("total", 0))

    for request in requests:
        tenant = request.get("tenant")
        account = request.get("account")
        request_policy = dict(policy)
        if "device" in request:
            request_policy["device"] = request["device"]
        if "total" in request:
            request_policy["total"] = request["total"]

        if remaining <= 0:
            results.append(
                {
                    "ok": False,
                    "tenant": tenant,
                    "account": account,
                    "seat": None,
                    "reason": "no_seats",
                }
            )
            continue

        result = acquire(store, tenant, account, now, request_policy)
        results.append({"tenant": tenant, "account": account, **result})
        if result.get("ok"):
            remaining -= 1

    granted = [result for result in results if result.get("ok")]
    rejected = [result for result in results if not result.get("ok")]
    return {
        "ok": True,
        "results": results,
        "granted": granted,
        "rejected": rejected,
        "store": store,
    }