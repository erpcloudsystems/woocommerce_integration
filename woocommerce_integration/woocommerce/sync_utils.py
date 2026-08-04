import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime

from woocommerce_integration.general_utils import (
    get_woocommerce_setup,
    update_woocommerce_sync,
)
from woocommerce_integration.order_creation_utils import create_sales_order
from woocommerce_integration.woocommerce_connector import WooCommerceConnector


@frappe.whitelist()
def batch_sync_stock():
    """
    Flow: From ERPNext to WooCommerce.
    Called by the scheduler. Batch update items from all recent stock updates.
    """
    setup = get_woocommerce_setup()
    setup.check_permission("write")
    if not setup.enable_stock_sync:
        return

    filters = {"warehouse": setup.warehouse}
    if setup.last_stock_sync:
        filters["modified"] = (">=", setup.last_stock_sync)

    # Get all recent Bins
    data = {"update": []}
    for row in frappe.get_all(
        "Bin", filters=filters, fields=["item_code", "actual_qty"]
    ):
        if product_id := frappe.db.get_value(
            "Item", row.item_code, "woocomm_product_id"
        ):
            data["update"].append(
                {
                    "id": product_id,
                    "stock_quantity": cint(row.actual_qty),
                    "manage_stock": True,
                }
            )

    # Update stock in WooCommerce
    if data["update"]:
        connector = WooCommerceConnector(setup)
        connector.batch_update_products(data)
        update_woocommerce_sync("last_stock_sync", get_datetime())


@frappe.whitelist()
def batch_sync_order():
    """Batch sync orders from WooCommerce to ERPNext.

    Every order is imported independently: one that fails is logged and skipped
    so the rest of the batch still lands, instead of the first bad order
    aborting the whole run.

    The sync watermark stops at the first failure, so a skipped order is picked
    up again on the next run. Orders after it are re-fetched too, which is
    harmless because create_sales_order() skips orders already imported.
    """
    setup = get_woocommerce_setup()
    setup.check_permission("write")

    if not setup.enable_order_sync:
        return

    last_sync_datetime = None
    has_failed = False
    failed_orders = []

    for order in get_woocommerce_orders():
        try:
            create_sales_order(order, setup)
            # Commit per order so a later failure cannot roll back this one
            frappe.db.commit()
            if not has_failed:
                last_sync_datetime = order.get("date_modified")
        except Exception:
            traceback = frappe.get_traceback(with_context=True)
            # Discard the partially built order (customer, address, contact,
            # draft) before logging: the rollback would take the Error Log with
            # it, since log_error() writes in the current transaction.
            frappe.db.rollback()

            has_failed = True
            failed_orders.append(order.get("id"))
            frappe.log_error(
                title=_("WooCommerce Error: Order {0} Skipped").format(
                    order.get("id")
                ),
                message=traceback,
            )

    if last_sync_datetime:
        update_woocommerce_sync("last_order_sync", last_sync_datetime)
        frappe.db.commit()

    if failed_orders:
        # Surface the count in the Scheduled Job Log; details are in Error Log
        frappe.log_error(
            title=_("WooCommerce Notice: {0} Order(s) Skipped").format(
                len(failed_orders)
            ),
            message=_("WooCommerce order ids skipped this run: {0}").format(
                ", ".join(cstr(order_id) for order_id in failed_orders)
            ),
        )


def get_woocommerce_orders():
    """Get all the new orders from WooCommerce."""
    setup = get_woocommerce_setup()
    woocommerce = WooCommerceConnector(setup)
    last_sync_datetime = (
        get_datetime(setup.last_order_sync).isoformat()
        if setup.last_order_sync
        else None
    )
    per_page = cint(setup.order_per_page) or 10
    return woocommerce.get_orders(
        per_page=per_page,
        modified_after=last_sync_datetime,
        status=setup.order_status_filters,
        orderby="modified",
        order="asc",
    )
