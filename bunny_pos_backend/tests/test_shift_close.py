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

	def test_change_handed_back_is_not_expected_in_the_drawer(self):
		"""Money given back as change is not money still in the till.

		ERPNext disagrees with itself here: the closing screen a shopkeeper
		actually sees subtracts the change, and the Python builder behind it
		does not. Following the builder told a till holding 364,000 that it
		was 850,000 short, so we follow the screen.
		"""
		change = flt(
			frappe.db.sql(
				"""
				select ifnull(sum(pinv.change_amount), 0)
				from `tabPOS Invoice` pinv
				where pinv.owner = %(user)s and pinv.docstatus = 1
					and pinv.pos_profile = %(profile)s
					and ifnull(pinv.consolidated_invoice, '') = ''
					and timestamp(pinv.posting_date, pinv.posting_time) >= %(start)s
				""",
				{
					"user": self.shift.user,
					"profile": self.shift.pos_profile,
					"start": self.shift.period_start_date,
				},
			)[0][0]
		)
		if change <= 0:
			raise unittest.SkipTest("no change was given on this shift")

		ours = sum(flt(r.expected_amount) for r in self.ours.payment_reconciliation)
		theirs = sum(flt(r.expected_amount) for r in self.theirs.payment_reconciliation)
		self.assertAlmostEqual(theirs - ours, change, places=2)

	def test_what_is_expected_is_what_was_actually_taken(self):
		"""Openings plus takings, less change, is what the drawers should hold."""
		taken = flt(
			frappe.db.sql(
				"""
				select ifnull(sum(pinv.paid_amount - ifnull(pinv.change_amount, 0)), 0)
				from `tabPOS Invoice` pinv
				where pinv.owner = %(user)s and pinv.docstatus = 1
					and pinv.pos_profile = %(profile)s
					and ifnull(pinv.consolidated_invoice, '') = ''
					and timestamp(pinv.posting_date, pinv.posting_time) >= %(start)s
				""",
				{
					"user": self.shift.user,
					"profile": self.shift.pos_profile,
					"start": self.shift.period_start_date,
				},
			)[0][0]
		)
		opening = sum(flt(d.opening_amount) for d in self.shift.balance_details)
		expected = sum(flt(r.expected_amount) for r in self.ours.payment_reconciliation)
		self.assertAlmostEqual(expected, opening + taken, places=2)

	def test_openings_are_carried_over_untouched(self):
		ours = {
			row.mode_of_payment: flt(row.opening_amount, 2)
			for row in self.ours.payment_reconciliation
		}
		theirs = {
			row.mode_of_payment: flt(row.opening_amount, 2)
			for row in self.theirs.payment_reconciliation
		}
		self.assertEqual(ours, theirs)

	def test_tax_is_gathered_the_same_way(self):
		ours = {(r.account_head, flt(r.rate, 4)): flt(r.amount, 2) for r in self.ours.taxes}
		theirs = {(r.account_head, flt(r.rate, 4)): flt(r.amount, 2) for r in self.theirs.taxes}
		self.assertEqual(ours, theirs)

	def test_the_close_screen_shows_what_will_be_submitted(self):
		"""The figures a cashier counts against are the ones that get filed."""
		summary = get_shift_summary()
		self.assertEqual(summary["invoice_count"], len(self.theirs.pos_transactions))
		self.assertAlmostEqual(
			flt(summary["grand_total"]), flt(self.theirs.grand_total), places=2
		)
		shown = {p["mode_of_payment"]: flt(p["expected_amount"], 2) for p in summary["payments"]}
		filed = {
			r.mode_of_payment: flt(r.expected_amount, 2) for r in self.ours.payment_reconciliation
		}
		self.assertEqual(shown, filed)
