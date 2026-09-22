# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Rename this app's DocTypes so none of them carry the Bunny prefix.

Runs before the doctypes are synced. Without it a site that already has the
old ones would end up with both: the new pair created empty by the sync, and
the old pair still holding every table the shop had set up.
"""

import frappe
from frappe.model.rename_doc import rename_doc

RENAMES = [
	("Bunny Kitchen Ticket Item", "Kitchen Ticket Item"),
	("Bunny Kitchen Ticket", "Kitchen Ticket"),
	("Bunny Restaurant Table", "Restaurant Table"),
]


def execute():
	for old, new in RENAMES:
		if not frappe.db.exists("DocType", old):
			continue
		if frappe.db.exists("DocType", new):
			# Already renamed, or the site had its own. Either way, leave it be.
			continue
		rename_doc("DocType", old, new, force=True, ignore_permissions=True)
