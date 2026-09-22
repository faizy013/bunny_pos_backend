# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Tests for the money path.

These run against whatever POS Profile the site already has an open shift
for, because that is the only state ``create_invoice`` will bill against.
"""

import unittest

import frappe

from bunny_pos_backend.api.invoices import create_invoice
from bunny_pos_backend.api.items import get_items


def _open_shift():
	"""Any open shift on the site.

	The test runner signs in as Administrator, who never has one, so the tests
	borrow the cashier that does -- create_invoice bills against the caller's
	own shift and there is no way around that by design.
	"""
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"status": "Open", "docstatus": 1},
		fields=["name", "pos_profile", "user"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _a_sellable_item(profile):
	"""An in-stock item the profile can actually sell."""
	items = get_items(profile, limit=40).get("items") or []
	for row in items:
		if row.get("is_stock_item") and (row.get("stock_qty") or 0) >= 5:
			return row
	for row in items:
		if not row.get("is_stock_item"):
			return row
	return None


class TestCreateInvoice(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.shift = _open_shift()
		if not cls.shift:
			raise unittest.SkipTest("no open shift on this site; open one before running these")
		cls.previous_user = frappe.session.user
		frappe.set_user(cls.shift.user)
		cls.item = _a_sellable_item(cls.shift.pos_profile)
		if not cls.item:
			frappe.set_user(cls.previous_user)
			raise unittest.SkipTest("no sellable item on this POS Profile")

	@classmethod
	def tearDownClass(cls):
		if getattr(cls, "previous_user", None):
			frappe.set_user(cls.previous_user)

	def _cart(self, qty=1):
		return [{"item_code": self.item["item_code"], "qty": qty}]

	def test_same_request_id_bills_once(self):
		"""The whole point: a retry must not charge the customer twice."""
		request_id = frappe.generate_hash(length=20)

		first = create_invoice(self._cart(), request_id=request_id)
		second = create_invoice(self._cart(), request_id=request_id)

		self.assertEqual(first["name"], second["name"])
		self.assertEqual(
			frappe.db.count("POS Invoice", {"remarks": ("like", f"%{request_id}%")}),
			1,
		)

	def test_request_id_survives_a_cold_cache(self):
		"""Redis restarting between the sale and the retry must not cost money."""
		from bunny_pos_backend.api.invoices import _request_key

		request_id = frappe.generate_hash(length=20)
		first = create_invoice(self._cart(), request_id=request_id)

		frappe.cache().delete(_request_key(request_id))

		second = create_invoice(self._cart(), request_id=request_id)
		self.assertEqual(first["name"], second["name"])

	def test_different_request_ids_are_separate_sales(self):
		"""Two customers buying the same thing are still two sales."""
		first = create_invoice(self._cart(), request_id=frappe.generate_hash(length=20))
		second = create_invoice(self._cart(), request_id=frappe.generate_hash(length=20))
		self.assertNotEqual(first["name"], second["name"])

	def test_works_without_a_request_id(self):
		"""An older till that does not send one still sells."""
		result = create_invoice(self._cart())
		self.assertTrue(result["name"])

	def test_empty_cart_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			create_invoice([])

	def test_rate_from_the_till_needs_the_profile_to_allow_it(self):
		"""Only a profile that permits rate changes may be priced by the till.

		Worth pinning down: without the check, anyone holding a cashier's token
		could sell anything for a rupee.
		"""
		profile = frappe.get_doc("POS Profile", self.shift.pos_profile)
		was = profile.allow_rate_change
		cart = [{"item_code": self.item["item_code"], "qty": 1, "rate": 0.01}]

		try:
			profile.db_set("allow_rate_change", 0)
			frappe.clear_document_cache("POS Profile", profile.name)
			with self.assertRaises(frappe.ValidationError):
				create_invoice(cart, request_id=frappe.generate_hash(length=20))

			profile.db_set("allow_rate_change", 1)
			frappe.clear_document_cache("POS Profile", profile.name)
			result = create_invoice(cart, request_id=frappe.generate_hash(length=20))
			doc = frappe.get_doc("POS Invoice", result["name"])
			self.assertEqual(float(doc.items[0].rate), 0.01)
		finally:
			profile.db_set("allow_rate_change", was)
			frappe.clear_document_cache("POS Profile", profile.name)

	def test_a_rate_just_above_zero_is_not_given_away(self):
		"""A near-zero rate rounds to a 100% discount, which ERPNext reads as free.

		Without the clamp the customer walks out with the item for nothing.
		"""
		profile = frappe.get_doc("POS Profile", self.shift.pos_profile)
		was = profile.allow_rate_change
		try:
			profile.db_set("allow_rate_change", 1)
			frappe.clear_document_cache("POS Profile", profile.name)
			for asked in (0.01, 0.5, 1):
				result = create_invoice(
					[{"item_code": self.item["item_code"], "qty": 1, "rate": asked}],
					request_id=frappe.generate_hash(length=20),
				)
				doc = frappe.get_doc("POS Invoice", result["name"])
				self.assertEqual(float(doc.items[0].rate), float(asked))
				self.assertGreater(float(doc.grand_total), 0)
		finally:
			profile.db_set("allow_rate_change", was)
			frappe.clear_document_cache("POS Profile", profile.name)

	def test_a_zero_value_sale_is_not_reported_as_returned(self):
		"""Everything discounted away is still a sale with goods to bring back.

		fully_returned used to be judged on money: nothing left to refund read
		as already refunded, and the cashier could not take the item back.
		"""
		from bunny_pos_backend.api.invoices import search_invoices

		profile = frappe.get_doc("POS Profile", self.shift.pos_profile)
		was = profile.allow_discount_change
		try:
			profile.db_set("allow_discount_change", 1)
			frappe.clear_document_cache("POS Profile", profile.name)
			created = create_invoice(
				[{"item_code": self.item["item_code"], "qty": 1, "discount_percentage": 100}],
				request_id=frappe.generate_hash(length=20),
			)
		finally:
			profile.db_set("allow_discount_change", was)
			frappe.clear_document_cache("POS Profile", profile.name)

		row = next(
			(r for r in search_invoices(limit=100) if r["name"] == created["name"]), None
		)
		self.assertIsNotNone(row, "the sale should be offered for return")
		self.assertEqual(float(row["grand_total"]), 0.0)
		self.assertFalse(row["fully_returned"])

	def test_unknown_item_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			create_invoice([{"item_code": "no-such-item-at-all", "qty": 1}])
