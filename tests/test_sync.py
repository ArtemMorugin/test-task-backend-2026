import json

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import OperationalError

from abstract_customer import get_customer_json, get_lead_json
from amocrm import Customer, Lead
from main import collect_leads_customers
from models import AbstractCustomer, db

TABLE = AbstractCustomer.__table__


def customer(entity_id, login="user", **kwargs):
    return Customer(id=entity_id, custom_fields_values={"Логин": {"value": login}}, **kwargs)


def lead(entity_id, login="user", **kwargs):
    return Lead(id=entity_id, custom_fields_values={"Логин": {"value": login}}, **kwargs)


def fetcher(customers=(), leads=()):
    def fetch(cls, **kwargs):
        assert "contacts" in kwargs["_with"]
        assert "companies" in kwargs["_with"]
        return iter(customers if cls is Customer else leads)

    return fetch


def seed(customers=(), leads=(), **kwargs):
    with db.engine.begin() as connection:
        return connection.execute(
            insert(TABLE)
            .values(
                amo_customers=[get_customer_json(value) for value in customers],
                amo_leads=[get_lead_json(value) for value in leads],
                **kwargs,
            )
            .returning(TABLE.c.id)
        ).scalar_one()


def rows():
    with db.engine.connect() as connection:
        return [
            dict(row) for row in connection.execute(select(TABLE).order_by(TABLE.c.id)).mappings()
        ]


def ids(row, name):
    return [value["general"]["id"] for value in row[name] or []]


def link(entity_id, name=None):
    return {"general": {"id": entity_id, "name": name}, "api_value": {"id": entity_id}}


def test_first_export_links_new_customer_and_lead_and_keeps_leads_without_login():
    stats = collect_leads_customers(
        fetch=fetcher(
            [customer(1), customer(2, None)],
            [lead(10), lead(11, None), lead(12, "other")],
        )
    )
    result = rows()
    assert len(result) == 3
    paired = next(row for row in result if row["amo_customers"])
    assert ids(paired, "amo_customers") == [1]
    assert ids(paired, "amo_leads") == [10]
    assert stats.customers == 1 and stats.leads == 3 and stats.inserted == 3
    assert all(row["id"] > 0 for row in result)


def test_customer_crm_id_takes_precedence_over_lead_login():
    owner = seed(customers=[customer(1, "old")])
    seed(leads=[lead(10, "new")])
    collect_leads_customers(False, fetch=fetcher([customer(1, "new")]))
    result = rows()
    assert len(result) == 2
    assert ids(next(row for row in result if row["id"] == owner), "amo_customers") == [1]


@pytest.mark.parametrize("separate_rows", [False, True])
def test_customer_login_counts_distinct_owner_rows(separate_rows):
    owner = seed(leads=[lead(10)] + ([] if separate_rows else [lead(11)]))
    if separate_rows:
        seed(leads=[lead(11)])
    collect_leads_customers(False, fetch=fetcher([customer(1)]))
    result = rows()
    found = next(row for row in result if row["amo_customers"])
    assert (found["id"] == owner) is (not separate_rows)
    assert len(result) == (3 if separate_rows else 1)


def test_merge_keeps_lead_owner_updates_current_lead_and_moves_seen_entities():
    target = seed(
        leads=[lead(10, price=1), lead(99, "stale")],
        amo_contacts=[link(999)],
        send_methods=[{"type": "mail", "value": "a"}],
    )
    seed(
        customers=[customer(1)],
        amo_contacts=[link(200)],
        amo_companies=[link(300)],
        send_methods=[{"type": "sms", "value": "b"}],
    )
    stats = collect_leads_customers(
        fetch=fetcher(
            [customer(1, _embedded={"contacts": [{"id": 200}], "companies": [{"id": 300}]})],
            [lead(10, name="current", price=42, _embedded={"contacts": [{"id": 201}]})],
        )
    )
    result = rows()
    assert len(result) == 1 and result[0]["id"] == target
    row = result[0]
    assert ids(row, "amo_customers") == [1] and ids(row, "amo_leads") == [10]
    assert row["amo_leads"][0]["api_value"]["price"] == 42
    assert row["amo_leads"][0]["general"]["name"] == "current"
    assert set(ids(row, "amo_contacts")) == {200, 201}
    assert ids(row, "amo_companies") == [300]
    assert len(row["send_methods"]) == 2
    assert stats.merged == 1 and stats.deleted == 1


def test_indexes_follow_repeated_merges_in_one_export():
    first = seed(leads=[lead(10, "old-a")])
    second = seed(leads=[lead(20, "old-b")])
    seed(customers=[customer(1)])
    stats = collect_leads_customers(
        False,
        fetch=fetcher(
            [customer(1)],
            [lead(10), lead(20), lead(30)],
        ),
    )
    result = rows()
    assert len(result) == 1 and result[0]["id"] == second
    assert result[0]["id"] != first
    assert set(ids(result[0], "amo_leads")) == {10, 20, 30}
    assert stats.merged == 2


def test_merge_of_new_customer_into_existing_lead_does_not_insert_temporary_owner():
    owner = seed(leads=[lead(10, "old")])
    stats = collect_leads_customers(fetch=fetcher([customer(1)], [lead(10)]))
    assert len(rows()) == 1 and rows()[0]["id"] == owner
    assert ids(rows()[0], "amo_customers") == [1]
    assert stats.inserted == 0 and stats.merged == 1 and stats.deleted == 0


def test_merge_preserves_latest_shared_contact_observed_this_run():
    seed(leads=[lead(10, "old"), lead(20, "old")], amo_contacts=[link(100, "cached")])
    seed(customers=[customer(1)], amo_contacts=[link(100, "cached source")])
    collect_leads_customers(
        fetch=fetcher(
            [customer(1, _embedded={"contacts": [{"id": 100, "name": "earlier customer"}]})],
            [
                lead(10, "other", _embedded={"contacts": [{"id": 100, "name": "latest lead"}]}),
                lead(20),
            ],
        )
    )
    assert len(rows()) == 1
    assert rows()[0]["amo_contacts"][0]["general"]["name"] == "latest lead"


def test_readers_continue_and_other_writers_cannot_invalidate_loaded_snapshot():
    seed(customers=[customer(1)])
    checked = []

    def fetch(cls, **kwargs):
        if cls is Customer:
            with db.engine.connect() as other:
                assert other.execute(select(TABLE.c.id)).scalars().all() == [1]
                other.rollback()
                with pytest.raises(OperationalError, match="lock timeout"), other.begin():
                    other.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    other.execute(TABLE.update().values(send_methods=[]))
            checked.append(True)
            yield customer(1)

    collect_leads_customers(fetch=fetch)
    assert checked == [True]
    with db.engine.begin() as connection:
        connection.execute(TABLE.update().values(send_methods=[]))
    assert rows()[0]["send_methods"] == []


def test_lead_does_not_use_ambiguous_customer_login():
    seed(customers=[customer(1)])
    seed(customers=[customer(2)])
    collect_leads_customers(False, fetch=fetcher(leads=[lead(10)]))
    result = rows()
    assert len(result) == 3
    assert not next(row for row in result if row["amo_leads"])["amo_customers"]


def test_lead_id_still_updates_when_customer_login_is_ambiguous():
    owner = seed(leads=[lead(10, "old")])
    seed(customers=[customer(1)])
    seed(customers=[customer(2)])
    collect_leads_customers(False, fetch=fetcher(leads=[lead(10, price=42)]))
    result = rows()
    assert len(result) == 3
    found = next(row for row in result if row["id"] == owner)
    assert found["amo_leads"][0]["api_value"]["price"] == 42


def test_full_cleanup_checks_each_entity_and_handles_null_names_and_arrays():
    owner = seed(
        customers=[customer(1), customer(2, "gone")],
        leads=[lead(10), lead(11, "gone")],
        amo_contacts=[link(100), link(101), link(102), link(103, "УДАЛИТЬ")],
        amo_companies=[link(200), link(201), link(202)],
    )
    seed(customers=[customer(3, "stale")])
    seed(amo_contacts=[link(900)])  # Orphaned aggregate.
    with db.engine.begin() as connection:
        connection.execute(insert(TABLE).values())  # Legacy null arrays.
    collect_leads_customers(
        fetch=fetcher(
            [
                customer(
                    1,
                    name=None,
                    _embedded={
                        "contacts": [
                            {"id": 100},
                            {"id": 101, "name": "УдАлИтЬ контакт"},
                            {"id": 103},
                        ],
                        "companies": [{"id": 200}, {"id": 201, "name": "удалить"}],
                    },
                ),
                customer(4, "deleted", name="УДАЛИТЬ покупателя"),
            ],
            [lead(10, name="удалить сделку")],
        )
    )
    result = rows()
    assert len(result) == 1 and result[0]["id"] == owner
    assert ids(result[0], "amo_customers") == [1]
    assert ids(result[0], "amo_leads") == []
    assert ids(result[0], "amo_contacts") == [100]
    assert ids(result[0], "amo_companies") == [200]


def test_partial_export_preserves_missing_and_marked_entities_and_status_filters():
    seed(customers=[customer(1)], leads=[lead(10)], amo_contacts=[link(100)])
    collect_leads_customers(
        False,
        fetch=fetcher(
            [customer(1, name="удалить"), customer(2, "skip", status_id=111349)],
            [lead(11, "skip", status_id=143)],
        ),
    )
    result = rows()
    assert len(result) == 1
    assert ids(result[0], "amo_leads") == [10]
    assert ids(result[0], "amo_contacts") == [100]
    assert result[0]["amo_customers"][0]["general"]["name"] == "удалить"


def test_full_export_does_not_skip_partial_statuses():
    collect_leads_customers(
        fetch=fetcher(
            [customer(1, status_id=111349)],
            [lead(10, status_id=143)],
        )
    )
    assert len(rows()) == 1
    assert ids(rows()[0], "amo_customers") == [1] and ids(rows()[0], "amo_leads") == [10]


@pytest.mark.parametrize("failed_class", [Customer, Lead])
def test_interrupted_stream_leaves_database_unchanged(failed_class):
    seed(customers=[customer(1)])
    before = rows()

    def fetch(cls, **kwargs):
        yield customer(2) if cls is Customer else lead(20)
        if cls is failed_class:
            raise RuntimeError("incomplete CRM page")

    with pytest.raises(RuntimeError, match="incomplete"):
        collect_leads_customers(fetch=fetch)
    assert rows() == before


def test_failure_during_second_write_batch_rolls_back_deletes_and_first_batch():
    seed(customers=[customer(1)])
    before = rows()
    writes = []

    def fail_second_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT"):
            writes.append(statement)
            if len(writes) == 2:
                raise RuntimeError("write failed")

    event.listen(db.engine, "before_cursor_execute", fail_second_insert)
    try:
        with pytest.raises(RuntimeError, match="write failed"):
            collect_leads_customers(
                fetch=fetcher(leads=[lead(10, None), lead(20, None)]), batch_size=1
            )
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_second_insert)
    assert len(writes) == 2
    assert rows() == before


def test_login_index_removes_old_login_and_counts_other_entities_in_same_row():
    owner = seed(customers=[customer(1), customer(2)])
    collect_leads_customers(
        False,
        fetch=fetcher(
            [customer(1, "new")],
            [lead(10, "user"), lead(11, "new")],
        ),
    )
    assert len(rows()) == 1 and rows()[0]["id"] == owner
    assert set(ids(rows()[0], "amo_leads")) == {10, 11}
    collect_leads_customers(False, fetch=fetcher([customer(2, "new")], [lead(12, "user")]))
    assert len(rows()) == 2
    assert not next(row for row in rows() if 12 in ids(row, "amo_leads"))["amo_customers"]


def test_repeated_export_is_idempotent_and_does_not_write_unchanged_rows():
    fetch = fetcher([customer(1)], [lead(10, price=42)])
    collect_leads_customers(fetch=fetch)
    before = rows()
    stats = collect_leads_customers(fetch=fetch)
    assert rows() == before
    assert stats.inserted == stats.updated == stats.deleted == 0


@pytest.mark.parametrize("is_full", [True, False])
def test_successful_empty_export_cleanup_depends_on_mode(is_full):
    seed(customers=[customer(1)])
    collect_leads_customers(is_full, fetch=fetcher())
    assert len(rows()) == (0 if is_full else 1)


@pytest.mark.parametrize("kind", ["amo_customers", "amo_leads"])
@pytest.mark.parametrize("field,value", [("id", 10), ("login", "user"), ("name", "exact name")])
def test_search_by_crm_id_exact_login_and_name_can_use_gin(kind, field, value):
    seed(customers=[customer(10, name="exact name")], leads=[lead(10, name="exact name")])
    query = select(TABLE.c.id).where(TABLE.c[kind].contains([{"general": {field: value}}]))
    with db.engine.begin() as connection:
        assert connection.execute(query).scalars().all() == [1]
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        # Column names come from the two fixed parametrized values; JSON stays bound.
        plan = "\n".join(
            connection.execute(
                text(
                    f"EXPLAIN SELECT id FROM abstract_customer WHERE {kind} @> CAST(:value AS jsonb)"
                ),
                {"value": json.dumps([{"general": {field: value}}])},
            ).scalars()
        )
    assert f"ix_abstract_customer_{kind}_gin" in plan


@pytest.mark.parametrize("existing", [False, True])
def test_number_of_sql_statements_grows_by_batches_and_transaction_commits_once(existing):
    if existing:
        for entity_id in range(1, 136):
            seed(leads=[lead(entity_id)])
    statements, commits = [], []

    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def committed(connection):
        commits.append(True)

    event.listen(db.engine, "before_cursor_execute", track)
    event.listen(db.engine, "commit", committed)
    try:
        collect_leads_customers(
            fetch=fetcher(leads=(lead(i, price=42) for i in range(1, 136))),
            batch_size=32,
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", track)
        event.remove(db.engine, "commit", committed)
    assert len(statements) == 3 + 5  # SET, LOCK, SELECT; ceil(135 / 32) multi-VALUES writes.
    assert len(commits) == 1
    assert len(rows()) == 135


def test_duplicate_stored_crm_ids_fail_without_deleting_data():
    seed(customers=[customer(1)])
    seed(customers=[customer(1)])
    before = rows()
    with pytest.raises(ValueError, match="several stored rows"):
        collect_leads_customers(fetch=fetcher())
    assert rows() == before


@pytest.mark.parametrize("size", [0, -1, 1001, True, 1.5])
def test_invalid_batch_size_is_rejected(size):
    with pytest.raises(ValueError, match="batch_size"):
        collect_leads_customers(batch_size=size)


def test_unconfigured_crm_client_cannot_trigger_cleanup():
    seed(customers=[customer(1)])
    before = rows()
    with pytest.raises(NotImplementedError, match="CRM client"):
        collect_leads_customers()
    assert rows() == before
