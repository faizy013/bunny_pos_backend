# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""POS shift handling -- opening an entry and finding the one already open."""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate

from bunny_pos_backend.api.utils import (
	get_payment_modes,
	get_pos_profile,
	parse_json,
	pos_api,
	profile_context,
	validate_profile_user,
)


@frappe.whitelist()
@pos_api
def get_pos_profiles():
	"""POS Profiles the current user may open a shift on."""
	profiles = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=[
			"name",
			"company",
			"warehouse",
			"currency",
			"selling_price_list as price_list",
			"customer",
		],
		order_by="name",
	)

	allowed = []
	for profile in profiles:
		users = frappe.get_all(
			"POS Profile User",
			filters={"parent": profile.name, "parenttype": "POS Profile"},
			pluck="user",
		)
		if users and frappe.session.user not in users:
			continue

		profile["payments"] = get_payment_modes(frappe._dict(name=profile.name))
		allowed.append(profile)

	return allowed


@frappe.whitelist()
@pos_api
def get_open_shift(user=None):
	"""Return the caller's currently open POS Opening Entry, or None."""
	user = _resolve_user(user)

	shifts = frappe.get_all(
		"POS Opening Entry",
		filters={"user": user, "status": "Open", "docstatus": 1},
		fields=["name", "pos_profile", "company", "period_start_date", "posting_date"],
		order_by="creation desc",
		limit=1,
	)

	if not shifts:
		return None

	shift = shifts[0]
	profile = get_pos_profile(shift.pos_profile)

	shift["user"] = user
	shift["balance_details"] = frappe.get_all(
		"POS Opening Entry Detail",
		filters={"parent": shift.name, "parenttype": "POS Opening Entry"},
		fields=["mode_of_payment", "opening_amount"],
		order_by="idx",
	)
	shift.update(profile_context(profile))

	return shift


@frappe.whitelist()
@pos_api
def get_shift_summary():
	"""What this shift has taken, ready for the close screen.

	Expected amounts come from ERPNext's own closing-entry builder, so the
	figures a cashier counts against are the ones ERPNext will reconcile.
	"""
	shift = _current_shift()

	entry = _build_closing_entry(shift)

	return {
		"shift": shift.name,
		"pos_profile": entry.pos_profile,
		"period_start_date": str(entry.period_start_date),
		"period_end_date": str(entry.period_end_date),
		"currency": frappe.get_cached_value("POS Profile", entry.pos_profile, "currency"),
		"invoice_count": len(entry.pos_transactions),
		"total_quantity": flt(entry.total_quantity),
		"net_total": flt(entry.net_total),
		"grand_total": flt(entry.grand_total),
		"payments": [
			{
				"mode_of_payment": row.mode_of_payment,
				"opening_amount": flt(row.opening_amount),
				"expected_amount": flt(row.expected_amount),
			}
			for row in entry.payment_reconciliation
		],
	}


@frappe.whitelist(methods=["POST"])
@pos_api
def close_shift(closing_amounts=None):
	"""Close the caller's shift with a POS Closing Entry.

	``closing_amounts`` is what was actually counted in the drawer, as a
	mapping of mode to amount or a list of rows. Anything not counted is
	assumed to match what ERPNext expects, so a cash-only till can close by
	entering one number.
	"""
	shift = _current_shift()
	entry = _build_closing_entry(shift)

	counted = _parse_closing_amounts(closing_amounts)
	unknown = [
		mode
		for mode in counted
		if mode not in [row.mode_of_payment for row in entry.payment_reconciliation]
	]
	if unknown:
		frappe.throw(
			_("Bunny POS: {0} was not used on this shift.").format(", ".join(unknown))
		)

	for row in entry.payment_reconciliation:
		expected = flt(row.expected_amount)
		row.closing_amount = (
			flt(counted[row.mode_of_payment])
			if row.mode_of_payment in counted
			else expected
		)
		row.difference = flt(row.closing_amount) - expected

	entry.insert()

	# ERPNext consolidates invoices during submit, and that path writes a
	# comment linked back to this entry. The link only resolves once the row
	# is committed, so commit before submitting rather than inside it.
	frappe.db.commit()

	entry.submit()

	return {
		"name": entry.name,
		"status": entry.status,
		"shift": shift.name,
		"pos_profile": entry.pos_profile,
		"period_start_date": str(entry.period_start_date),
		"period_end_date": str(entry.period_end_date),
		"invoice_count": len(entry.pos_transactions),
		"total_quantity": flt(entry.total_quantity),
		"net_total": flt(entry.net_total),
		"grand_total": flt(entry.grand_total),
		"payments": [
			{
				"mode_of_payment": row.mode_of_payment,
				"opening_amount": flt(row.opening_amount),
				"expected_amount": flt(row.expected_amount),
				"closing_amount": flt(row.closing_amount),
				"difference": flt(row.difference),
			}
			for row in entry.payment_reconciliation
		],
	}


def _current_shift():
	"""The caller's open POS Opening Entry, or a clear refusal."""
	names = frappe.get_all(
		"POS Opening Entry",
		filters={"user": frappe.session.user, "status": "Open", "docstatus": 1},
		pluck="name",
		order_by="creation desc",
		limit=1,
	)
	if not names:
		frappe.throw(_("Bunny POS: there is no open shift to close."))

	return frappe.get_doc("POS Opening Entry", names[0])


def _build_closing_entry(shift):
	"""Let ERPNext assemble the closing entry from the opening one."""
	from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
		make_closing_entry_from_opening,
	)

	return make_closing_entry_from_opening(shift)


def _parse_closing_amounts(closing_amounts):
	"""Accept ``{"Cash": 7250}`` or ``[{"mode_of_payment": ..., "closing_amount": ...}]``."""
	closing_amounts = parse_json(closing_amounts, "closing_amounts", (dict, list))
	if not closing_amounts:
		return {}

	if isinstance(closing_amounts, dict):
		return {mode: flt(amount) for mode, amount in closing_amounts.items()}

	counted = {}
	for row in closing_amounts:
		if not isinstance(row, dict):
			frappe.throw(_("Bunny POS: closing_amounts rows must be objects."))
		mode = row.get("mode_of_payment")
		if not mode:
			frappe.throw(_("Bunny POS: every closing_amounts row needs a mode_of_payment."))
		counted[mode] = flt(row.get("closing_amount"))
	return counted


@frappe.whitelist(methods=["POST"])
@pos_api
def open_shift(pos_profile, opening_amounts=None):
	"""Create and submit a POS Opening Entry for the caller."""
	profile = get_pos_profile(pos_profile)
	user = frappe.session.user

	existing = frappe.get_all(
		"POS Opening Entry",
		filters={"user": user, "status": "Open", "docstatus": 1},
		pluck="name",
		limit=1,
	)
	if existing:
		frappe.throw(
			_("Bunny POS: shift {0} is already open for {1}. Close it before opening a new one.").format(
				existing[0], user
			)
		)

	balance_details = _build_balance_details(profile, opening_amounts)

	entry = frappe.new_doc("POS Opening Entry")
	entry.period_start_date = now_datetime()
	entry.posting_date = nowdate()
	entry.company = profile.company
	entry.pos_profile = profile.name
	entry.user = user

	for row in balance_details:
		entry.append("balance_details", row)

	entry.insert()

	# ERPNext consolidates invoices during submit, and that path writes a
	# comment linked back to this entry. The link only resolves once the row
	# is committed, so commit before submitting rather than inside it.
	frappe.db.commit()

	entry.submit()

	return get_open_shift(user)


def _resolve_user(user):
	"""Only let a caller inspect their own shift unless they manage the system."""
	if not user:
		return frappe.session.user

	if user != frappe.session.user and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError(
			_("Bunny POS: not permitted to read the shift of another user.")
		)

	return user


def _build_balance_details(profile, opening_amounts):
	"""Normalise opening amounts and cover every mode of payment on the profile.

	``opening_amounts`` may be omitted, a mapping of mode to amount, or a list
	of ``{"mode_of_payment": ..., "opening_amount": ...}`` rows.
	"""
	profile_modes = [d.mode_of_payment for d in profile.get("payments") or []]
	if not profile_modes:
		frappe.throw(
			_("Bunny POS: POS Profile {0} has no mode of payment configured.").format(profile.name)
		)

	opening_amounts = parse_json(opening_amounts, "opening_amounts", (dict, list))
	amounts = {}

	if isinstance(opening_amounts, dict):
		amounts = {mode: flt(amount) for mode, amount in opening_amounts.items()}
	elif isinstance(opening_amounts, list):
		for row in opening_amounts:
			if not isinstance(row, dict):
				frappe.throw(_("Bunny POS: opening_amounts rows must be objects."))
			mode = row.get("mode_of_payment")
			if not mode:
				frappe.throw(_("Bunny POS: every opening_amounts row needs a mode_of_payment."))
			amounts[mode] = flt(row.get("opening_amount"))

	unknown = [mode for mode in amounts if mode not in profile_modes]
	if unknown:
		frappe.throw(
			_("Bunny POS: mode of payment {0} is not configured on POS Profile {1}.").format(
				", ".join(unknown), profile.name
			)
		)

	return [
		{"mode_of_payment": mode, "opening_amount": flt(amounts.get(mode))}
		for mode in profile_modes
	]
