# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BunnyKitchenTicket(Document):
	"""A record of what the kitchen has already been told to cook.

	Kept so a second trip to the pass sends only what is new. Without it the
	cook gets the whole table again and has no way to tell the difference.
	"""

	pass
