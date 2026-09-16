# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""POS Invoice creation.

The client sends what was scanned and how it was paid. Pricing, taxes, stock
validation and GL posting are all left to ERPNext's own POS Invoice controller
so none of that logic ever lives on the till.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from bunny_pos_backend.api.utils import get_pos_profile, parse_json, pos_api


@frappe.whitelist(methods=["POST"])
@pos_api
def create_invoice(cart_data, customer=None, payments=None, pos_profile=None):
	"""Create and submit a POS Invoice for the caller's open shift.

	``cart_data`` is a list of ``{"item_code": ..., "qty": ...}`` rows. Any rate
	sent by the client is ignored on purpose -- the price list decides the rate
	server-side.

	``payments`` may be omitted (the whole invoice goes to the profile's default
	mode of payment), a mapping of mode to amount, or a list of
	``{"mode_of_payment": ..., "amount": ...}`` rows.
	"""
	cart = parse_json(cart_data, "cart_data", (list,))
	if not cart:
		frappe.throw(_("Bunny POS: the cart is empty."))

	profile = _resolve_profile(pos_profile)

	customer = customer or profile.customer
	if not customer:
		frappe.throw(
			_("Bunny POS: no customer supplied and POS Profile {0} has no default customer.").format(
				profile.name
			)
		)
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Bunny POS: customer {0} does not exist.").format(customer))

	doc = frappe.new_doc("POS Invoice")
	doc.company = profile.company
	doc.pos_profile = profile.name
	doc.customer = customer
	doc.is_pos = 1

	rows = _clean_cart(cart, profile)
	for row in rows:
		doc.append(
			"items",
			{k: v for k, v in row.items() if not k.startswith("_")},
		)

	# Pull price list, warehouse, taxes and payment modes off the POS Profile,
	# then price the cart, so the payable amount is known before it is paid.
	doc.set_missing_values()
	_apply_line_overrides(doc, rows, profile)
	doc.calculate_taxes_and_totals()

	requested = _parse_payments(payments)
	_apply_payments(doc, profile, requested)

	doc.insert()

	# validate() reprices the invoice; make sure a full auto-payment still
	# covers the final total before submitting.
	if not requested:
		total = flt(doc.rounded_total) or flt(doc.grand_total)
		if flt(doc.paid_amount, doc.precision("paid_amount")) != flt(
			total, doc.precision("paid_amount")
		):
			_apply_payments(doc, profile, None)
			doc.save()

	doc.submit()

	return _invoice_response(doc)


def _resolve_profile(pos_profile):
	"""A POS Invoice may only be raised against the caller's open shift."""
	shifts = frappe.get_all(
		"POS Opening Entry",
		filters={"user": frappe.session.user, "status": "Open", "docstatus": 1},
		fields=["name", "pos_profile"],
		order_by="creation desc",
		limit=1,
	)

	if not shifts:
		frappe.throw(_("Bunny POS: no open shift. Open a shift before billing."))

	shift = shifts[0]

	if pos_profile and pos_profile != shift.pos_profile:
		frappe.throw(
			_("Bunny POS: the open shift is on POS Profile {0}, not {1}.").format(
				shift.pos_profile, pos_profile
			)
		)

	return get_pos_profile(shift.pos_profile)


def _clean_cart(cart, profile):
	"""Validate the cart and reduce it to what the invoice needs.

	Keys prefixed with an underscore are held back for
	:func:`_apply_line_overrides`, which runs once ERPNext has priced the row.
	"""
	rows = []

	for idx, row in enumerate(cart, start=1):
		if not isinstance(row, dict):
			frappe.throw(_("Bunny POS: cart row {0} must be an object.").format(idx))

		item_code = row.get("item_code")
		if not item_code:
			frappe.throw(_("Bunny POS: cart row {0} has no item_code.").format(idx))

		if not frappe.db.exists("Item", item_code):
			frappe.throw(_("Bunny POS: item {0} does not exist.").format(item_code))

		qty = flt(row.get("qty"))
		if qty <= 0:
			frappe.throw(
				_("Bunny POS: item {0} needs a quantity greater than zero.").format(item_code)
			)

		uom, conversion_factor = _resolve_uom(item_code, row.get("uom"))

		clean = {
			"item_code": item_code,
			"qty": qty,
			"uom": uom,
			"conversion_factor": conversion_factor,
		}

		if row.get("discount_percentage") not in (None, ""):
			if not cint(profile.allow_discount_change):
				frappe.throw(
					_("Bunny POS: POS Profile {0} does not allow discounts to be changed.").format(
						profile.name
					)
				)
			discount = flt(row.get("discount_percentage"))
			if discount < 0 or discount > 100:
				frappe.throw(
					_("Bunny POS: discount on {0} must be between 0 and 100.").format(item_code)
				)
			clean["_discount_percentage"] = discount

		elif row.get("rate") not in (None, ""):
			if not cint(profile.allow_rate_change):
				frappe.throw(
					_("Bunny POS: POS Profile {0} does not allow the rate to be changed.").format(
						profile.name
					)
				)
			rate = flt(row.get("rate"))
			if rate < 0:
				frappe.throw(_("Bunny POS: rate for {0} cannot be negative.").format(item_code))
			clean["_rate"] = rate

		rows.append(clean)

	return rows


def _resolve_uom(item_code, uom):
	"""Return a valid UOM for the item and its conversion factor.

	The factor is always read from the Item, never taken from the client --
	it decides how much stock the sale consumes.
	"""
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")

	if not uom or uom == stock_uom:
		return stock_uom, 1.0

	factor = frappe.db.get_value(
		"UOM Conversion Detail",
		{"parent": item_code, "parenttype": "Item", "uom": uom},
		"conversion_factor",
	)

	if not factor:
		frappe.throw(
			_("Bunny POS: {0} cannot be sold in {1}.").format(item_code, uom)
		)

	return uom, flt(factor)


def _apply_line_overrides(doc, rows, profile):
	"""Apply cashier-entered discount or rate once ERPNext has priced the line.

	ERPNext keeps a rate that is already set, so the effective rate is worked
	out here rather than left to the pricing pass.
	"""
	for item, row in zip(doc.items, rows):
		price_list_rate = flt(item.price_list_rate)
		discount = row.get("_discount_percentage")
		rate = row.get("_rate")

		if discount is not None:
			item.discount_percentage = discount
			item.discount_amount = flt(
				price_list_rate * discount / 100.0, item.precision("discount_amount")
			)
			item.rate = flt(price_list_rate - item.discount_amount, item.precision("rate"))

		elif rate is not None:
			item.rate = flt(rate, item.precision("rate"))
			if price_list_rate:
				item.discount_amount = flt(
					price_list_rate - item.rate, item.precision("discount_amount")
				)
				item.discount_percentage = flt(
					item.discount_amount / price_list_rate * 100.0,
					item.precision("discount_percentage"),
				)


def _parse_payments(payments):
	"""Normalise the payments argument to ``{mode: amount or None}``."""
	payments = parse_json(payments, "payments", (dict, list))

	if not payments:
		return None

	requested = {}

	if isinstance(payments, dict):
		for mode, amount in payments.items():
			requested[mode] = flt(amount)
	else:
		for idx, row in enumerate(payments, start=1):
			if not isinstance(row, dict):
				frappe.throw(_("Bunny POS: payment row {0} must be an object.").format(idx))

			mode = row.get("mode_of_payment")
			if not mode:
				frappe.throw(_("Bunny POS: payment row {0} has no mode_of_payment.").format(idx))

			amount = row.get("amount")
			requested[mode] = None if amount in (None, "") else flt(amount)

	return requested


def _apply_payments(doc, profile, requested):
	"""Write payment amounts onto the rows set up from the POS Profile."""
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	precision = doc.precision("paid_amount")

	if not doc.get("payments"):
		frappe.throw(
			_("Bunny POS: POS Profile {0} has no mode of payment configured.").format(profile.name)
		)

	for row in doc.payments:
		row.amount = 0
		row.base_amount = 0

	if not requested:
		# Whole amount on the profile's default mode of payment.
		target = next((d for d in doc.payments if cint(d.default)), doc.payments[0])
		target.amount = flt(total, precision)
		doc.paid_amount = target.amount
		return

	unknown = [mode for mode in requested if mode not in [d.mode_of_payment for d in doc.payments]]
	if unknown:
		frappe.throw(
			_("Bunny POS: mode of payment {0} is not configured on POS Profile {1}.").format(
				", ".join(unknown), profile.name
			)
		)

	# A single mode with no amount means "take the whole invoice".
	if len(requested) == 1 and next(iter(requested.values())) is None:
		requested = {next(iter(requested)): total}

	paid = 0.0
	for row in doc.payments:
		if row.mode_of_payment in requested:
			amount = requested[row.mode_of_payment]
			if amount is None:
				frappe.throw(
					_("Bunny POS: an amount is required for mode of payment {0}.").format(
						row.mode_of_payment
					)
				)
			row.amount = flt(amount, precision)
			paid += row.amount

	if flt(paid, precision) < flt(total, precision) and not cint(profile.allow_partial_payment):
		frappe.throw(
			_("Bunny POS: payments total {0} but the invoice is {1}. Partial payment is not allowed.").format(
				flt(paid, precision), flt(total, precision)
			)
		)

	doc.paid_amount = flt(paid, precision)


def _invoice_response(doc):
	return {
		"name": doc.name,
		"status": doc.status,
		"customer": doc.customer,
		"pos_profile": doc.pos_profile,
		"company": doc.company,
		"currency": doc.currency,
		"posting_date": str(doc.posting_date),
		"net_total": flt(doc.net_total),
		"total_taxes_and_charges": flt(doc.total_taxes_and_charges),
		"grand_total": flt(doc.grand_total),
		"rounded_total": flt(doc.rounded_total),
		"paid_amount": flt(doc.paid_amount),
		"change_amount": flt(doc.change_amount),
		"items": [
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"qty": flt(d.qty),
				"uom": d.uom,
				"conversion_factor": flt(d.conversion_factor),
				"rate": flt(d.rate),
				"price_list_rate": flt(d.price_list_rate),
				"discount_percentage": flt(d.discount_percentage),
				"amount": flt(d.amount),
			}
			for d in doc.items
		],
		"payments": [
			{"mode_of_payment": d.mode_of_payment, "amount": flt(d.amount)} for d in doc.payments
		],
	}

# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


@frappe.whitelist()
@pos_api
def get_receipt_html(invoice):
	"""The receipt as ERPNext would print it, when the profile names a format.

	If the POS Profile has a Print Format, that is what the customer gets --
	letter head, terms and print heading included. With no format set this
	returns nothing and the till falls back to its own built-in layout.
	"""
	if not frappe.db.exists("POS Invoice", invoice):
		frappe.throw(_("Bunny POS: invoice {0} does not exist.").format(invoice))

	pos_profile = frappe.db.get_value("POS Invoice", invoice, "pos_profile")
	profile = get_pos_profile(pos_profile)

	if not profile.print_format:
		return {"html": None, "print_format": None}

	html = frappe.get_print(
		"POS Invoice",
		invoice,
		print_format=profile.print_format,
		letterhead=profile.letter_head or None,
		no_letterhead=0 if profile.letter_head else 1,
	)

	return {
		"html": html,
		"print_format": profile.print_format,
		"letter_head": profile.letter_head or "",
	}


@frappe.whitelist()
@pos_api
def search_invoices(pos_profile=None, search_term=None, limit=20):
	"""Recent sales that could be returned, newest first."""
	profile = _resolve_profile(pos_profile)

	limit = max(1, min(cint(limit) or 20, 100))
	conditions = ["pinv.docstatus = 1", "pinv.is_return = 0", "pinv.company = %(company)s"]
	params = {"company": profile.company, "limit": limit}

	if search_term:
		search_term = str(search_term).strip()

	if search_term:
		conditions.append("(pinv.name like %(like)s or pinv.customer like %(like)s)")
		params["like"] = f"%{search_term}%"

	rows = frappe.db.sql(
		"""
		select
			pinv.name,
			pinv.customer,
			pinv.customer_name,
			pinv.posting_date,
			pinv.currency,
			pinv.grand_total,
			pinv.rounded_total,
			pinv.owner,
			pinv.pos_profile,
			ifnull((
				select sum(r.grand_total)
				from `tabPOS Invoice` r
				where r.return_against = pinv.name
					and r.docstatus = 1
					and r.is_return = 1
			), 0) as returned_total
		from `tabPOS Invoice` pinv
		where {conditions}
		order by pinv.creation desc
		limit %(limit)s
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)

	# Returns are stored as negative totals, so adding them gives what is left.
	# The till shows this so a cashier isn't surprised by a part-returned sale.
	for row in rows:
		total = flt(row.rounded_total) or flt(row.grand_total)
		row["returned_amount"] = abs(flt(row.returned_total))
		row["refundable_amount"] = max(0.0, flt(total) + flt(row.returned_total))
		row["fully_returned"] = row["refundable_amount"] <= 0
		row.pop("returned_total", None)

	return rows


@frappe.whitelist()
@pos_api
def get_invoice(invoice):
	"""One sale with per-line quantities that are still returnable."""
	if not frappe.db.exists("POS Invoice", invoice):
		frappe.throw(_("Bunny POS: invoice {0} does not exist.").format(invoice))

	doc = frappe.get_doc("POS Invoice", invoice)

	if doc.docstatus != 1:
		frappe.throw(_("Bunny POS: invoice {0} is not submitted.").format(invoice))
	if cint(doc.is_return):
		frappe.throw(_("Bunny POS: {0} is already a return.").format(invoice))

	returned = _returned_qty(invoice)

	items = []
	for row in doc.items:
		already = flt(returned.get(row.name))
		items.append(
			{
				"row": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": flt(row.qty),
				"returned_qty": already,
				"returnable_qty": flt(row.qty) - already,
				"uom": row.uom,
				"rate": flt(row.rate),
				"amount": flt(row.amount),
			}
		)

	return {
		"name": doc.name,
		"customer": doc.customer,
		"customer_name": doc.customer_name,
		"posting_date": str(doc.posting_date),
		"currency": doc.currency,
		"pos_profile": doc.pos_profile,
		"grand_total": flt(doc.grand_total),
		"rounded_total": flt(doc.rounded_total),
		"paid_amount": flt(doc.paid_amount),
		"items": items,
		"payments": [
			{"mode_of_payment": d.mode_of_payment, "amount": flt(d.amount)}
			for d in doc.payments
			if d.amount
		],
		"fully_returned": all(i["returnable_qty"] <= 0 for i in items),
	}


@frappe.whitelist(methods=["POST"])
@pos_api
def create_return(invoice, items=None, payments=None):
	"""Create and submit a return against a previous sale.

	``items`` is a list of ``{"row": <source row id>, "qty": <positive qty>}``.
	Omit it to return the whole sale. Quantities are sent positive and written
	to the return as negatives, which is the shape ERPNext expects.
	"""
	profile = _resolve_profile(None)
	source = frappe.get_doc("POS Invoice", invoice)

	if source.docstatus != 1:
		frappe.throw(_("Bunny POS: invoice {0} is not submitted.").format(invoice))
	if cint(source.is_return):
		frappe.throw(_("Bunny POS: {0} is already a return.").format(invoice))
	if source.company != profile.company:
		frappe.throw(
			_("Bunny POS: {0} belongs to another company.").format(invoice)
		)

	wanted = _parse_return_items(source, items)

	doc = frappe.new_doc("POS Invoice")
	doc.company = source.company
	doc.pos_profile = profile.name
	doc.customer = source.customer
	doc.currency = source.currency
	doc.selling_price_list = source.selling_price_list
	doc.is_pos = 1
	doc.is_return = 1
	doc.return_against = source.name
	doc.ignore_pricing_rule = 1

	by_name = {row.name: row for row in source.items}
	for row_name, qty in wanted.items():
		src = by_name[row_name]
		doc.append(
			"items",
			{
				"item_code": src.item_code,
				"item_name": src.item_name,
				"qty": -abs(qty),
				"uom": src.uom,
				"stock_uom": src.stock_uom,
				"conversion_factor": src.conversion_factor,
				"rate": src.rate,
				"price_list_rate": src.price_list_rate,
				"discount_percentage": src.discount_percentage,
				"warehouse": src.warehouse,
				"income_account": src.income_account,
				"expense_account": src.expense_account,
				"cost_center": src.cost_center,
				"pos_invoice_item": src.name,
			},
		)

	doc.set_missing_values()
	doc.calculate_taxes_and_totals()

	_apply_refund(doc, profile, payments)

	doc.insert()
	doc.submit()

	return _invoice_response(doc)


def _returned_qty(invoice):
	"""Qty already returned against each row of a sale, keyed by source row."""
	rows = frappe.db.sql(
		"""
		select child.pos_invoice_item as row_name, sum(child.qty) as qty
		from `tabPOS Invoice` pinv, `tabPOS Invoice Item` child
		where pinv.name = child.parent
			and pinv.docstatus = 1
			and pinv.is_return = 1
			and pinv.return_against = %(invoice)s
			and ifnull(child.pos_invoice_item, '') != ''
		group by child.pos_invoice_item
		""",
		{"invoice": invoice},
		as_dict=True,
	)
	# Return quantities are stored negative; report them as positive.
	return {row.row_name: abs(flt(row.qty)) for row in rows}


def _parse_return_items(source, items):
	"""Work out which rows to return and how much of each."""
	returned = _returned_qty(source.name)
	by_name = {row.name: row for row in source.items}

	items = parse_json(items, "items", (list,))

	if not items:
		# No selection means return everything still outstanding.
		wanted = {
			row.name: flt(row.qty) - flt(returned.get(row.name))
			for row in source.items
			if flt(row.qty) - flt(returned.get(row.name)) > 0
		}
		if not wanted:
			frappe.throw(_("Bunny POS: every line on {0} has already been returned.").format(source.name))
		return wanted

	wanted = {}
	for idx, entry in enumerate(items, start=1):
		if not isinstance(entry, dict):
			frappe.throw(_("Bunny POS: return row {0} must be an object.").format(idx))

		row_name = entry.get("row")
		if row_name not in by_name:
			frappe.throw(_("Bunny POS: return row {0} is not part of {1}.").format(idx, source.name))

		qty = flt(entry.get("qty"))
		if qty <= 0:
			continue

		src = by_name[row_name]
		outstanding = flt(src.qty) - flt(returned.get(row_name))
		if qty > outstanding:
			frappe.throw(
				_("Bunny POS: only {0} of {1} can still be returned.").format(
					outstanding, src.item_code
				)
			)
		wanted[row_name] = qty

	if not wanted:
		frappe.throw(_("Bunny POS: nothing selected to return."))

	return wanted


def _apply_refund(doc, profile, payments):
	"""Set the refund rows. A return must be settled exactly, never partly."""
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	precision = doc.precision("paid_amount")

	if not doc.get("payments"):
		frappe.throw(
			_("Bunny POS: POS Profile {0} has no mode of payment configured.").format(profile.name)
		)

	for row in doc.payments:
		row.amount = 0
		row.base_amount = 0

	requested = _parse_payments(payments)

	if not requested:
		target = next((d for d in doc.payments if cint(d.default)), doc.payments[0])
		target.amount = flt(total, precision)
		doc.paid_amount = target.amount
		return

	available = [d.mode_of_payment for d in doc.payments]
	unknown = [mode for mode in requested if mode not in available]
	if unknown:
		frappe.throw(
			_("Bunny POS: mode of payment {0} is not configured on POS Profile {1}.").format(
				", ".join(unknown), profile.name
			)
		)

	if len(requested) == 1 and next(iter(requested.values())) is None:
		requested = {next(iter(requested)): total}

	refunded = 0.0
	for row in doc.payments:
		if row.mode_of_payment in requested:
			amount = requested[row.mode_of_payment]
			if amount is None:
				frappe.throw(
					_("Bunny POS: an amount is required for mode of payment {0}.").format(
						row.mode_of_payment
					)
				)
			# Accept a positive refund amount and store it the way ERPNext wants.
			row.amount = -abs(flt(amount, precision))
			refunded += row.amount

	if flt(refunded, precision) != flt(total, precision):
		frappe.throw(
			_("Bunny POS: the refund must come to {0}, not {1}.").format(
				flt(total, precision), flt(refunded, precision)
			)
		)

	doc.paid_amount = flt(refunded, precision)
