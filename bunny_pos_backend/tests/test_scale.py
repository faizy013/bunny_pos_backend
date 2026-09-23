# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Labels printed by a weighing scale."""

import unittest

import frappe

from bunny_pos_backend.api.items import get_items, scan


def _open_shift():
	rows = frappe.get_all(
		"POS Opening Entry",
		filters={"status": "Open", "docstatus": 1},
		fields=["name", "pos_profile", "user"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _ean13(body):
	"""Add the check digit a scale would print, so the codes are realistic."""
	digits = [int(d) for d in body]
	total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(reversed(digits)))
	return body + str((10 - total % 10) % 10)


class TestScaleLabels(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.shift = _open_shift()
		if not cls.shift:
			raise unittest.SkipTest("no open shift on this site")
		cls.previous_user = frappe.session.user
		frappe.set_user(cls.shift.user)

		items = get_items(cls.shift.pos_profile, limit=60).get("items") or []
		cls.item = next((i for i in items if i["item_code"] == "BUNNY-CHICKEN"), None)
		if not cls.item:
			frappe.set_user(cls.previous_user)
			raise unittest.SkipTest("no weighed item set up on this site")

		cls.settings = frappe.get_single("Scale Barcode Settings")
		cls.was = {
			f: cls.settings.get(f)
			for f in ("enabled", "prefixes", "code_digits", "value_digits", "value_is", "weight_uom")
		}

	@classmethod
	def tearDownClass(cls):
		if getattr(cls, "was", None):
			for field, value in cls.was.items():
				cls.settings.set(field, value)
			cls.settings.save(ignore_permissions=True)
			frappe.db.commit()
		if getattr(cls, "previous_user", None):
			frappe.set_user(cls.previous_user)

	def _configure(self, value_is="Price", prefixes="2", code_digits=5, value_digits=5, enabled=1):
		self.settings.enabled = enabled
		self.settings.prefixes = prefixes
		self.settings.code_digits = code_digits
		self.settings.value_digits = value_digits
		self.settings.value_is = value_is
		self.settings.weight_uom = "Kg"
		self.settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Scale Barcode Settings", "Scale Barcode Settings")

	def test_a_price_label_becomes_the_weight_it_paid_for(self):
		"""The line has to come to the figure printed on the label."""
		self._configure(value_is="Price")
		result = scan(self.shift.pos_profile, _ean13("201234" + "62500"))

		rate = float(result["item"]["rate"])
		self.assertGreater(rate, 0)
		self.assertTrue(result["weighed"])
		self.assertAlmostEqual(result["qty"] * rate, 625.00, places=2)

	def test_a_weight_label_is_read_in_grams(self):
		self._configure(value_is="Weight")
		result = scan(self.shift.pos_profile, _ean13("201234" + "01250"))
		self.assertAlmostEqual(result["qty"], 1.25, places=3)

	def test_an_ordinary_barcode_is_left_alone(self):
		self._configure(value_is="Price")
		result = scan(self.shift.pos_profile, "8964000112236")
		self.assertFalse(result["weighed"])
		self.assertEqual(result["qty"], 1)

	def test_a_label_for_an_item_nobody_knows_is_refused(self):
		self._configure(value_is="Price")
		with self.assertRaises(frappe.ValidationError):
			scan(self.shift.pos_profile, _ean13("209999" + "62500"))

	def test_nothing_is_read_as_a_label_while_it_is_switched_off(self):
		"""A shop with no scale must not have ordinary barcodes reinterpreted."""
		self._configure(enabled=0)
		with self.assertRaises(frappe.ValidationError):
			scan(self.shift.pos_profile, _ean13("201234" + "62500"))

	def test_another_prefix_is_not_a_label(self):
		self._configure(prefixes="2")
		with self.assertRaises(frappe.ValidationError):
			# Starts with 7, so it is an ordinary product barcode.
			scan(self.shift.pos_profile, _ean13("701234" + "62500"))

	def test_a_short_code_is_not_a_label(self):
		self._configure()
		with self.assertRaises(frappe.ValidationError):
			scan(self.shift.pos_profile, "2012")

	def test_the_shape_is_configurable(self):
		"""Scales differ by country, so the digits have to be told, not guessed."""
		self._configure(prefixes="21", code_digits=4, value_digits=5, value_is="Weight")
		result = scan(self.shift.pos_profile, _ean13("21" + "1234" + "02000"))
		self.assertAlmostEqual(result["qty"], 2.0, places=3)
