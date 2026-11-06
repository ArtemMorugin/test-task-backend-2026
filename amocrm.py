"""CRM DTOs and the integration boundary (a real CRM client is not supplied)."""

from collections.abc import Iterator

CUSTOMERS = "customers"
COMPANIES = "companies"
CONTACTS = "contacts"
LEADS = "leads"


class BaseEntity:
    def __str__(self):
        return f"<{type(self).__name__}: {self.name}, {self.id}>"

    __repr__ = __str__

    def get_field(self, field_name):
        return (self.custom_fields_values or {}).get(field_name, {}).get("value")

    def as_dict(self):
        # Derived link lists are already present in _embedded.
        return {
            name: value
            for name, value in vars(self).items()
            if name not in {"contacts", "companies", "tags"}
        }

    def _load_links(self, embedded):
        self._embedded = embedded
        self.contacts = (embedded or {}).get(CONTACTS) or []
        self.companies = (embedded or {}).get(COMPANIES) or []
        self.tags = (embedded or {}).get("tags") or []


class Customer(BaseEntity):
    url = "api/v4/customers"
    entity_name = CUSTOMERS

    def __init__(
        self,
        id,
        name=None,
        next_price=None,
        next_date=None,
        responsible_user_id=None,
        status_id=None,
        periodicity=None,
        created_by=None,
        updated_by=None,
        created_at=None,
        updated_at=None,
        closest_task_at=None,
        is_deleted=None,
        ltv=None,
        purchases_count=None,
        average_check=None,
        account_id=None,
        custom_fields_values=None,
        _embedded=None,
    ):
        self.id = id
        self.name = name
        self.next_price = next_price
        self.next_date = next_date
        self.responsible_user_id = responsible_user_id
        self.status_id = status_id
        self.periodicity = periodicity
        self.created_by = created_by
        self.updated_by = updated_by
        self.created_at = created_at
        self.updated_at = updated_at
        self.closest_task_at = closest_task_at
        self.is_deleted = is_deleted
        self.ltv = ltv
        self.purchases_count = purchases_count
        self.average_check = average_check
        self.account_id = account_id
        # The adapter supplies the same name -> {value: ...} mapping as in the task.
        self.custom_fields_values = custom_fields_values or {}
        self._load_links(_embedded)


class Lead(BaseEntity):
    url = "api/v4/leads"
    entity_name = LEADS

    def __init__(
        self,
        name=None,
        id=None,
        price=None,
        responsible_user_id=None,
        group_id=None,
        status_id=None,
        pipeline_id=None,
        source_id=None,
        created_by=None,
        updated_by=None,
        created_at=None,
        updated_at=None,
        closest_task_at=None,
        is_deleted=None,
        score=None,
        account_id=None,
        custom_fields_values=None,
        _embedded=None,
    ):
        self.id = id
        self.name = name
        self.price = price
        self.responsible_user_id = responsible_user_id
        self.group_id = group_id
        self.status_id = status_id
        self.pipeline_id = pipeline_id
        self.source_id = source_id
        self.created_by = created_by
        self.updated_by = updated_by
        self.created_at = created_at
        self.updated_at = updated_at
        self.closest_task_at = closest_task_at
        self.is_deleted = is_deleted
        self.score = score
        self.account_id = account_id
        self.custom_fields_values = custom_fields_values or {}
        self._load_links(_embedded)


def amocrm_get(obj_cls: type[BaseEntity], **kwargs) -> Iterator[BaseEntity]:
    raise NotImplementedError(
        "CRM client is not included. Pass fetch=your_crm_iterator to "
        "collect_leads_customers; it must raise if the export is incomplete."
    )
