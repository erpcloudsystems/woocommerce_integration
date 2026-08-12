import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cstr, flt

from woocommerce_integration.general_utils import get_woocommerce_setup

# WooCommerce sends 0 as the customer id for a guest checkout, i.e. there is no
# registered account on the webshop to map to a Customer in ERPNext.
GUEST_CUSTOMER_IDS = ("", "0")

# Phone numbers are stored as the country code followed by the 10 digit national
# number (201017318848), the same shape the TCW Customer hook builds. WooCommerce
# sends the local form the buyer types at checkout (01017318848), so numbers are
# normalised before they are matched or saved, or the same buyer would come back
# as a new customer on every order.
COUNTRY_CODE = "20"
NATIONAL_NUMBER_LENGTH = 10

# Mobile fields checked by the TCW duplicate-mobile validation on Customer
CUSTOMER_MOBILE_FIELDS = ("mobile_no", "mobile1", "custom_mobile_no3")

# A shorter number cannot identify a buyer, and there are customer records
# holding two and three digit leftovers that any of them would match.
MIN_MATCHABLE_DIGITS = 9

# WooCommerce product ids are plain integers and would collide with the numeric
# item codes already in use in ERPNext, so new items get a prefixed code.
ITEM_CODE_PREFIX = "WOO-"

# custom_customer_category is mandatory on Sales Order but has no counterpart in
# the WooCommerce payload, so every imported order is filed under this one
# category. The Customer Category record must exist, or the insert fails on link
# validation (ignore_mandatory does not skip that check).
CUSTOMER_CATEGORY = "Website GP"


def create_sales_order(order_data: dict, setup: dict | None = None):
    """Create a sales order with its dependencies."""
    if not setup:
        setup = get_woocommerce_setup()

    # Orders are re-sent on every modification, only import each one once
    woocomm_order_id = cstr(order_data.get("id"))
    if woocomm_order_id and (
        existing := frappe.db.exists(
            "Sales Order", {"woocomm_order_id": woocomm_order_id}
        )
    ):
        return existing

    try:
        customer = create_update_customer(order_data)
        create_order(order_data, setup, customer.name)
    except Exception:
        frappe.log_error(
            message=frappe.get_traceback(),
            title=_("WooCommerce Error: Creation of Sales Order"),
        )
        raise


def create_update_customer(order_data: dict):
    """Find the customer the order belongs to, or create one."""
    billing_data = order_data.get("billing") or {}

    # cstr: woocomm_customer_id is a Data field, so filtering it by an int makes
    # MySQL cast the column instead of the value, and '' = 0 is true there. That
    # matched an arbitrary customer whose field was never set.
    customer_id = cstr(order_data.get("customer_id")).strip()
    is_guest = customer_id in GUEST_CUSTOMER_IDS

    erp_customer = None
    if not is_guest:
        # Customer could have been created manually which may differ in naming
        # always check woocomm_customer_id
        erp_customer = frappe.db.exists(
            "Customer", {"woocomm_customer_id": customer_id}
        )

    if not erp_customer:
        erp_customer = find_customer_by_contact(billing_data)

    if erp_customer:
        customer = frappe.get_doc("Customer", erp_customer)
        # customer_name is deliberately left alone: the order only carries the
        # name typed at checkout and must not overwrite the customer record
        # maintained in ERPNext.
        if not is_guest and not customer.woocomm_customer_id:
            customer.db_set(
                "woocomm_customer_id", customer_id, update_modified=False
            )
    else:
        customer = frappe.new_doc("Customer")
        customer.customer_name = get_customer_name(billing_data, order_data)
        customer.woocomm_customer_id = None if is_guest else customer_id
        set_customer_mobile(customer, billing_data.get("phone"))
        customer.flags.ignore_mandatory = True
        customer.insert()

    # Create address/contact if does not exist
    create_address(billing_data, customer, "Billing")
    create_address(order_data.get("shipping"), customer, "Shipping")
    create_contact(billing_data, customer)

    return customer


def get_customer_name(billing_data: dict, order_data: dict) -> str:
    """Build a customer name from the billing details, which may be partial."""
    name = " ".join(
        part
        for part in (billing_data.get("first_name"), billing_data.get("last_name"))
        if part
    ).strip()

    return (
        name
        or cstr(billing_data.get("email")).strip()
        or _("WooCommerce Order {0}").format(order_data.get("id"))
    )


def normalize_phone(phone: str) -> str:
    """Bring a phone number to the country code + national number form."""
    digits = re.sub(r"\D", "", cstr(phone))
    if not digits:
        return ""

    # Strip the international or trunk prefix the buyer may have typed, but only
    # when what is left is a full national number, so a number of an unexpected
    # length is never truncated into a different one.
    for prefix in (f"00{COUNTRY_CODE}", COUNTRY_CODE, "0"):
        if digits.startswith(prefix) and len(digits) - len(prefix) == (
            NATIONAL_NUMBER_LENGTH
        ):
            return f"{COUNTRY_CODE}{digits[len(prefix):]}"

    if len(digits) == NATIONAL_NUMBER_LENGTH:
        return f"{COUNTRY_CODE}{digits}"

    # Not a number we can interpret (landline, foreign, mistyped): keep the
    # digits as they are so it is still matched literally.
    return digits


def get_phone_variants(phone: str) -> list[str]:
    """Every form the number may already be stored in, canonical form first."""
    normalized = normalize_phone(phone)
    if not normalized:
        return []

    variants = [normalized]
    if normalized.startswith(COUNTRY_CODE) and len(normalized) == len(
        COUNTRY_CODE
    ) + NATIONAL_NUMBER_LENGTH:
        national = normalized[len(COUNTRY_CODE) :]
        # Records entered by hand often kept the local form
        variants += [national, f"0{national}"]

    if (raw := cstr(phone).strip()) and raw not in variants:
        variants.append(raw)

    return variants


def is_matchable_phone(phone: str) -> bool:
    """Whether the number is complete enough to pin down a buyer."""
    return len(re.sub(r"\D", "", cstr(phone))) >= MIN_MATCHABLE_DIGITS


def set_customer_mobile(customer, phone: str):
    """Store the checkout number on the customer in the format TCW expects.

    mobile1 is written explicitly even when there is no usable number: the field
    default is the bare country code, which the TCW duplicate-mobile validation
    then matches against the customers whose mobile_no is that same placeholder,
    failing every import with 'already used in customer'.
    """
    normalized = normalize_phone(phone)
    customer.mobile_no = normalized
    customer.mobile1 = (
        normalized[len(COUNTRY_CODE) :]
        if normalized.startswith(COUNTRY_CODE)
        and len(normalized) == len(COUNTRY_CODE) + NATIONAL_NUMBER_LENGTH
        else ""
    )


def find_customer_by_contact(billing_data: dict) -> str | None:
    """Match a checkout to an existing customer by phone or email."""
    email = cstr(billing_data.get("email")).strip()
    phone = cstr(billing_data.get("phone")).strip()
    if not email and not phone:
        return None

    # The number is the identity of a buyer here, and it is validated as unique
    # across customers, so it decides before the email does.
    if customer := find_customer_by_mobile(phone):
        return customer

    if not is_matchable_phone(phone):
        phone = ""

    for contact in get_matching_contacts(email, phone):
        if customer := frappe.db.get_value(
            "Dynamic Link",
            {
                "parenttype": "Contact",
                "parent": contact,
                "link_doctype": "Customer",
            },
            "link_name",
        ):
            return customer

    return None


def find_customer_by_mobile(phone: str) -> str | None:
    """The customer already holding this number in one of its mobile fields."""
    if not is_matchable_phone(phone):
        return None

    variants = get_phone_variants(phone)

    for field in CUSTOMER_MOBILE_FIELDS:
        for variant in variants:
            if customer := frappe.db.get_value("Customer", {field: variant}, "name"):
                return customer

    return None


def get_matching_contacts(email: str, phone: str) -> list[str]:
    """Contacts carrying the given email or phone, primary field and child rows."""
    contacts = []
    if email:
        contacts += frappe.get_all("Contact", filters={"email_id": email}, pluck="name")
        contacts += frappe.get_all(
            "Contact Email", filters={"email_id": email}, pluck="parent"
        )

    if variants := get_phone_variants(phone):
        contacts += frappe.get_all(
            "Contact Phone", filters={"phone": ("in", variants)}, pluck="parent"
        )

    return list(dict.fromkeys(contacts))


def get_linked_docs(doctype: str, customer: str) -> list[str]:
    """Names of the doctype's records linked to the customer."""
    return frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": doctype,
            "link_doctype": "Customer",
            "link_name": customer,
        },
        pluck="parent",
    )


def create_address(raw_data: dict, customer: dict, address_type: str):
    """Create an address for the customer if it does not exist."""
    if not raw_data:
        return

    # Scoped to the customer's own addresses: woocomm_customer_id is not set for
    # guests, so it cannot be used to tell one buyer's address from another's.
    if linked := get_linked_docs("Address", customer.name):
        if frappe.db.exists(
            "Address",
            {
                "name": ("in", linked),
                "pincode": raw_data.get("postcode"),
                "address_line1": raw_data.get("address_1", "Not Provided"),
                "address_type": address_type,
            },
        ):
            return

    address = frappe.new_doc("Address")
    address.address_title = customer.get("customer_name")
    address.address_line1 = raw_data.get("address_1", "Not Provided")
    address.address_line2 = raw_data.get("address_2")
    address.city = raw_data.get("city", "Not Provided")
    address.woocomm_customer_id = customer.woocomm_customer_id
    address.address_type = address_type
    address.state = raw_data.get("state")
    address.pincode = raw_data.get("postcode")
    address.phone = normalize_phone(raw_data.get("phone"))
    address.email_id = raw_data.get("email")

    if country := raw_data.get("country"):
        address.country = frappe.db.get_value("Country", {"code": country.lower()})
    else:
        address.country = frappe.get_system_settings("country")

    address.append("links", {"link_doctype": "Customer", "link_name": customer.name})
    address.flags.ignore_mandatory = True
    address.save()


def create_contact(data: dict, customer: str):
    email = data.get("email")
    phone = data.get("phone")
    if not email and not phone:
        return

    # Scoped to the customer's own contacts, for the same reason as the address
    linked = set(get_linked_docs("Contact", customer.name))
    if linked.intersection(get_matching_contacts(cstr(email), cstr(phone))):
        return

    contact = frappe.new_doc("Contact")
    contact.first_name = data.get("first_name")
    contact.last_name = data.get("last_name")
    contact.email_id = email
    contact.woocomm_customer_id = customer.woocomm_customer_id
    contact.is_primary_contact = 1
    contact.is_billing_contact = 1

    if phone:
        contact.add_phone(
            normalize_phone(phone), is_primary_mobile_no=1, is_primary_phone=1
        )

    if email:
        contact.add_email(email, is_primary=1)

    contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})
    contact.flags.ignore_mandatory = True
    contact.save()


def create_order(order: dict, woocommerce_setup: dict, customer: str):
    """Create a sales order based on the order data."""
    sales_order = frappe.new_doc("Sales Order")
    sales_order.customer = customer
    sales_order.company = woocommerce_setup.default_company
    sales_order.po_no = sales_order.woocomm_order_id = cstr(order.get("id"))
    sales_order.naming_series = woocommerce_setup.sales_order_series
    sales_order.custom_customer_category = CUSTOMER_CATEGORY

    created_date = datetime.fromisoformat(order.get("date_created")).date()
    sales_order.transaction_date = created_date
    sales_order.delivery_date = frappe.utils.add_days(
        created_date, woocommerce_setup.delivery_after or 7
    )

    add_items_to_sales_order(order, sales_order, woocommerce_setup)

    # Left as a draft on purpose: the fields the TCW submit validations require
    # (shipping_method, way_of_pay, brand, ...) are not part of the WooCommerce
    # payload, so submitting here would always fail. A user completes and submits.
    sales_order.flags.ignore_mandatory = True
    sales_order.insert()


def add_items_to_sales_order(order: dict, sales_order: dict, setup: dict):
    """Set the items in the sales order with taxes based on the order data."""
    for line_item in order.get("line_items") or []:
        item = get_item(line_item, setup)
        sales_order.append(
            "items",
            {
                "item_code": item.name,
                "item_name": item.item_name,
                "description": item.description,
                "delivery_date": sales_order.delivery_date,
                # the item's own UOM, never the WooCommerce SKU
                "uom": item.stock_uom or setup.default_uom or "Nos",
                "qty": line_item.get("quantity"),
                "rate": line_item.get("price"),
                "warehouse": setup.default_warehouse,
            },
        )

        if ordered_items_tax := flt(line_item.get("total_tax")):
            add_tax_details(
                sales_order, ordered_items_tax, "Item Tax", setup.tax_account
            )

    add_tax_details(
        sales_order,
        flt(order.get("shipping_tax")),
        "Shipping Tax",
        setup.shipping_tax_account,
    )
    add_tax_details(
        sales_order,
        flt(order.get("shipping_total")),
        "Shipping Total",
        setup.shipping_tax_account,
    )


def get_item(item_data: dict, setup: dict) -> dict:
    """Get item document or create it if it does not exist."""
    # A line item of a variable product carries the bought variant in variation_id
    woo_com_id = cstr(item_data.get("variation_id") or item_data.get("product_id"))
    sku = cstr(item_data.get("sku")).strip()

    if erp_item := frappe.db.exists("Item", {"woocomm_product_id": woo_com_id}):
        return get_item_values(erp_item)

    # Items are usually maintained in ERPNext first, so fall back to the SKU
    # before creating a duplicate of an item that is already there.
    if erp_item := match_item_by_sku(sku):
        # Store the mapping so the next order resolves on the first lookup
        frappe.db.set_value(
            "Item", erp_item, "woocomm_product_id", woo_com_id, update_modified=False
        )
        return get_item_values(erp_item)

    return create_item(item_data, woo_com_id, sku, setup)


def get_item_values(item_code: str) -> dict:
    return frappe.db.get_values(
        "Item",
        item_code,
        ["name", "item_name", "description", "stock_uom"],
        as_dict=True,
    )[0]


def match_item_by_sku(sku: str) -> str | None:
    """Match the WooCommerce SKU against an item code or one of its barcodes."""
    if not sku:
        return None

    if frappe.db.exists("Item", sku):
        return sku

    return frappe.db.get_value("Item Barcode", {"barcode": sku}, "parent")


def create_item(item_data: dict, woo_com_id: str, sku: str, setup: dict):
    """Create an item based on the item data."""
    item = frappe.new_doc("Item")
    item.item_code = f"{ITEM_CODE_PREFIX}{woo_com_id}"
    item.item_name = item_data.get("name") or item.item_code
    item.stock_uom = setup.default_uom or "Nos"
    item.item_group = "WooCommerce Products"
    item.image = (item_data.get("image") or {}).get("src")
    item.woocomm_product_id = woo_com_id
    item.flags.ignore_mandatory = True
    item.insert()

    # Surface it: an unmatched product usually means a missing SKU on the webshop
    # rather than a genuinely new item, and the duplicate needs cleaning up.
    frappe.log_error(
        title=_("WooCommerce Notice: Item Created"),
        message=_(
            "No item matched WooCommerce product {0} (SKU: {1}), so {2} was created.\n"
            "If this product already exists in ERPNext, set its WooCommerce Product "
            "ID to {0} and delete {2}."
        ).format(woo_com_id, sku or _("not set"), item.name),
    )

    return item


def add_tax_details(sales_order, price, desc, tax_account_head):
    if not price:
        return

    sales_order.append(
        "taxes",
        {
            "charge_type": "Actual",
            "account_head": tax_account_head,
            "tax_amount": price,
            "description": desc,
        },
    )
