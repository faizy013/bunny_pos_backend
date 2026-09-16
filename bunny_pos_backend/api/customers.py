# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Customer lookup for the till.

Read-only search plus a narrow create, so a cashier can attach a walk-in to
a sale without leaving the POS. Standard Customer records only.
"""

import frappe
from frappe import _
from frappe.utils import cint

from bunny_pos_backend.api.utils import get_pos_profile, pos_api

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


@frappe.whitelist()
@pos_api
def search_customers(pos_profile, search_term=None, limit=None):
	"""Customers this profile may sell to, filtered by name, phone or email."""
	profile = get_pos_profile(pos_profile)

	limit = cint(limit) or DEFAULT_LIMIT
	limit = max(1, min(limit, MAX_LIMIT))

	conditions = ["c.disabled = 0"]
	params = {"limit": limit}

	groups = [d.customer_group for d in profile.get("customer_groups") or [] if d.customer_group]
	if groups:
		expanded = set()
		for group in groups:
			expanded.add(group)
			expanded.update(frappe.db.get_descendants("Customer Group", group) or [])
		conditions.append("c.customer_group in %(groups)s")
		params["groups"] = list(expanded)

	if search_term:
		search_term = str(search_term).strip()

	if search_term:
		conditions.append(
			"""(
				c.name like %(like)s
				or c.customer_name like %(like)s
				or c.mobile_no like %(like)s
				or c.email_id like %(like)s
			)"""
		)
		params["like"] = f"%{search_term}%"

	return frappe.db.sql(
		"""
		select
			c.name,
			c.customer_name,
			c.customer_group,
			c.territory,
			c.mobile_no,
			c.email_id,
			c.default_currency
		from `tabCustomer` c
		where {conditions}
		order by c.customer_name
		limit %(limit)s
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
@pos_api
def create_customer(pos_profile, customer_name, mobile_no=None, email_id=None):
	"""Create a walk-in customer, taking defaults from the POS Profile."""
	profile = get_pos_profile(pos_profile)

	customer_name = (customer_name or "").strip()
	if not customer_name:
		frappe.throw(_("Bunny POS: a customer name is required."))

	default_customer = profile.customer
	group = territory = None
	if default_customer and frappe.db.exists("Customer", default_customer):
		group, territory = frappe.db.get_value(
			"Customer", default_customer, ["customer_group", "territory"]
		)

	groups = [d.customer_group for d in profile.get("customer_groups") or [] if d.customer_group]
	if groups:
		group = groups[0]

	doc = frappe.new_doc("Customer")
	doc.customer_name = customer_name
	doc.customer_type = "Individual"
	doc.customer_group = group or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	doc.territory = territory or frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if mobile_no:
		doc.mobile_no = str(mobile_no).strip()
	if email_id:
		doc.email_id = str(email_id).strip()
	doc.insert()

	return {
		"name": doc.name,
		"customer_name": doc.customer_name,
		"customer_group": doc.customer_group,
		"territory": doc.territory,
		"mobile_no": doc.mobile_no,
		"email_id": doc.email_id,
		"default_currency": doc.default_currency,
	}
