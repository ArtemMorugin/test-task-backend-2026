"""Atomic CRM synchronization with no per-entity database queries."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from abstract_customer import get_customer_json, get_lead_json
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import AbstractCustomer, db

logger = logging.getLogger(__name__)

ENTITY_FIELDS = ("amo_customers", "amo_leads", "amo_contacts", "amo_companies")
WRITE_FIELDS = ENTITY_FIELDS + ("send_methods",)


def _entity_id(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("CRM entity must have a positive integer id")
    return value


def _login(payload):
    value = payload["general"].get("login")
    if value is not None and not isinstance(value, str):
        raise ValueError("CRM login must be a string or null")
    return value


def _marked_for_deletion(payload):
    return "удалить" in (payload["general"].get("name") or "").casefold()


@dataclass
class SyncStats:
    customers: int = 0
    leads: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    merged: int = 0


@dataclass
class _Group:
    key: int
    persisted: bool
    entities: dict[str, dict]
    send_methods: list | None = None
    dirty: bool = False
    seen: dict[str, dict[int, int]] = field(
        default_factory=lambda: {name: {} for name in ENTITY_FIELDS}
    )

    def values(self):
        return {
            **{name: list(self.entities[name].values()) for name in ENTITY_FIELDS},
            "send_methods": self.send_methods,
        }


class _Index:
    """Count entities per owner: several same-login entities in one row are unique."""

    def __init__(self):
        self.by_id = {}
        self.by_login = defaultdict(Counter)

    def add(self, key, payload):
        entity_id = _entity_id(payload["general"]["id"])
        owner = self.by_id.get(entity_id)
        if owner is not None and owner != key:
            raise ValueError(f"CRM id {entity_id} belongs to several stored rows")
        self.by_id[entity_id] = key
        login = _login(payload)
        if login:
            self.by_login[login][key] += 1

    def remove(self, key, payload):
        del self.by_id[payload["general"]["id"]]
        login = _login(payload)
        if login:
            owners = self.by_login[login]
            owners[key] -= 1
            if not owners[key]:
                del owners[key]
            if not owners:
                del self.by_login[login]

    def unique_login(self, login):
        owners = self.by_login.get(login)
        if owners and len(owners) == 1:
            return next(iter(owners))
        return None


class _Snapshot:
    def __init__(self, rows):
        self.groups = {}
        self.deleted = set()
        self.indexes = {name: _Index() for name in ENTITY_FIELDS[:2]}
        self.next_key = -1  # Temporary keys never go into PostgreSQL.
        self.stats = SyncStats()
        self.version = 0
        for row in rows:
            entities = {}
            for name in ENTITY_FIELDS:
                values = {}
                for payload in row[name] or []:
                    entity_id = _entity_id(payload["general"]["id"])
                    if entity_id in values:
                        raise ValueError("Duplicate CRM id inside a stored JSON array")
                    values[entity_id] = payload
                    if name in self.indexes:
                        self.indexes[name].add(row["id"], payload)
                entities[name] = values
            self.groups[row["id"]] = _Group(row["id"], True, entities, row["send_methods"])
            self.next_key = min(self.next_key, row["id"] - 1)

    def _new(self):
        group = _Group(self.next_key, False, {name: {} for name in ENTITY_FIELDS})
        self.groups[group.key] = group
        self.next_key -= 1
        return group.key

    def _put(self, key, name, payload):
        group = self.groups[key]
        entity_id = _entity_id(payload["general"]["id"])
        previous = group.entities[name].get(entity_id)
        self.version += 1
        group.seen[name][entity_id] = self.version
        if previous == payload:
            return
        if name in self.indexes:
            index = self.indexes[name]
            if previous is not None:
                index.remove(key, previous)
            index.add(key, payload)
        group.entities[name][entity_id] = payload
        group.dirty = True

    def _links(self, key, name, links):
        for link in links or []:
            entity_id = _entity_id(link["id"])
            # Embedded API links can contain just an id; keep previously known details.
            previous = self.groups[key].entities[name].get(entity_id, {})
            payload = {
                "general": {**previous.get("general", {}), **link},
                "api_value": {**previous.get("api_value", {}), **link},
            }
            self._put(key, name, payload)

    def customer(self, customer):
        payload = get_customer_json(customer)
        entity_id = _entity_id(customer.id)
        login = _login(payload)
        if not login:
            return
        key = self.indexes["amo_customers"].by_id.get(entity_id)
        if key is None:
            key = self.indexes["amo_leads"].unique_login(login)
        if key is None:
            key = self._new()
        self._put(key, "amo_customers", payload)
        self._links(key, "amo_contacts", customer.contacts)
        self._links(key, "amo_companies", customer.companies)
        self.stats.customers += 1

    def _merge(self, target_key, source_key):
        target, source = self.groups[target_key], self.groups[source_key]
        for name in ENTITY_FIELDS:
            if name in self.indexes:
                for payload in source.entities[name].values():
                    self.indexes[name].remove(source_key, payload)
                    self.indexes[name].add(target_key, payload)
            for entity_id, payload in source.entities[name].items():
                source_version = source.seen[name].get(entity_id, 0)
                target_version = target.seen[name].get(entity_id, 0)
                # The same contact/company can be linked to both groups. Keep the
                # latest value read this run, rather than overwriting it with cache.
                if source_version >= target_version:
                    target.entities[name][entity_id] = payload
                    if source_version:
                        target.seen[name][entity_id] = source_version
        # Do not discard notification settings when removing the source row.
        if source.send_methods:
            methods = list(target.send_methods or [])
            for method in source.send_methods:
                if method not in methods:
                    methods.append(method)
            target.send_methods = methods
        target.dirty = True
        del self.groups[source_key]
        if source.persisted:
            self.deleted.add(source_key)
        self.stats.merged += 1

    def lead(self, lead):
        payload = get_lead_json(lead)
        entity_id = _entity_id(lead.id)
        login = _login(payload)
        key = self.indexes["amo_leads"].by_id.get(entity_id)
        customer_key = self.indexes["amo_customers"].unique_login(login) if login else None
        if key is not None and customer_key is not None and key != customer_key:
            self._merge(key, customer_key)
        if key is None:
            key = customer_key if customer_key is not None else self._new()
        # Always apply the current version, including after a merge.
        self._put(key, "amo_leads", payload)
        self._links(key, "amo_contacts", lead.contacts)
        self._links(key, "amo_companies", lead.companies)
        self.stats.leads += 1

    def cleanup(self):
        for key, group in list(self.groups.items()):
            for name in ENTITY_FIELDS:
                values = group.entities[name]
                kept = {
                    entity_id: payload
                    for entity_id, payload in values.items()
                    if entity_id in group.seen[name] and not _marked_for_deletion(payload)
                }
                if len(kept) != len(values):
                    group.entities[name] = kept
                    group.dirty = True
            if not group.entities["amo_customers"] and not group.entities["amo_leads"]:
                del self.groups[key]
                if group.persisted:
                    self.deleted.add(key)

    def save(self, connection, batch_size):
        table = AbstractCustomer.__table__
        deleted = list(self.deleted)
        for offset in range(0, len(deleted), batch_size):
            connection.execute(
                delete(table).where(table.c.id.in_(deleted[offset : offset + batch_size]))
            )
        self.stats.deleted = len(deleted)

        # Explicit multi-VALUES statements: executemany UPDATE would still execute N SQLs.
        for persisted in (True, False):
            batch = []
            for group in self.groups.values():
                if group.persisted != persisted or (persisted and not group.dirty):
                    continue
                values = group.values()
                if persisted:
                    values["id"] = group.key
                    self.stats.updated += 1
                else:
                    self.stats.inserted += 1
                batch.append(values)
                if len(batch) == batch_size:
                    self._write(connection, batch, persisted)
                    batch = []
            if batch:
                self._write(connection, batch, persisted)

    @staticmethod
    def _write(connection, batch, persisted):
        table = AbstractCustomer.__table__
        statement = insert(table).values(batch)
        if persisted:
            statement = statement.on_conflict_do_update(
                index_elements=[table.c.id],
                set_={
                    **{name: statement.excluded[name] for name in WRITE_FIELDS},
                    "updated_at": func.now(),
                },
            )
        connection.execute(statement)


def collect_leads_customers(is_full=True, *, fetch=None, batch_size=1000):
    """Run in a Flask app context; the supplied fetcher must raise on incomplete export.

    This entry point owns its transaction. It serializes writers to the aggregate
    table while reading and reconciling the CRM stream; SELECT readers continue.
    No database writes or cleanup happen until both iterators are exhausted.
    """
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= 1000
    ):
        raise ValueError("batch_size must be between 1 and 1000")
    fetch = fetch or amocrm_get
    table = AbstractCustomer.__table__
    with db.engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("LOCK TABLE abstract_customer IN SHARE ROW EXCLUSIVE MODE"))
        rows = connection.execution_options(yield_per=batch_size).execute(
            select(table.c.id, *(table.c[name] for name in WRITE_FIELDS))
        )
        try:
            snapshot = _Snapshot(rows.mappings())
        finally:
            rows.close()
        # yield_per enables server-side cursors; do not apply it to INSERT/DELETE.
        connection = connection.execution_options(stream_results=False, yield_per=0)
        for customer in fetch(Customer, _with=f"{COMPANIES},{CONTACTS}"):
            if not is_full and customer.status_id == 111349:
                continue
            if customer.get_field("Логин"):
                snapshot.customer(customer)
        for lead in fetch(Lead, _with=f"{COMPANIES},{CONTACTS}"):
            if not is_full and lead.status_id == 143:
                continue
            snapshot.lead(lead)
        if is_full:
            snapshot.cleanup()
        snapshot.save(connection, batch_size)
    logger.info("CRM synchronization completed: %s", snapshot.stats)
    return snapshot.stats
