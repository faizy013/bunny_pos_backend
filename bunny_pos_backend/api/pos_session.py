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
