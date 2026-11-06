"""Serialization of CRM entities; persistence belongs to the synchronizer."""

from amocrm import Customer, Lead


def get_customer_json(customer: Customer) -> dict:
    fields = customer.custom_fields_values or {}
    return {
        "general": {
            "id": customer.id,
            "login": customer.get_field("Логин"),
            "billing_type": fields.get("Вид оплаты", {}).get("value"),
            "is_robot": bool(fields.get("Робот")),
            "is_nds": bool(fields.get("Оплата с НДС")),
            "system": fields.get("Система", {}).get("value"),
            "host": fields.get("Хост", {}).get("value"),
            "name": customer.name,
        },
        "api_value": customer.as_dict(),
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        "general": {
            "id": lead.id,
            "login": lead.get_field("Логин"),
            "name": lead.name,
        },
        "api_value": lead.as_dict(),
    }
