# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Shared helpers for the Bunny POS whitelisted API.

Every public endpoint in this app goes through :func:`pos_api`, which enforces
that the caller authenticated with a Frappe API Key + API Secret token. The
Electron client never talks to ``/api/resource/*`` -- it only calls the
whitelisted methods in this package.
"""

import functools
import json

import frappe
from frappe import _
from frappe.utils import cint, flt

# Authorization schemes Frappe accepts for API Key + Secret pairs.
# "token <key>:<secret>" is what Bunny POS sends; "basic" is the base64 variant
# of the same credential pair and is accepted so curl/Postman testing works.
TOKEN_AUTH_SCHEMES = ("token", "basic")


def pos_api(fn):
	"""Guard a whitelisted method so only API Key + Secret callers reach it."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		validate_api_token_auth()
		return fn(*args, **kwargs)

	return wrapper


def validate_api_token_auth():
	"""Reject guests and anything that is not API Key + Secret token auth."""
	user = getattr(frappe.session, "user", None)

	if not user or user == "Guest":
		raise frappe.AuthenticationError(
			_("Bunny POS: a valid API Key and API Secret are required.")
		)

	request = getattr(frappe.local, "request", None)
	if request is None:
		# Called in-process (bench console, tests, server scripts) rather than
		# over HTTP -- there is no Authorization header to inspect.
		return

	authorization = frappe.get_request_header("Authorization") or ""
	scheme = authorization.split(" ", 1)[0].lower() if authorization else ""

	if scheme not in TOKEN_AUTH_SCHEMES:
		raise frappe.AuthenticationError(
			_("Bunny POS: API Key and API Secret token authentication is required.")
		)


def parse_json(value, fieldname, expected=None):
	"""Accept a value that may arrive as JSON text (form-encoded request).

	Frappe hands over real Python objects for JSON request bodies but plain
	strings for form-encoded ones, so both shapes have to be tolerated.
	"""
	if value is None or value == "":
		return None

	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (ValueError, TypeError):
			frappe.throw(_("Bunny POS: {0} is not valid JSON.").format(fieldname))

	if expected and not isinstance(value, expected):
		frappe.throw(_("Bunny POS: {0} has an unexpected format.").format(fieldname))

	return value


def get_pos_profile(pos_profile: str):
	"""Load a POS Profile the current user is actually allowed to use."""
	if not pos_profile:
		frappe.throw(_("Bunny POS: pos_profile is required."))

	if not frappe.db.exists("POS Profile", pos_profile):
		frappe.throw(_("Bunny POS: POS Profile {0} does not exist.").format(pos_profile))

	profile = frappe.get_cached_doc("POS Profile", pos_profile)

	if profile.disabled:
		frappe.throw(_("Bunny POS: POS Profile {0} is disabled.").format(pos_profile))

	validate_profile_user(profile)

	return profile


def validate_profile_user(profile):
	"""A POS Profile restricted to a user list may only be used by those users."""
	allowed = [d.user for d in profile.get("applicable_for_users") or []]

	if allowed and frappe.session.user not in allowed:
		raise frappe.PermissionError(
			_("Bunny POS: user {0} is not allowed to use POS Profile {1}.").format(
				frappe.session.user, profile.name
			)
		)


def get_payment_modes(profile) -> list:
	"""Modes of payment on a profile, with the type the till needs.

	``type`` is the Mode of Payment type (Cash, Bank, Phone, General). The
	client uses it to decide which modes can be over-tendered for change.
	"""
	rows = frappe.get_all(
		"POS Payment Method",
		filters={"parent": profile.name, "parenttype": "POS Profile"},
		fields=["mode_of_payment", "default", "allow_in_returns"],
		order_by="idx",
	)

	if not rows:
		return []

	types = dict(
		frappe.get_all(
			"Mode of Payment",
			filters={"name": ("in", [r.mode_of_payment for r in rows])},
			fields=["name", "type"],
			as_list=True,
		)
	)

	for row in rows:
		row["type"] = types.get(row.mode_of_payment) or "General"

	return rows


def profile_context(profile) -> dict:
	"""The bits of a POS Profile the client needs, so nothing is hardcoded."""
	return {
		"pos_profile": profile.name,
		"company": profile.company,
		"warehouse": profile.warehouse,
		"currency": profile.currency,
		"price_list": profile.selling_price_list,
		"customer": profile.customer,
		"allow_partial_payment": cint(profile.allow_partial_payment),
		# Everything below is a POS Profile switch the till has to obey, so a
		# shop configures its tills from ERPNext and nowhere else.
		"hide_images": cint(profile.hide_images),
		"hide_unavailable_items": cint(profile.hide_unavailable_items),
		"auto_add_item_to_cart": cint(profile.auto_add_item_to_cart),
		"print_receipt_on_order_complete": cint(profile.print_receipt_on_order_complete),
		"disable_grand_total_to_default_mop": cint(profile.disable_grand_total_to_default_mop),
		"disable_rounded_total": cint(profile.disable_rounded_total),
		"validate_stock_on_save": cint(profile.validate_stock_on_save),
		"write_off_limit": flt(profile.write_off_limit),
		"print_format": profile.print_format or "",
		"letter_head": profile.letter_head or "",
		"terms": profile.tc_name or "",
		"print_heading": profile.select_print_heading or "",
		"allow_rate_change": cint(profile.allow_rate_change),
		"allow_discount_change": cint(profile.allow_discount_change),
		"payments": get_payment_modes(profile),
	}
