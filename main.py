import logging

from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from crm_sync import sync_crm

logger = logging.getLogger(__name__)


def collect_leads_customers(is_full=True):
    customers = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
    leads = amocrm_get(Lead, _with=CONTACTS)

    logger.info("CRM synchronization started (is_full=%s)", is_full)
    result = sync_crm(customers, leads, is_full=is_full)
    logger.info("CRM synchronization finished (run_id=%s)", result["run_id"])
    return result
