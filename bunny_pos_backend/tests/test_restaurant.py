# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Dine-in service: a table holds its order until somebody pays for it."""

import unittest

import frappe

from bunny_pos_backend.api.items import get_items
from bunny_pos_backend.api.restaurant import (
	bill_table,
	clear_table,
	get_kitchen_ticket,
	get_order,
	get_tables,
	save_order,
)


def _open_shift():
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"status": "Open", "docstatus": 1},
		fields=["name", "pos_profile", "user"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


class TestRestaurant(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.shift = _open_shift()
		if not cls.shift:
			raise unittest.SkipTest("no open shift on this site")

		cls.previous_user = frappe.session.user
		frappe.set_user(cls.shift.user)

		items = get_items(cls.shift.pos_profile, limit=40).get("items") or []
		cls.item = next(
			(i for i in items if i.get("is_stock_item") and (i.get("stock_qty") or 0) >= 10),
			None,
		) or next(iter(items), None)
		if not cls.item:
			frappe.set_user(cls.previous_user)
			raise unittest.SkipTest("no sellable item on this POS Profile")

		# A table of our own, so a real floor plan is never disturbed.
		cls.table = "ZZ-TEST-TABLE"
		if not frappe.db.exists("Bunny Restaurant Table", cls.table):
			frappe.get_doc(
				{
					"doctype": "Bunny Restaurant Table",
					"table_name": cls.table,
					"pos_profile": cls.shift.pos_profile,
					"section": "Testing",
					"seats": 2,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if getattr(cls, "table", None):
			try:
				clear_table(cls.table, pos_profile=cls.shift.pos_profile)
			except Exception:
				pass
			frappe.delete_doc("Bunny Restaurant Table", cls.table, ignore_permissions=True, force=True)
			frappe.db.commit()
		if getattr(cls, "previous_user", None):
			frappe.set_user(cls.previous_user)

	def setUp(self):
		clear_table(self.table, pos_profile=self.shift.pos_profile)

	def _cart(self, qty=2):
		return [{"item_code": self.item["item_code"], "qty": qty}]

	def _floor_row(self):
		floor = get_tables(self.shift.pos_profile)
		return next((t for t in floor["tables"] if t["table"] == self.table), None)

	def test_a_free_table_has_no_order(self):
		self.assertIsNone(get_order(self.table, pos_profile=self.shift.pos_profile))
		self.assertFalse(self._floor_row()["busy"])

	def test_an_order_survives_being_left_and_come_back_to(self):
		"""The whole point of keeping it on the server rather than in the till."""
		saved = save_order(self.table, self._cart(), pos_profile=self.shift.pos_profile)
		self.assertEqual(saved["line_count"], 1)

		again = get_order(self.table, pos_profile=self.shift.pos_profile)
		self.assertEqual(again["order"], saved["order"])
		self.assertEqual(again["items"][0]["qty"], 2)
		self.assertTrue(self._floor_row()["busy"])

	def test_saving_again_replaces_the_order_rather_than_adding_to_it(self):
		first = save_order(self.table, self._cart(2), pos_profile=self.shift.pos_profile)
		second = save_order(self.table, self._cart(5), pos_profile=self.shift.pos_profile)

		self.assertEqual(first["order"], second["order"], "a table must keep one order")
		self.assertEqual(second["line_count"], 1)
		self.assertEqual(second["items"][0]["qty"], 5)

	def test_emptying_an_order_frees_the_table(self):
		save_order(self.table, self._cart(), pos_profile=self.shift.pos_profile)
		self.assertIsNone(save_order(self.table, [], pos_profile=self.shift.pos_profile))
		self.assertFalse(self._floor_row()["busy"])

	def test_the_kitchen_gets_what_was_ordered(self):
		save_order(self.table, self._cart(3), pos_profile=self.shift.pos_profile)
		ticket = get_kitchen_ticket(self.table, pos_profile=self.shift.pos_profile)
		self.assertIn(self.item["item_name"], ticket["html"])
		self.assertIn(self.table, ticket["html"])

	def test_a_free_table_cannot_be_sent_to_the_kitchen(self):
		with self.assertRaises(frappe.ValidationError):
			get_kitchen_ticket(self.table, pos_profile=self.shift.pos_profile)

	def test_billing_frees_the_table(self):
		save_order(self.table, self._cart(2), pos_profile=self.shift.pos_profile)
		invoice = bill_table(
			self.table,
			pos_profile=self.shift.pos_profile,
			request_id=frappe.generate_hash(length=20),
		)
		self.assertTrue(invoice["name"])
		self.assertIsNone(get_order(self.table, pos_profile=self.shift.pos_profile))
		self.assertFalse(self._floor_row()["busy"])

	def test_billing_a_free_table_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			bill_table(self.table, pos_profile=self.shift.pos_profile)

	def test_the_same_bill_twice_charges_once(self):
		"""An impatient second press at the counter must not bill the party again."""
		save_order(self.table, self._cart(1), pos_profile=self.shift.pos_profile)
		request_id = frappe.generate_hash(length=20)

		first = bill_table(self.table, pos_profile=self.shift.pos_profile, request_id=request_id)
		# The table is free by now, so a retry has nothing to bill -- which is
		# itself the protection, and it must not raise a second invoice.
		with self.assertRaises(frappe.ValidationError):
			bill_table(self.table, pos_profile=self.shift.pos_profile, request_id=request_id)

		self.assertEqual(
			frappe.db.count("POS Invoice", {"remarks": ("like", f"%{request_id}%")}),
			1,
		)
		self.assertTrue(first["name"])

	def test_an_unknown_table_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			save_order("NO-SUCH-TABLE", self._cart(), pos_profile=self.shift.pos_profile)

	def test_a_line_with_no_quantity_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			save_order(
				self.table,
				[{"item_code": self.item["item_code"], "qty": 0}],
				pos_profile=self.shift.pos_profile,
			)
