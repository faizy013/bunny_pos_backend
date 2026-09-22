# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RestaurantTable(Document):
	def validate(self):
		self.table_name = (self.table_name or "").strip()
		if not self.table_name:
			frappe.throw(_("A table needs a name."))
		if self.seats is not None and self.seats < 0:
			frappe.throw(_("A table cannot have fewer than no seats."))
		self.section = (self.section or "").strip()

	def on_trash(self):
		"""A table with people sitting at it cannot be deleted out from under them."""
		open_order = frappe.db.exists(
			"Sales Order",
			{"po_no": self.name, "docstatus": 0, "company": frappe.db.get_value("POS Profile", self.pos_profile, "company")},
		)
		if open_order:
			frappe.throw(
				_("{0} has an open order. Settle or cancel it before deleting the table.").format(self.name)
			)
