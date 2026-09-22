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
def create_invoice(
	cart_data,
	customer=None,
	payments=None,
	pos_profile=None,
	request_id=None,
	coupon_code=None,
	loyalty_points=None,
):
	"""Create and submit a POS Invoice for the caller's open shift.

	``cart_data`` is a list of ``{"item_code": ..., "qty": ...}`` rows. A row may
	also carry a rate or a discount, but only a POS Profile that allows those
	changes will accept them; otherwise the price list decides, server-side.

	``payments`` may be omitted (the whole invoice goes to the profile's default
	mode of payment), a mapping of mode to amount, or a list of
	``{"mode_of_payment": ..., "amount": ...}`` rows.

	``coupon_code`` is the code printed on the voucher; the matching pricing
	rule is applied by ERPNext. ``loyalty_points`` redeems that many of the
	customer's points against this sale.

	``request_id`` makes the call idempotent. A till that loses the reply to a
	successful sale will retry it, and without this that retry bills the
	customer a second time. Send the same id for every retry of one sale and a
	new one for the next sale; the first call wins and the rest get the invoice
	it made.
	"""
	cart = parse_json(cart_data, "cart_data", (list,))
	if not cart:
		frappe.throw(_("Bunny POS: the cart is empty."))

	request_id = (str(request_id or "").strip() or None)
	if request_id:
		settled = _settled_invoice(request_id)
		if settled:
			return _invoice_response(frappe.get_doc("POS Invoice", settled))
		_claim_request(request_id)

	try:
		doc = _build_and_submit(
			cart, customer, pos_profile, payments, request_id, coupon_code, loyalty_points
		)
	except Exception:
		# The sale did not happen, so the till must be free to try again with
		# the same id rather than being told its own retry is a duplicate.
		if request_id:
			frappe.cache().delete(_request_key(request_id))
		raise

	return _invoice_response(doc)


def _price_cart(cart, customer, pos_profile, coupon_code=None, loyalty_points=None):
	"""Build and price a POS Invoice without saving it.

	Shared by the sale itself and by the preview the till shows once a coupon
	or some points have been entered -- the cashier has to be able to read the
	new total out to the customer before taking the money, and only the server
	knows what a pricing rule did.
	"""
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

	# The coupon has to be on the document before pricing, because it decides
	# what the items cost.
	_apply_coupon(doc, coupon_code, customer)

	# Pull price list, warehouse, taxes and payment modes off the POS Profile,
	# then price the cart, so the payable amount is known before it is paid.
	doc.set_missing_values()
	_apply_line_overrides(doc, rows, profile)

	# ERPNext only applies a transaction-level pricing rule -- which is what a
	# coupon is -- during validate. The preview never gets that far, so it is
	# asked for here and the totals below are the ones the customer will pay.
	if not cint(doc.get("ignore_pricing_rule")):
		from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

		apply_pricing_rule_on_transaction(doc)

	doc.calculate_taxes_and_totals()

	# Only now is there a total for the points to be measured against.
	redeemed = _redeem_loyalty(doc, loyalty_points)

	return doc, profile, redeemed


def _build_and_submit(
	cart, customer, pos_profile, payments, request_id, coupon_code=None, loyalty_points=None
):
	doc, profile, redeemed = _price_cart(cart, customer, pos_profile, coupon_code, loyalty_points)

	requested = _parse_payments(payments)
	_apply_payments(doc, profile, requested)

	doc.insert()

	# validate() reprices the invoice; make sure a full auto-payment still
	# covers the final total before submitting.
	if not requested:
		total = _amount_due(doc)
		if flt(doc.paid_amount, doc.precision("paid_amount")) != flt(
			total, doc.precision("paid_amount")
		):
			_apply_payments(doc, profile, None)
			doc.save()

	doc.submit()

	if redeemed:
		_take_loyalty_points(doc)

	if request_id:
		_mark_settled(doc, request_id)

	return doc


def _apply_coupon(doc, code, customer):
	"""Attach a coupon so ERPNext's pricing rules discount the sale."""
	code = str(code or "").strip()
	if not code:
		return

	doc.coupon_code = _resolve_coupon(code, customer)["name"]


def _redeem_loyalty(doc, points):
	"""Take points off the bill as a discount, and return how many were used.

	ERPNext's own redemption flag cannot be used on a POS Invoice: the
	full-payment check measures the tendered rows against the whole total and
	never subtracts what points covered, so the sale refuses to submit. Taking
	the value off as a discount instead leaves a bill the customer really does
	pay in full, and reads plainly on the receipt. The points themselves are
	still removed through ERPNext, once the invoice exists to charge them
	against.
	"""
	from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
		get_loyalty_program_details_with_points,
	)

	points = cint(points)
	if points <= 0:
		return 0

	# One offer per sale. A coupon is a transaction pricing rule, and ERPNext
	# rebuilds its discount from a percentage on every validate -- which quietly
	# drops anything added alongside it while the points are still taken off the
	# customer. Refusing the combination is the only way to be sure that cannot
	# happen; stacking them would have to be settled in ERPNext, not here.
	if doc.get("coupon_code"):
		frappe.throw(
			_("Bunny POS: a coupon and loyalty points cannot be used on the same sale.")
		)

	program = frappe.db.get_value("Customer", doc.customer, "loyalty_program")
	if not program:
		frappe.throw(
			_("Bunny POS: {0} is not on a loyalty programme.").format(doc.customer_name or doc.customer)
		)

	details = (
		get_loyalty_program_details_with_points(
			doc.customer, company=doc.company, loyalty_program=program, silent=True
		)
		or {}
	)

	available = cint(details.get("loyalty_points"))
	if points > available:
		frappe.throw(
			_("Bunny POS: only {0} points are available, not {1}.").format(available, points)
		)

	value = flt(points * flt(details.get("conversion_factor")), doc.precision("grand_total"))
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	if value > total:
		frappe.throw(
			_("Bunny POS: those points are worth more than the sale. Redeem fewer.")
		)

	doc.loyalty_program = program
	doc.loyalty_points = points

	# loyalty_amount is deliberately left alone. ERPNext works out points
	# earned from `grand_total - loyalty_amount`, and the discount below has
	# already taken the points off grand_total -- setting it too would subtract
	# them twice and hand the customer a negative earning.
	doc.apply_discount_on = "Grand Total"
	doc.discount_amount = flt(doc.discount_amount) + value
	doc.calculate_taxes_and_totals()
	return points


def _amount_due(doc):
	"""What has to be tendered. Points already came off as a discount."""
	return flt(doc.rounded_total) or flt(doc.grand_total)


def _take_loyalty_points(doc):
	"""Charge the redeemed points to the customer's balance.

	Runs after submit because ERPNext writes the entries against the invoice,
	which has to exist first.
	"""
	if not cint(doc.get("loyalty_points")):
		return
	doc.apply_loyalty_points()


REQUEST_PREFIX = "bunny-pos:request:"
REQUEST_TTL_SECONDS = 30 * 60
REMARK_TAG = "Bunny POS request:"


def _request_key(request_id):
	"""Raw Redis key, site-scoped by hand.

	The raw redis methods are used throughout rather than frappe's set_value /
	get_value helpers: those namespace the key themselves, and only the raw
	call takes the NX flag this needs. Mixing the two would read a different
	key than it wrote.
	"""
	return f"{REQUEST_PREFIX}{frappe.local.site}:{request_id}"


def _claim_request(request_id):
	"""Let exactly one caller through for a given request id.

	SET NX is atomic, so two tills -- or one till retrying while the first
	attempt is still running -- cannot both pass this point.
	"""
	won = frappe.cache().set(_request_key(request_id), b"working", nx=True, ex=REQUEST_TTL_SECONDS)
	if not won:
		frappe.throw(
			_("Bunny POS: this sale is already being billed. Wait a moment and check the sale list."),
			title=_("Duplicate request"),
		)


def _mark_settled(doc, request_id):
	"""Record the id on the invoice itself so the guard outlives Redis.

	``remarks`` is a standard POS Invoice field that nothing else here writes,
	which keeps this working on a stock ERPNext with no customisation.
	"""
	tag = f"{REMARK_TAG} {request_id}"
	remarks = (doc.remarks or "").strip()
	doc.db_set("remarks", f"{remarks}\n{tag}".strip() if remarks else tag, update_modified=False)
	frappe.cache().set(_request_key(request_id), doc.name, ex=REQUEST_TTL_SECONDS)


def _settled_invoice(request_id):
	"""The invoice already raised for this id, or None."""
	cached = frappe.cache().get(_request_key(request_id))
	if isinstance(cached, bytes):
		cached = cached.decode()
	if cached and cached != "working":
		return cached

	# Redis can be restarted between the sale and the retry, so fall back to
	# the invoice. Scoped to this cashier's recent sales to keep it cheap.
	rows = frappe.get_all(
		"POS Invoice",
		filters={
			"owner": frappe.session.user,
			"docstatus": 1,
			"creation": (">", frappe.utils.add_to_date(None, hours=-24)),
			"remarks": ("like", f"%{REMARK_TAG} {request_id}%"),
		},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None


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
				precision = item.precision("discount_percentage")
				percentage = flt(item.discount_amount / price_list_rate * 100.0, precision)
				# ERPNext reads a discount of exactly 100% as "free" and forces the
				# rate to zero. A rate just above zero rounds up to 100 here, so a
				# cashier keying 0.01 would hand the item over for nothing. Hold it
				# just under instead, and keep discount_amount as the true figure.
				if percentage >= 100 and item.rate > 0:
					percentage = flt(100 - 10**-precision, precision)
				item.discount_percentage = percentage


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
	"""Write payment amounts onto the rows set up from the POS Profile.

	The rows carry what the customer actually hands over. ``paid_amount`` has
	to carry that plus anything points covered, because ERPNext only folds the
	redeemed amount into paid_amount for a Sales Invoice -- a POS Invoice keeps
	whatever we set, and its full-payment check measures it against the whole
	total.
	"""
	total = _amount_due(doc)
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
			), 0) as returned_total,
			ifnull((
				select sum(pi.qty)
				from `tabPOS Invoice Item` pi
				where pi.parent = pinv.name
			), 0) as sold_qty,
			ifnull((
				select sum(ri.qty)
				from `tabPOS Invoice Item` ri
				inner join `tabPOS Invoice` r on r.name = ri.parent
				where r.return_against = pinv.name
					and r.docstatus = 1
					and r.is_return = 1
			), 0) as returned_qty
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

		# Judged on quantity, not money. A sale that came to zero -- everything
		# discounted, or a giveaway -- has nothing left to refund by value, and
		# reading that as "fully returned" locked the cashier out of returning
		# goods that were never brought back.
		sold = flt(row.sold_qty)
		returned = abs(flt(row.returned_qty))
		row["fully_returned"] = sold > 0 and returned >= sold

		row.pop("returned_total", None)
		row.pop("sold_qty", None)
		row.pop("returned_qty", None)

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


@frappe.whitelist()
@pos_api
def check_coupon(code, customer=None, pos_profile=None):
	"""Look a typed coupon up and say plainly whether it can be used.

	The cashier types the code printed on the voucher, which is not the
	document's name, so it is resolved here. ERPNext's own validation decides
	whether it is in date and has uses left; this only turns its complaint into
	something readable at a till.
	"""
	profile = get_pos_profile(pos_profile)
	found = _resolve_coupon(code, customer)
	found["currency"] = profile.currency
	return found


def _resolve_coupon(code, customer=None):
	"""Turn a typed code into a usable Coupon Code, or say why it is not.

	Kept apart from the endpoint because the sale itself needs this too, and
	the sale already knows its POS Profile.
	"""
	from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code

	code = str(code or "").strip()
	if not code:
		frappe.throw(_("Bunny POS: no coupon code was entered."))

	name = frappe.db.get_value("Coupon Code", {"coupon_code": code}, "name") or (
		frappe.db.get_value("Coupon Code", {"name": code}, "name")
	)
	if not name:
		frappe.throw(_("Bunny POS: {0} is not a coupon we know.").format(code))

	coupon = frappe.get_doc("Coupon Code", name)

	# A coupon issued to one customer is not a coupon for anyone who finds it.
	if coupon.customer and customer and coupon.customer != customer:
		frappe.throw(_("Bunny POS: this coupon belongs to another customer."))

	try:
		validate_coupon_code(name)
	except Exception as err:
		frappe.throw(_("Bunny POS: {0}").format(str(err) or _("this coupon cannot be used.")))

	return {
		"name": coupon.name,
		"code": coupon.coupon_code,
		"title": coupon.coupon_name,
		"type": coupon.coupon_type,
		"pricing_rule": coupon.pricing_rule,
		"valid_upto": coupon.valid_upto,
		"uses_left": (cint(coupon.maximum_use) - cint(coupon.used)) if coupon.maximum_use else None,
	}


@frappe.whitelist()
@pos_api
def quote(cart_data, customer=None, pos_profile=None, coupon_code=None, loyalty_points=None):
	"""Price a cart without selling it.

	A coupon's discount comes out of a pricing rule, which only the server can
	work out, so the till cannot show the customer what they owe until it asks.
	Nothing here is saved.
	"""
	cart = parse_json(cart_data, "cart_data", (list,))
	if not cart:
		frappe.throw(_("Bunny POS: the cart is empty."))

	doc, profile, redeemed = _price_cart(cart, customer, pos_profile, coupon_code, loyalty_points)

	return {
		"currency": doc.currency or profile.currency,
		"net_total": flt(doc.net_total),
		"total": flt(doc.total),
		"discount_amount": flt(doc.discount_amount),
		"grand_total": flt(doc.grand_total),
		"rounded_total": flt(doc.rounded_total),
		"amount_due": _amount_due(doc),
		"coupon": doc.coupon_code or None,
		"loyalty_points": cint(redeemed),
		"loyalty_amount": flt(doc.get("loyalty_amount")),
	}
