## Bunny POS Backend

The server half of **Bunny POS**, a standalone desktop POS for ERPNext.

This app is a thin, purpose-built API layer. It exposes a small set of
whitelisted methods to the Electron till and nothing else.

### What it does not do

- It creates **no new DocTypes**.
- It adds **no custom fields** and no Property Setters.
- It only reads and writes standard ERPNext records: Item, Item Price, Bin,
  Customer, POS Profile, POS Invoice, POS Opening Entry and Mode of Payment.

All pricing, tax, stock validation and GL posting is left to ERPNext's own
POS Invoice controller, so none of that logic lives on the till.

### Why a dedicated app instead of raw REST

A till holds an API Key and Secret on disk. That key decides *who* connects,
not *what* they may do. Handing the client `/api/resource/*` would make a
leaked key equivalent to full read/write access to every customer, invoice and
stock record on the site. These endpoints narrow that to POS operations only.

### Signing a till in

A till needs three things, all typed on the till itself:

| | |
| --- | --- |
| Server address | the IP or hostname this app is installed on, e.g. `http://192.168.1.50:8000` |
| Username | the cashier's ERPNext login (email, or their `username`) |
| Password | the cashier's own password |

Nothing about the shop is registered on the server beforehand. Install the app
on the shop's own ERPNext server, point the till at that address, and sign in.

`auth.server_info` is there so the setup screen can check an address really is
a Bunny POS server before saving it. It is open to guests and says only that
this app is installed — nothing about the site or its users.

### Install

```bash
bench get-app bunny_pos_backend /path/to/bunny_pos_backend
bench --site your-site.com install-app bunny_pos_backend
```

### Authentication

Every endpoint except `auth.login` and `auth.server_info` requires Frappe
API Key + Secret token authentication:

```
Authorization: token <api_key>:<api_secret>
```

Session cookies are rejected. Requests with no credentials, or bad ones, never
reach the endpoint body. Tills get their key pair from `auth.login`; you can
also generate one by hand from **User → API Access → Generate Keys**.

#### How a till gets its credentials

`auth.login` is the one endpoint open to guests that hands out credentials: it
takes a username and password, verifies the password, and returns **that
cashier's** own API Key + Secret. The till stores those and uses them for every
call afterwards, so POS Opening Entries and POS Invoices are owned by the real
cashier.

The password is the only thing that opens it. Sign-in is refused for a disabled
user, or one not allowed on any POS Profile. Five failed attempts lock that
username out for five minutes, counted **per source address** — so a stranger
guessing at a cashier's name cannot lock that cashier out of their own till.
Sign-in is additionally rate limited to 20 attempts a minute per address.

The cashier's API secret is generated once and then reused, not rotated per
login — rotating it would sign the same person out of every other till. Sign-out
is therefore local: `auth.logout` only acknowledges, and the till erases the
credentials from its own config.

### Endpoints

All methods live under `bunny_pos_backend.api.` and are called as
`/api/method/bunny_pos_backend.api.<module>.<method>`.

| Method | Verb | Arguments | Returns |
| --- | --- | --- | --- |
| `auth.test_connection` | GET | – | Site, user and backend version. Confirms stored credentials still work. |
| `auth.server_info` | GET | – | **Guest.** Confirms Bunny POS is installed here. |
| `auth.login` | POST | `username`, `password` | **Guest.** Verifies a cashier's password and returns their own API Key + Secret. |
| `auth.logout` | POST | – | Acknowledges sign-out. The till drops the credentials locally. |
| `pos_session.get_pos_profiles` | GET | – | POS Profiles this user may open a shift on, with their modes of payment and each mode's type. |
| `pos_session.get_open_shift` | GET | `user` (optional) | The caller's open POS Opening Entry, or `null`. |
| `pos_session.open_shift` | POST | `pos_profile`, `opening_amounts` | Creates and submits a POS Opening Entry. |
| `items.get_items` | GET | `pos_profile`, `search_term`, `item_group`, `start`, `limit` | One page of sellable items with price and stock resolved. |
| `items.get_item_groups` | GET | `pos_profile` | Item groups that have sellable items, with counts. |
| `customers.search_customers` | GET | `pos_profile`, `search_term`, `limit` | Customers this profile may sell to. |
| `customers.create_customer` | POST | `pos_profile`, `customer_name`, `mobile_no`, `email_id` | Creates a walk-in Customer using the profile's defaults. |
| `invoices.create_invoice` | POST | `cart_data`, `customer`, `payments`, `pos_profile` | Creates and submits a POS Invoice. |

#### `items.get_items`

Returns the POS Profile's context plus one row per item, with the price taken
from the profile's selling price list and the quantity from the Bin for the
profile's warehouse — already merged, so the client makes one call:

```json
{
  "pos_profile": "Bunny POS Test",
  "company": "Test",
  "warehouse": "Stores - T",
  "currency": "PKR",
  "price_list": "Standard Selling",
  "customer": "Bunny Walk-in Customer",
  "items": [
    {
      "item_code": "BUNNY-002",
      "item_name": "Bunny Ceramic Mug",
      "rate": 850.0,
      "stock_qty": 60.0,
      "uom": "Nos",
      "currency": "PKR"
    }
  ]
}
```

`search_term` matches item code, item name or an exact barcode. `item_group`
narrows to one category and everything nested under it. The profile's own item
group filter and its `hide_unavailable_items` flag are always respected.

Results are paged: `start` and `limit` (default 100, max 500) with `has_more`
in the response, so a site with thousands of items never ships them all at once.

`stock_qty` is **sellable** quantity, not the raw Bin figure: it mirrors
ERPNext's own `get_stock_availability`, subtracting quantities held by
submitted POS Invoices that have not been consolidated yet (including items
inside Product Bundles). Returning the Bin quantity instead would let a till
build a cart the server then rejects at submit.

#### `pos_session.open_shift`

`opening_amounts` accepts either shape:

```json
[{"mode_of_payment": "Cash", "opening_amount": 5000}]
{"Cash": 5000}
```

Modes configured on the profile but left out are recorded as zero. A mode that
is not on the profile is rejected. Only one shift may be open per user.

#### `invoices.create_invoice`

```json
{
  "cart_data": [{"item_code": "BUNNY-002", "qty": 2}],
  "payments": [{"mode_of_payment": "Cash"}]
}
```

- The invoice is raised against the caller's **open shift**; `pos_profile` is
  optional and, if sent, must match that shift.
- `customer` falls back to the POS Profile's default customer.
- A cart row may carry `uom`, `rate` and `discount_percentage` as well as
  `item_code` and `qty`.
- `rate` and `discount_percentage` are honoured **only** when the POS Profile
  has *Allow Rate Change* / *Allow Discount Change* ticked; otherwise the
  request is refused outright rather than silently repriced. A discount outside
  0–100 is refused.
- The **UOM conversion factor is always read from the Item**, never taken from
  the request — it decides how much stock the sale consumes.
- A payment row with no `amount` (when it is the only row) settles the whole
  invoice, so the till never has to predict the server's tax calculation.
- Amounts that do not cover the total are rejected unless the POS Profile
  allows partial payment.
- **Over-tendering is how change works.** Send the amount actually handed over
  and ERPNext returns `change_amount`:

  ```json
  {"payments": [{"mode_of_payment": "Cash", "amount": 2000}]}
  ```

  against a 1,150 invoice returns `paid_amount: 2000`, `change_amount: 850`,
  `outstanding_amount: 0`. The POS Profile needs an
  *Account for Change Amount*.
- **Split payments** are just more rows:
  `[{"mode_of_payment": "Cash", "amount": 500}, {"mode_of_payment": "Credit Card", "amount": 650}]`.

### Manual test

`scripts/smoke_test.sh` walks the whole flow with curl, including the
authentication-failure case:

```bash
BASE_URL=http://localhost:8000 API_KEY=xxx API_SECRET=yyy \
  POS_PROFILE="Bunny POS Test" ./scripts/smoke_test.sh
```

### License

MIT
# bunny_pos_backend
