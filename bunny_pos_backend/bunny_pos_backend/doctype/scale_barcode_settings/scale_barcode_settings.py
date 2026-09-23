# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ScaleBarcodeSettings(Document):
	def validate(self):
		if not self.enabled:
			return
		if not (self.prefixes or "").strip():
			frappe.throw(_("A label needs at least one prefix to be recognised by."))
		if self.code_digits < 1 or self.value_digits < 1:
			frappe.throw(_("A label needs digits for both the item and the value."))
		# EAN-13 is 13 digits: prefix, item, value, and one check digit.
		longest = max(len(p.strip()) for p in self.prefixes.split(",") if p.strip())
		if longest + self.code_digits + self.value_digits > 12:
			frappe.throw(
				_("That is more digits than a barcode has: {0} plus {1} plus {2} leaves no room for the check digit.").format(
					longest, self.code_digits, self.value_digits
				)
			)
