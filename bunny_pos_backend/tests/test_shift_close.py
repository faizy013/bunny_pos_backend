# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""The shift close is built from sums, not from every invoice of the day.

ERPNext's own builder reads each sale as a full document, which is what made
the close screen take seconds on a busy shift. These hold the fast version
against ERPNext's so it cannot drift from what ERPNext would reconcile.
"""

import unittest

import frappe
from frappe.utils import flt

from bunny_pos_backend.api.pos_session import (
	_build_closing_entry,
	_current_shift,
	get_shift_summary,
)


class TestShiftClose(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		rows = frappe.get_all(
			"POS Opening Entry",
			filters={"status": "Open", "docstatus": 1},
			fields=["name", "user"],
			order_by="creation desc",
			limit=1,
		)
		if not rows:
			raise unittest.SkipTest("no open shift on this site")
		cls.previous_user = frappe.session.user
		frappe.set_user(rows[0].user)
		cls.shift = _current_shift()

		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
			make_closing_entry_from_opening,
		)

		cls.theirs = make_closing_entry_from_opening(cls.shift)
		cls.ours = _build_closing_entry(cls.shift)

	@classmethod
	def tearDownClass(cls):
		if getattr(cls, "previous_user", None):
			frappe.set_user(cls.previous_user)

	def test_the_totals_are_erpnexts_totals(self):
		for field in ("grand_total", "net_total", "total_quantity"):
			self.assertAlmostEqual(
				flt(self.ours.get(field)), flt(self.theirs.get(field)), places=2, msg=field
			)

	def test_the_same_sales_are_listed(self):
		ours = {row.pos_invoice for row in self.ours.pos_transactions}
		theirs = {row.pos_invoice for row in self.theirs.pos_transactions}
		self.assertEqual(ours, theirs)

	def test_every_mode_expects_the_same_amount(self):
		"""What the cashier counts against has to be what ERPNext reconciles."""
		ours = {
			row.mode_of_payment: (flt(row.opening_amount, 2), flt(row.expected_amount, 2))
			for row in self.ours.payment_reconciliation
		}
		theirs = {
			row.mode_of_payment: (flt(row.opening_amount, 2), flt(row.expected_amount, 2))
			for row in self.theirs.payment_reconciliation
		}
		self.assertEqual(ours, theirs)

	def test_tax_is_gathered_the_same_way(self):
		ours = {(r.account_head, flt(r.rate, 4)): flt(r.amount, 2) for r in self.ours.taxes}
		theirs = {(r.account_head, flt(r.rate, 4)): flt(r.amount, 2) for r in self.theirs.taxes}
		self.assertEqual(ours, theirs)

	def test_the_close_screen_shows_those_same_figures(self):
		summary = get_shift_summary()
		self.assertEqual(summary["invoice_count"], len(self.theirs.pos_transactions))
		self.assertAlmostEqual(
			flt(summary["grand_total"]), flt(self.theirs.grand_total), places=2
		)
		shown = {p["mode_of_payment"]: flt(p["expected_amount"], 2) for p in summary["payments"]}
		expected = {
			r.mode_of_payment: flt(r.expected_amount, 2) for r in self.theirs.payment_reconciliation
		}
		self.assertEqual(shown, expected)
