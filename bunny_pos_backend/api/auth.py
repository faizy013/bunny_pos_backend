# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Connection check and cashier sign-in.

Setting a till up takes three things, all typed on the till itself: the
server's address (an IP or hostname), a username and a password. Nothing about
the shop is registered on the server beforehand.

``login`` verifies the password and hands back that cashier's own API Key +
Secret. From then on the till authenticates with those, so POS Opening Entries
and POS Invoices carry the real cashier's name.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

from bunny_pos_backend import __version__
from bunny_pos_backend.api.utils import pos_api

# Sign-in is the one door open to the world, so it is deliberately slow to
# guess against: a run of failures from one place stops being answered.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


@frappe.whitelist(allow_guest=True)
def server_info():
	"""Confirm an address really is a Bunny POS server, before a till saves it.

	Open to guests on purpose: the setup screen has no credentials yet. It
	says only that this app is installed, and nothing about the site or its
	users.
	"""
	return {"app": "bunny_pos_backend", "version": __version__, "ready": True}


@frappe.whitelist()
@pos_api
def client_version():
	"""What version the tills on this site should be running.

	Read from site_config.json, so a new build is announced by editing one
	file on the server the tills already talk to -- no extra hosting:

	    "bunny_pos_client_version": "0.2.0",
	    "bunny_pos_download_url": "https://drive.google.com/...",
	    "bunny_pos_update_notes": "Returns and live stock",
	    "bunny_pos_update_mandatory": 0

	Leave the version unset and tills simply never mention updates.
	"""
	return {
		"version": str(frappe.conf.get("bunny_pos_client_version") or "").strip(),
		"download_url": str(frappe.conf.get("bunny_pos_download_url") or "").strip(),
		"notes": str(frappe.conf.get("bunny_pos_update_notes") or "").strip(),
		"mandatory": cint(frappe.conf.get("bunny_pos_update_mandatory")),
	}


@frappe.whitelist()
@pos_api
def test_connection():
	"""Confirm the address and stored credentials on this till still work."""
	return {
		"connected": True,
		"app": "bunny_pos_backend",
		"version": __version__,
		"site": frappe.local.site,
		"user": frappe.session.user,
		"full_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
		"server_time": str(frappe.utils.now_datetime()),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="username", limit=20, seconds=60, methods=["POST"])
def login(username, password):
	"""Sign a cashier in and hand back their own API credentials.

	Open to guests because this is where a till gets its credentials -- it has
	none before this call. The password is the only thing that opens it, and a
	run of wrong ones closes it (see ``_record_failure``).
	"""
	username = (username or "").strip()
	if not username or not password:
		frappe.throw(_("Bunny POS: username and password are required."))

	user = _resolve_user(username)
	_check_not_locked_out(user or username)

	if not user:
		_record_failure(username)
		raise frappe.AuthenticationError(_("Bunny POS: incorrect username or password."))

	try:
		from frappe.utils.password import check_password

		check_password(user, password)
	except frappe.AuthenticationError:
		_record_failure(user)
		raise frappe.AuthenticationError(_("Bunny POS: incorrect username or password."))

	if not cint(frappe.db.get_value("User", user, "enabled")):
		raise frappe.AuthenticationError(_("Bunny POS: this user is disabled."))

	profiles = _usable_profiles(user)
	if not profiles:
		raise frappe.PermissionError(
			_("Bunny POS: {0} is not allowed on any POS Profile.").format(user)
		)

	_clear_failures(user)

	key, secret = _api_credentials(user)

	return {
		"user": user,
		"full_name": frappe.db.get_value("User", user, "full_name"),
		"api_key": key,
		"api_secret": secret,
		"pos_profiles": profiles,
		"site": frappe.local.site,
		"version": __version__,
	}


@frappe.whitelist(methods=["POST"])
@pos_api
def logout():
	"""Acknowledge sign-out. The till drops the cashier's credentials locally.

	The API secret is deliberately left alone -- rotating it here would sign
	the same cashier out of every other till they use.
	"""
	return {"user": frappe.session.user, "signed_out": True}


def _resolve_user(username):
	"""Accept either the login email or the username field."""
	if frappe.db.exists("User", username):
		return username
	return frappe.db.get_value("User", {"username": username}, "name")


def _usable_profiles(user):
	"""POS Profiles this user may open a shift on."""
	usable = []
	for profile in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		allowed = frappe.get_all(
			"POS Profile User",
			filters={"parent": profile, "parenttype": "POS Profile"},
			pluck="user",
		)
		if not allowed or user in allowed:
			usable.append(profile)
	return usable


def _api_credentials(user):
	"""The user's own API Key and Secret, generated once and then reused.

	Reused rather than rotated so signing in here does not invalidate the same
	cashier's session on another till.
	"""
	doc = frappe.get_doc("User", user)
	secret = None

	if doc.api_key:
		try:
			secret = get_decrypted_password("User", user, fieldname="api_secret")
		except Exception:
			secret = None

	if not doc.api_key or not secret:
		doc.api_key = doc.api_key or frappe.generate_hash(length=15)
		secret = frappe.generate_hash(length=15)
		doc.api_secret = secret
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	return doc.api_key, secret


def _failure_key(user):
	"""Count failures per username *and* source.

	Keying on the username alone would let anyone on the internet lock a real
	cashier out of their own till by guessing at them five times.
	"""
	source = getattr(frappe.local, "request_ip", None) or "local"
	return f"bunny_pos:login_failures:{source}:{user}"


def _check_not_locked_out(user):
	attempts = cint(frappe.cache().get_value(_failure_key(user)))
	if attempts >= MAX_ATTEMPTS:
		raise frappe.AuthenticationError(
			_("Bunny POS: too many failed attempts. Try again in a few minutes.")
		)


def _record_failure(user):
	key = _failure_key(user)
	attempts = cint(frappe.cache().get_value(key)) + 1
	frappe.cache().set_value(key, attempts, expires_in_sec=LOCKOUT_SECONDS)


def _clear_failures(user):
	frappe.cache().delete_value(_failure_key(user))
