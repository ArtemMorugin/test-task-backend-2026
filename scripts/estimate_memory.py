"""Small synthetic sample, without CRM or PostgreSQL; extrapolates, not a load test."""

import argparse
import json
import resource
import sys
import tracemalloc

from abstract_customer import get_customer_json, get_lead_json
from amocrm import Customer, Lead
from main import ENTITY_FIELDS, _Snapshot


def entity(cls, entity_id, padding, current=False):
    value = cls(
        id=entity_id,
        name=f"{'Current' if current else 'Previous'} entity {entity_id}",
        custom_fields_values={
            "Логин": {"value": f"{cls.__name__}-{entity_id}"},
            "Sample payload": {"value": "x" * padding},
        },
        _embedded={"contacts": [{"id": entity_id, "name": f"Contact {entity_id}"}]},
    )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=2000)
    parser.add_argument("--leads", type=int, default=5000)
    parser.add_argument("--padding-bytes", type=int, default=1024)
    parser.add_argument(
        "--trace-python", action="store_true", help="Adds tracemalloc overhead to RSS"
    )
    args = parser.parse_args()
    if min(args.customers, args.leads) <= 0 or args.padding_bytes < 0:
        parser.error("positive entity counts and nonnegative padding are required")
    if args.customers * 5 != args.leads * 2:
        parser.error("keep the target 2:5 ratio for extrapolation")

    def rows():
        key = 0
        for cls, count, name, serialize in (
            (Customer, args.customers, "amo_customers", get_customer_json),
            (Lead, args.leads, "amo_leads", get_lead_json),
        ):
            for entity_id in range(1, count + 1):
                key += 1
                value = entity(cls, entity_id, args.padding_bytes)
                row = {"id": key, "send_methods": None, **{name: [] for name in ENTITY_FIELDS}}
                row[name] = [serialize(value)]
                # Each PG JSONB column is decoded independently; don't share DTO objects.
                for field in ENTITY_FIELDS:
                    row[field] = json.loads(json.dumps(row[field]))
                yield row

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if args.trace_python:
        tracemalloc.start()
    snapshot = _Snapshot(rows())
    for cls, count, consume in (
        (Customer, args.customers, snapshot.customer),
        (Lead, args.leads, snapshot.lead),
    ):
        for entity_id in range(1, count + 1):
            consume(entity(cls, entity_id, args.padding_bytes, current=True))
    snapshot.cleanup()
    current, peak = tracemalloc.get_traced_memory() if args.trace_python else (None, None)
    if args.trace_python:
        tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_unit = 1 if sys.platform == "darwin" else 1024
    scale = 20000 / args.customers
    mib = 1024**2
    print(
        json.dumps(
            {
                "sample": {
                    "customers": args.customers,
                    "leads": args.leads,
                    "separate_groups": len(snapshot.groups),
                    "extra_payload_bytes": args.padding_bytes,
                },
                "sample_python_current_mib": round(current / mib, 1) if current else None,
                "sample_python_peak_mib": round(peak / mib, 1) if peak else None,
                "sample_process_peak_rss_mib": round(rss_after * rss_unit / mib, 1),
                "extrapolated_python_peak_mib_20k_50k": round(peak * scale / mib) if peak else None,
                "extrapolated_process_mib_20k_50k": round(
                    (rss_before + (rss_after - rss_before) * scale) * rss_unit / mib
                ),
                "tracemalloc_enabled": args.trace_python,
                "note": "Synthetic, disjoint owners, one contact per entity; excludes SQL serialization. "
                "Real CRM payload sizes are unknown.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
