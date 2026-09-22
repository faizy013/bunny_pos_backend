# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Dine-in service: a floor of tables, each with an order that stays open.

An order is a draft Sales Order with the table's name in ``po_no``. Keeping it
on the server rather than in the till is what lets a waiter take the order on
one device and the counter settle it on another, and what stops a reload from
losing a table's food.

Nothing is stored about whether a table is busy. That is read from whether it
has an open order, so two tills can never disagree about it.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from bunny_pos_backend.api.invoices import create_invoice
from bunny_pos_backend.api.utils import get_pos_profile, parse_json, pos_api


NOTE_TAG = "\u2014 "


def _split_note(description, item_name):
	"""Pull the cashier's note back out of the line's description."""
	text = (description or "").strip()
	if NOTE_TAG not in text:
		return ""
	return text.split(NOTE_TAG, 1)[1].strip()


def _with_note(item_name, note):
	"""Notes ride on ``description``, a standard Sales Order Item field.

	A kitchen ticket without "no onions" on it is not much use, and there is
	nowhere else to put one without changing the customer's ERPNext.
	"""
	note = (note or "").strip()
	return f"{item_name}\n{NOTE_TAG}{note}" if note else item_name


def _table(name, profile):
	if not frappe.db.exists("Restaurant Table", name):
		frappe.throw(_("Bunny POS: there is no table called {0}.").format(name))
	doc = frappe.get_doc("Restaurant Table", name)
	if doc.pos_profile != profile.name:
		frappe.throw(
			_("Bunny POS: {0} belongs to {1}, not this till.").format(name, doc.pos_profile)
		)
	if cint(doc.disabled):
		frappe.throw(_("Bunny POS: {0} is out of use.").format(name))
	return doc


def _open_order_name(table, company):
	rows = frappe.get_all(
		"Sales Order",
		filters={"po_no": table, "docstatus": 0, "company": company},
		fields=["name"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0].name if rows else None


def _forget_tickets(order_name):
	"""Drop the kitchen history when its order goes.

	The tickets exist to tell one open order's new courses from its old ones.
	Once the order is gone they answer a question nobody will ask again, and
	leaving them behind lets a later order inherit the wrong history.
	"""
	for name in frappe.get_all(
		"Kitchen Ticket", filters={"sales_order": order_name}, pluck="name"
	):
		frappe.delete_doc("Kitchen Ticket", name, ignore_permissions=True, force=True)


def _order_summary(doc):
	return {
		"order": doc.name,
		"table": doc.po_no,
		"customer": doc.customer,
		"customer_name": doc.customer_name,
		"currency": doc.currency,
		"total": flt(doc.total),
		"grand_total": flt(doc.grand_total),
		"quantity": flt(sum(flt(row.qty) for row in doc.items)),
		"line_count": len(doc.items),
		"opened_at": str(doc.creation),
		"items": [
			{
				"name": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": flt(row.qty),
				"uom": row.uom,
				"rate": flt(row.rate),
				"amount": flt(row.amount),
				"notes": _split_note(row.description, row.item_name),
			}
			for row in doc.items
		],
	}


@frappe.whitelist()
@pos_api
def get_tables(pos_profile=None):
	"""The floor, with what each table currently owes."""
	profile = get_pos_profile(pos_profile)

	tables = frappe.get_all(
		"Restaurant Table",
		filters={"pos_profile": profile.name, "disabled": 0},
		fields=["name", "table_name", "section", "seats"],
		order_by="section asc, table_name asc",
	)

	# One query for every open order, rather than one per table.
	orders = frappe.get_all(
		"Sales Order",
		filters={"docstatus": 0, "company": profile.company, "po_no": ("in", [t.name for t in tables] or [""])},
		fields=["name", "po_no", "grand_total", "creation", "customer_name"],
	)
	by_table = {o.po_no: o for o in orders}

	out = []
	for row in tables:
		order = by_table.get(row.name)
		out.append(
			{
				"table": row.name,
				"section": row.section or "",
				"seats": cint(row.seats),
				"busy": bool(order),
				"order": order.name if order else None,
				"total": flt(order.grand_total) if order else 0.0,
				"since": str(order.creation) if order else None,
				"customer_name": order.customer_name if order else None,
			}
		)

	return {"currency": profile.currency, "tables": out}


@frappe.whitelist()
@pos_api
def get_order(table, pos_profile=None):
	"""What is on a table right now, or nothing if it is free."""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	name = _open_order_name(table, profile.company)
	if not name:
		return None
	return _order_summary(frappe.get_doc("Sales Order", name))


@frappe.whitelist(methods=["POST"])
@pos_api
def save_order(table, cart_data, customer=None, pos_profile=None):
	"""Write the table's order, creating it on the first course.

	The whole order is sent each time rather than a difference. A table is
	edited by one person at a time and the list is short, so sending the truth
	is simpler than reconciling changes -- and it cannot drift.
	"""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	cart = parse_json(cart_data, "cart_data", (list,))
	customer = customer or profile.customer
	if not customer:
		frappe.throw(
			_("Bunny POS: no customer supplied and POS Profile {0} has no default customer.").format(
				profile.name
			)
		)

	name = _open_order_name(table, profile.company)

	if not cart:
		# An order emptied to nothing is a table nobody sat at after all.
		if name:
			_forget_tickets(name)
			frappe.delete_doc("Sales Order", name, ignore_permissions=True, force=True)
		return None

	doc = frappe.get_doc("Sales Order", name) if name else frappe.new_doc("Sales Order")
	doc.company = profile.company
	doc.customer = customer
	doc.po_no = table
	doc.order_type = "Sales"
	doc.currency = profile.currency
	doc.selling_price_list = profile.selling_price_list
	if not doc.transaction_date:
		doc.transaction_date = frappe.utils.nowdate()
	if not doc.delivery_date:
		doc.delivery_date = frappe.utils.nowdate()

	doc.set("items", [])
	for idx, row in enumerate(cart, start=1):
		if not isinstance(row, dict):
			frappe.throw(_("Bunny POS: order row {0} must be an object.").format(idx))
		item_code = row.get("item_code")
		qty = flt(row.get("qty"))
		if not item_code:
			frappe.throw(_("Bunny POS: order row {0} has no item.").format(idx))
		if qty <= 0:
			frappe.throw(_("Bunny POS: {0} needs a quantity above zero.").format(item_code))
		line = {
			"item_code": item_code,
			"qty": qty,
			"warehouse": profile.warehouse,
			"delivery_date": doc.delivery_date,
		}
		if row.get("uom"):
			line["uom"] = row["uom"]
			line["conversion_factor"] = flt(row.get("conversion_factor")) or 1
		note = str(row.get("notes") or "").strip()
		if note:
			line["description"] = _with_note(
				frappe.db.get_value("Item", item_code, "item_name") or item_code, note
			)
		doc.append("items", line)

	doc.set_missing_values()
	doc.flags.ignore_permissions = True
	doc.save()
	return _order_summary(doc)


@frappe.whitelist(methods=["POST"])
@pos_api
def clear_table(table, pos_profile=None):
	"""Throw the order away -- the party left, or it was opened by mistake."""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	name = _open_order_name(table, profile.company)
	if not name:
		return {"cleared": False}

	_forget_tickets(name)
	frappe.delete_doc("Sales Order", name, ignore_permissions=True, force=True)
	return {"cleared": True}


@frappe.whitelist(methods=["POST"])
@pos_api
def bill_table(table, payments=None, request_id=None, pos_profile=None, coupon_code=None, loyalty_points=None):
	"""Settle a table: bill what is on it, then free it.

	The order is only deleted once the invoice exists. If billing fails the
	table keeps its food, which is the side to err on with a customer standing
	at the counter.
	"""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	name = _open_order_name(table, profile.company)
	if not name:
		frappe.throw(_("Bunny POS: {0} has nothing to bill.").format(table))

	order = frappe.get_doc("Sales Order", name)
	if not order.items:
		frappe.throw(_("Bunny POS: {0} has nothing to bill.").format(table))

	cart = [
		{
			"item_code": row.item_code,
			"qty": flt(row.qty),
			"uom": row.uom,
			"conversion_factor": flt(row.conversion_factor) or 1,
		}
		for row in order.items
	]

	invoice = create_invoice(
		cart,
		customer=order.customer,
		payments=payments,
		pos_profile=profile.name,
		request_id=request_id,
		coupon_code=coupon_code,
		loyalty_points=loyalty_points,
	)

	_forget_tickets(name)
	frappe.delete_doc("Sales Order", name, ignore_permissions=True, force=True)
	invoice["table"] = table
	return invoice


def _sent_so_far(order_name):
	"""How much of each item+note the kitchen has already been given."""
	sent = {}
	tickets = frappe.get_all(
		"Kitchen Ticket", filters={"sales_order": order_name}, pluck="name"
	)
	if not tickets:
		return sent
	for row in frappe.get_all(
		"Kitchen Ticket Item",
		filters={"parent": ("in", tickets)},
		fields=["item_code", "notes", "qty"],
	):
		key = (row.item_code, (row.notes or "").strip())
		sent[key] = sent.get(key, 0) + flt(row.qty)
	return sent


def _pending_for_kitchen(order):
	"""What the cook has not been told about yet, in order."""
	sent = _sent_so_far(order.name)
	pending = []
	for row in order.items:
		note = _split_note(row.description, row.item_name)
		key = (row.item_code, note)
		already = sent.get(key, 0)
		outstanding = flt(row.qty) - already
		if outstanding <= 0:
			# Fully sent; anything left over counts against later lines.
			sent[key] = already - flt(row.qty)
			continue
		sent[key] = 0
		pending.append(
			{"item_code": row.item_code, "item_name": row.item_name, "qty": outstanding, "notes": note}
		)
	return pending


def _kitchen_html(table, profile, rows, repeat=False):
	body = "".join(
		"<tr><td class='q'>{qty:g}</td><td>{name}{note}</td></tr>".format(
			qty=flt(row["qty"]),
			name=frappe.utils.escape_html(row["item_name"]),
			note=(
				f"<div class='note'>{frappe.utils.escape_html(row['notes'])}</div>"
				if row.get("notes")
				else ""
			),
		)
		for row in rows
	)
	mark = "<div class='again'>REPRINT</div>" if repeat else ""
	return f"""<!doctype html><meta charset="utf-8">
<style>
  body {{ font-family: "Courier New", monospace; width: 72mm; margin: 0; padding: 6mm 4mm; color: #000; }}
  h1 {{ font-size: 22px; margin: 0 0 1mm; letter-spacing: .04em; }}
  .meta {{ font-size: 12px; margin-bottom: 3mm; }}
  .again {{ font-size: 13px; font-weight: 700; border: 2px solid #000; display: inline-block;
            padding: 0 2mm; margin-bottom: 2mm; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
  td {{ padding: 2mm 0; border-bottom: 1px dashed #999; vertical-align: top; }}
  td.q {{ width: 12mm; font-weight: 700; }}
  .note {{ font-size: 12px; font-weight: 700; text-transform: uppercase; padding-top: .5mm; }}
</style>
<h1>TABLE {frappe.utils.escape_html(table)}</h1>
{mark}
<div class="meta">{frappe.utils.format_datetime(frappe.utils.now(), "dd MMM, HH:mm")} &middot; {frappe.utils.escape_html(profile.name)}</div>
<table>{body}</table>"""


@frappe.whitelist()
@pos_api
def get_kitchen_ticket(table, pos_profile=None, everything=0):
	"""What the kitchen has not been told yet.

	Only the new courses go, because sending the whole table again leaves the
	cook guessing which dishes they have already made. ``everything`` reprints
	the lot, for when a ticket is lost.
	"""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	name = _open_order_name(table, profile.company)
	if not name:
		frappe.throw(_("Bunny POS: {0} has no order to send.").format(table))

	order = frappe.get_doc("Sales Order", name)
	repeat = bool(cint(everything))

	if repeat:
		rows = [
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": flt(row.qty),
				"notes": _split_note(row.description, row.item_name),
			}
			for row in order.items
		]
	else:
		rows = _pending_for_kitchen(order)

	if not rows:
		frappe.throw(_("Bunny POS: the kitchen already has everything on {0}.").format(table))

	return {
		"table": table,
		"order": name,
		"repeat": repeat,
		"items": rows,
		"html": _kitchen_html(table, profile, rows, repeat),
	}


@frappe.whitelist(methods=["POST"])
@pos_api
def mark_sent(table, items=None, pos_profile=None):
	"""Record what the kitchen was just given, so it is not sent twice.

	Kept apart from building the ticket: nothing counts as sent until the till
	says the printer took it.
	"""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)

	name = _open_order_name(table, profile.company)
	if not name:
		frappe.throw(_("Bunny POS: {0} has no order.").format(table))

	rows = parse_json(items, "items", (list,)) if items else _pending_for_kitchen(
		frappe.get_doc("Sales Order", name)
	)
	if not rows:
		return {"recorded": 0}

	ticket = frappe.new_doc("Kitchen Ticket")
	ticket.restaurant_table = table
	ticket.sales_order = name
	ticket.pos_profile = profile.name
	ticket.printed_at = frappe.utils.now()
	for row in rows:
		ticket.append(
			"items",
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": flt(row.get("qty")),
				"notes": (row.get("notes") or "").strip(),
			},
		)
	ticket.flags.ignore_permissions = True
	ticket.insert()
	return {"recorded": len(ticket.items), "ticket": ticket.name}


@frappe.whitelist(methods=["POST"])
@pos_api
def move_table(table, to_table, pos_profile=None):
	"""Move a party to another table, or join them to one already sitting there.

	Parties move and tables get pushed together; without this the only way is
	to retype the order, which is how courses go missing.
	"""
	profile = get_pos_profile(pos_profile)
	_table(table, profile)
	_table(to_table, profile)

	if table == to_table:
		frappe.throw(_("Bunny POS: that is the same table."))

	source_name = _open_order_name(table, profile.company)
	if not source_name:
		frappe.throw(_("Bunny POS: {0} has nothing to move.").format(table))

	target_name = _open_order_name(to_table, profile.company)
	source = frappe.get_doc("Sales Order", source_name)

	if not target_name:
		source.po_no = to_table
		source.flags.ignore_permissions = True
		source.save()
		# The tickets follow the food.
		frappe.db.set_value(
			"Kitchen Ticket", {"sales_order": source_name}, "restaurant_table", to_table
		)
		return {"moved": True, "merged": False, "order": source_name, "table": to_table}

	# Both tables are occupied, so the orders join into the one already there.
	target = frappe.get_doc("Sales Order", target_name)
	for row in source.items:
		target.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"uom": row.uom,
				"conversion_factor": row.conversion_factor,
				"rate": row.rate,
				"description": row.description,
				"warehouse": row.warehouse,
				"delivery_date": row.delivery_date or target.delivery_date,
			},
		)
	target.flags.ignore_permissions = True
	target.save()

	frappe.db.set_value(
		"Kitchen Ticket", {"sales_order": source_name}, "sales_order", target_name
	)
	frappe.db.set_value(
		"Kitchen Ticket", {"sales_order": target_name}, "restaurant_table", to_table
	)
	frappe.delete_doc("Sales Order", source_name, ignore_permissions=True, force=True)

	return {"moved": True, "merged": True, "order": target_name, "table": to_table}
