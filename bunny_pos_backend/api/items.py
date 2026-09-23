# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Item catalogue for the POS screen.

One call returns code, name, price and sellable stock already stitched
together, so the client never has to fan out over several resource APIs.
Paged and filterable, so a site with thousands of items stays workable.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from bunny_pos_backend.api.utils import get_pos_profile, parse_json, pos_api, profile_context

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_STOCK_CODES = 400


@frappe.whitelist()
@pos_api
def get_items(pos_profile, search_term=None, item_group=None, start=0, limit=None):
	"""Return sellable items for a POS Profile with price and stock resolved.

	Price comes from the profile's selling price list, stock from the Bin for
	the profile's warehouse less anything held by unconsolidated POS Invoices.
	Both are looked up server-side and merged into a single response.
	"""
	profile = get_pos_profile(pos_profile)

	limit = cint(limit) or DEFAULT_LIMIT
	limit = max(1, min(limit, MAX_LIMIT))
	start = max(0, cint(start))

	# Fetch one extra row to tell the client whether another page exists.
	items = _fetch_items(profile, search_term, item_group, start, limit + 1)
	has_more = len(items) > limit
	items = items[:limit]

	if items:
		item_codes = [d.item_code for d in items]
		prices = _get_prices(profile, item_codes)
		stock = _get_stock(profile, item_codes)
		uoms = _get_uoms(item_codes)

		for item in items:
			item.uoms = uoms.get(item.item_code) or [
				{"uom": item.stock_uom, "conversion_factor": 1.0}
			]
			price = prices.get(item.item_code) or {}
			item.rate = flt(price.get("price_list_rate"))
			item.price_list_rate = item.rate
			item.currency = price.get("currency") or profile.currency
			item.stock_qty = flt(stock.get(item.item_code))
			item.uom = item.stock_uom

		if cint(profile.hide_unavailable_items):
			items = [d for d in items if not cint(d.is_stock_item) or d.stock_qty > 0]

	context = profile_context(profile)
	context.update({"items": items, "start": start, "limit": limit, "has_more": has_more})

	return context


def _scale_label(code):
	"""Read a label a weighing scale printed, or return None.

	These carry the item's own number and either what it weighed or what it
	came to, so the till can put the right quantity on the line instead of
	asking the cashier to key it. The shape differs by scale, which is why it
	is configured rather than guessed.
	"""
	settings = frappe.get_cached_doc("Scale Barcode Settings")
	if not cint(settings.enabled) or not code.isdigit():
		return None

	prefixes = [p.strip() for p in (settings.prefixes or "").split(",") if p.strip()]
	prefix = next((p for p in sorted(prefixes, key=len, reverse=True) if code.startswith(p)), None)
	if not prefix:
		return None

	digits = cint(settings.code_digits)
	values = cint(settings.value_digits)
	if len(code) < len(prefix) + digits + values:
		return None

	at = len(prefix)
	item_number = code[at : at + digits]
	raw = code[at + digits : at + digits + values]
	if not raw.isdigit():
		return None

	return {
		"item_number": item_number,
		# Scales count in whole small units: paise for money, grams for weight.
		"units": cint(raw),
		"is_price": settings.value_is != "Weight",
		"weight_uom": settings.weight_uom or "Kg",
	}


def _scale_qty(item, label):
	"""How much of the item the label stands for.

	A label that carries a weight says so outright. One that carries a price
	says what it came to, and the quantity is whatever that buys at the
	shelf price -- which is how the line still adds up to the printed figure.
	"""
	if not label["is_price"]:
		grams = flt(label["units"])
		if (label["weight_uom"] or "").lower() in ("gram", "g", "grams"):
			return grams
		return flt(grams / 1000.0, 3)

	price = flt(label["units"]) / 100.0
	rate = flt(item.rate)
	if rate <= 0:
		frappe.throw(
			_("Bunny POS: {0} has no price, so a label worth {1} cannot be weighed out.").format(
				item.item_code, price
			)
		)
	return flt(price / rate, 3)


def _item_for_number(number):
	"""The item whose barcode is the number the scale printed.

	Leading zeros are ignored on both sides. A shop writes 1234 against the
	item, and how many zeros the scale pads it with depends on how many digits
	the label was set up for -- neither should have to match the other.
	"""
	exact = frappe.db.get_value("Item Barcode", {"barcode": number}, "parent")
	if exact:
		return exact

	trimmed = number.lstrip("0")
	if not trimmed:
		return None

	rows = frappe.db.sql(
		"""
		select parent
		from `tabItem Barcode`
		where barcode regexp '^[0-9]+$' and cast(barcode as unsigned) = %(value)s
		limit 1
		""",
		{"value": int(trimmed)},
	)
	return rows[0][0] if rows else None


@frappe.whitelist()
@pos_api
def scan(pos_profile, code):
	"""Resolve a scanned barcode, serial number or batch to a sellable item.

	Uses ERPNext's own ``scan_barcode``, so whatever a shop has already set up
	on Item Barcode, Serial No or Batch works here with no extra configuration.
	A barcode that names a UOM -- a case barcode, say -- sells in that UOM.
	"""
	from erpnext.stock.utils import scan_barcode

	profile = get_pos_profile(pos_profile)

	code = str(code or "").strip()
	if not code:
		frappe.throw(_("Bunny POS: nothing was scanned."))

	label = _scale_label(code)
	found = {}
	item_code = None

	if label:
		item_code = _item_for_number(label["item_number"])
		if not item_code:
			frappe.throw(
				_("Bunny POS: the scale label is for item {0}, which nothing here answers to.").format(
					label["item_number"]
				)
			)
	else:
		found = scan_barcode(code) or {}
		item_code = found.get("item_code")

	if not item_code:
		frappe.throw(_("Bunny POS: nothing matches {0}.").format(code))

	rows = _fetch_items(profile, None, None, 0, 1, item_code=item_code)
	if not rows:
		frappe.throw(
			_("Bunny POS: {0} is not sellable on this POS Profile.").format(item_code)
		)

	item = rows[0]
	prices = _get_prices(profile, [item_code])
	stock = _get_stock(profile, [item_code])
	uoms = _get_uoms([item_code])

	price = prices.get(item_code) or {}
	item.rate = flt(price.get("price_list_rate"))
	item.price_list_rate = item.rate
	item.currency = price.get("currency") or profile.currency
	item.stock_qty = flt(stock.get(item_code))
	item.uom = item.stock_uom
	item.uoms = uoms.get(item_code) or [{"uom": item.stock_uom, "conversion_factor": 1.0}]

	# A barcode may be specific to a pack size; sell it in that UOM.
	scanned_uom = found.get("uom")
	if scanned_uom and any(u["uom"] == scanned_uom for u in item.uoms):
		item.uom = scanned_uom

	qty = 1.0
	if label:
		qty = _scale_qty(item, label)

	return {
		"item": item,
		"uom": item.uom,
		# How many of that unit the label says, so a weighed item does not
		# arrive as "1" and have to be keyed in again.
		"qty": qty,
		"weighed": bool(label),
		"barcode": found.get("barcode") or "",
		"serial_no": found.get("serial_no") or "",
		"batch_no": found.get("batch_no") or "",
	}


@frappe.whitelist()
@pos_api
def get_stock(pos_profile, item_codes):
	"""Current sellable quantity for a set of items -- nothing else.

	Tills poll this every few seconds so a cashier sees an item go out of stock
	when another till sells it, rather than finding out at checkout. Kept
	deliberately small: no prices, no names, just ``{item_code: qty}``.
	"""
	profile = get_pos_profile(pos_profile)

	codes = parse_json(item_codes, "item_codes", (list,)) or []
	codes = [str(code) for code in codes if code][:MAX_STOCK_CODES]
	if not codes:
		return {}

	stock = _get_stock(profile, codes)

	# Report every code asked about, so an item whose Bin row has gone still
	# reads as zero rather than silently keeping its old number on the till.
	return {code: flt(stock.get(code)) for code in codes}


@frappe.whitelist()
@pos_api
def get_item_groups(pos_profile):
	"""Item groups that actually have sellable items on this profile."""
	profile = get_pos_profile(pos_profile)

	conditions, params = _base_conditions(profile)

	rows = frappe.db.sql(
		"""
		select i.item_group as item_group, count(*) as item_count
		from `tabItem` i
		where {conditions}
		group by i.item_group
		order by i.item_group
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)

	return rows


def _base_conditions(profile):
	"""Conditions every item lookup on this profile shares."""
	conditions = ["i.disabled = 0", "i.has_variants = 0", "i.is_sales_item = 1"]
	params = {}

	item_groups = _get_item_groups(profile)
	if item_groups:
		conditions.append("i.item_group in %(profile_groups)s")
		params["profile_groups"] = item_groups

	return conditions, params


def _fetch_items(profile, search_term, item_group, start, limit, item_code=None):
	conditions, params = _base_conditions(profile)
	params.update({"limit": limit, "start": start})

	if item_code:
		conditions.append("i.name = %(item_code)s")
		params["item_code"] = item_code

	if item_group:
		# One chosen category, plus anything nested under it.
		wanted = [item_group] + (frappe.db.get_descendants("Item Group", item_group) or [])
		conditions.append("i.item_group in %(item_group)s")
		params["item_group"] = wanted

	if search_term:
		search_term = str(search_term).strip()

	if search_term:
		conditions.append(
			"""(
				i.name like %(like)s
				or i.item_name like %(like)s
				or exists (
					select 1 from `tabItem Barcode` bc
					where bc.parent = i.name and bc.barcode = %(exact)s
				)
			)"""
		)
		params["like"] = f"%{search_term}%"
		params["exact"] = search_term

	return frappe.db.sql(
		"""
		select
			i.name as item_code,
			i.item_name,
			i.description,
			i.item_group,
			i.stock_uom,
			i.image,
			i.is_stock_item
		from `tabItem` i
		where {conditions}
		order by i.item_name
		limit %(limit)s offset %(start)s
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)


def _get_item_groups(profile):
	"""Expand the profile's item group filter to include child groups."""
	groups = [d.item_group for d in profile.get("item_groups") or [] if d.item_group]
	if not groups:
		return None

	expanded = set()
	for group in groups:
		expanded.add(group)
		expanded.update(frappe.db.get_descendants("Item Group", group) or [])

	return list(expanded)


def _get_uoms(item_codes):
	"""Selling UOMs per item, stock UOM first, with conversion factors.

	A till needs these to sell a case as well as a single unit; the conversion
	factor is what turns the entered quantity back into stock quantity.
	"""
	rows = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": ("in", item_codes), "parenttype": "Item"},
		fields=["parent", "uom", "conversion_factor"],
		order_by="idx",
	)

	stock_uoms = dict(
		frappe.get_all(
			"Item",
			filters={"name": ("in", item_codes)},
			fields=["name", "stock_uom"],
			as_list=True,
		)
	)

	by_item = {}
	for row in rows:
		if not row.conversion_factor:
			continue
		by_item.setdefault(row.parent, []).append(
			{"uom": row.uom, "conversion_factor": flt(row.conversion_factor)}
		)

	for item_code, stock_uom in stock_uoms.items():
		entries = by_item.setdefault(item_code, [])
		if not any(e["uom"] == stock_uom for e in entries):
			entries.insert(0, {"uom": stock_uom, "conversion_factor": 1.0})
		# Selling in the stock UOM is the common case, so list it first.
		entries.sort(key=lambda e: e["uom"] != stock_uom)

	return by_item


def _get_prices(profile, item_codes):
	"""Best valid selling price per item from the profile's price list."""
	if not profile.selling_price_list:
		return {}

	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": profile.selling_price_list, "item_code": ("in", item_codes)},
		fields=["item_code", "price_list_rate", "currency", "uom", "valid_from", "valid_upto"],
	)

	today = getdate(nowdate())
	prices = {}

	for row in rows:
		if row.currency and profile.currency and row.currency != profile.currency:
			continue
		if row.valid_from and getdate(row.valid_from) > today:
			continue
		if row.valid_upto and getdate(row.valid_upto) < today:
			continue

		current = prices.get(row.item_code)
		if not current or _price_rank(row) > _price_rank(current):
			prices[row.item_code] = row

	return prices


def _price_rank(row):
	"""Prefer a dated price over an open-ended one, newest first."""
	return (1 if row.get("valid_from") else 0, str(row.get("valid_from") or ""))


def _get_stock(profile, item_codes):
	"""Quantity a till may actually sell, per item, in the profile's warehouse.

	This mirrors ERPNext's own ``get_stock_availability``: the Bin quantity
	less anything already sold on submitted POS Invoices that have not been
	consolidated yet. Reporting the raw Bin quantity would let a cashier build
	a cart the server then rejects at submit.
	"""
	if not profile.warehouse:
		return {}

	bins = frappe.get_all(
		"Bin",
		filters={"warehouse": profile.warehouse, "item_code": ("in", item_codes)},
		fields=["item_code", "actual_qty"],
	)
	stock = {row.item_code: flt(row.actual_qty) for row in bins}

	for item_code, reserved in _get_pos_reserved_qty(profile.warehouse, item_codes).items():
		if item_code in stock:
			stock[item_code] -= flt(reserved)

	return stock


def _get_pos_reserved_qty(warehouse, item_codes):
	"""Qty held by submitted POS Invoices that are not consolidated yet."""
	reserved = {}

	# "Packed Item" covers items sold as part of a Product Bundle.
	for table, qty_field in (("POS Invoice Item", "stock_qty"), ("Packed Item", "qty")):
		rows = frappe.db.sql(
			"""
			select child.item_code as item_code, sum(child.{qty_field}) as qty
			from `tabPOS Invoice` pinv, `tab{table}` child
			where pinv.name = child.parent
				and child.parenttype = 'POS Invoice'
				and child.docstatus = 1
				and ifnull(pinv.consolidated_invoice, '') = ''
				and child.warehouse = %(warehouse)s
				and child.item_code in %(item_codes)s
			group by child.item_code
			""".format(table=table, qty_field=qty_field),
			{"warehouse": warehouse, "item_codes": item_codes},
			as_dict=True,
		)

		for row in rows:
			reserved[row.item_code] = reserved.get(row.item_code, 0) + flt(row.qty)

	return reserved
