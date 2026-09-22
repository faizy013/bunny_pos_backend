# Copyright (c) 2026, Bunny POS and contributors
# For license information, please see license.txt

"""Run the Bunny POS tests without letting the test runner near your data.

``bench run-tests`` fires ERPNext's ``before_tests`` hook first, and that hook
runs ``delete from `tabItem Price```. On a site you are demoing or developing
against, that silently prices every item at zero -- and nothing about the test
output tells you it happened.

So these run in-process instead:

    bench --site <site> execute bunny_pos_backend.tests.run.all
"""

import unittest

import frappe


def all(verbosity: int = 2):
	"""Run every Bunny POS test module and return a short summary."""
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromNames(
		[
			"bunny_pos_backend.tests.test_invoices",
			"bunny_pos_backend.tests.test_restaurant",
		]
	)
	result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

	summary = {
		"run": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"skipped": len(result.skipped),
	}
	print(f"\nBunny POS: {summary}")

	# These tests bill real invoices, so leave the site consistent either way.
	frappe.db.commit()
	return summary
