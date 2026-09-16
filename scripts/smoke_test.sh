#!/usr/bin/env bash
# Manual smoke test for the Bunny POS backend.
#
# Signs in the way a till does -- address + username + password -- and then
# uses the credentials that sign-in hands back for every other call.
#
# Usage:
#   BASE_URL=http://192.168.1.50:8000 \
#   USERNAME=cashier@shop.com PASSWORD='...' \
#   POS_PROFILE="Bunny POS Test" \
#   ITEM_1=BUNNY-002 ITEM_2=BUNNY-003 \
#   ./scripts/smoke_test.sh
#
# An existing API Key + Secret can be used instead, skipping sign-in:
#   BASE_URL=... API_KEY=xxx API_SECRET=yyy POS_PROFILE=... ITEM_1=... ./scripts/smoke_test.sh

set -u

BASE_URL="${BASE_URL:-http://localhost:8000}"
POS_PROFILE="${POS_PROFILE:?set POS_PROFILE}"
ITEM_1="${ITEM_1:?set ITEM_1}"
ITEM_2="${ITEM_2:-}"

API="${BASE_URL}/api/method/bunny_pos_backend.api"

hr() { printf '\n--- %s ---\n' "$1"; }

hr "server_info (guest: is this a Bunny POS server?)"
curl -s "${API}.auth.server_info"; echo

hr "login must fail with a wrong password"
curl -s -o /dev/null -w "  HTTP %{http_code} (expect 401)\n" \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"nobody@example.invalid","password":"wrong"}' "${API}.auth.login"

API_KEY="${API_KEY:-}"
API_SECRET="${API_SECRET:-}"

if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
  USERNAME="${USERNAME:?set USERNAME, or API_KEY and API_SECRET}"
  PASSWORD="${PASSWORD:?set PASSWORD, or API_KEY and API_SECRET}"

  hr "login"
  LOGIN=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}" "${API}.auth.login")
  echo "$LOGIN"

  # Pull the credentials out without assuming jq is installed.
  API_KEY=$(printf '%s' "$LOGIN" | sed -n 's/.*"api_key":[[:space:]]*"\([^"]*\)".*/\1/p')
  API_SECRET=$(printf '%s' "$LOGIN" | sed -n 's/.*"api_secret":[[:space:]]*"\([^"]*\)".*/\1/p')

  if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
    echo "  sign-in did not return credentials; stopping." >&2
    exit 1
  fi
fi

AUTH="Authorization: token ${API_KEY}:${API_SECRET}"

hr "protected endpoints must fail without credentials"
curl -s -o /dev/null -w "  HTTP %{http_code} (expect 401 or 403)\n" "${API}.auth.test_connection"

hr "protected endpoints must fail with a wrong key"
curl -s -o /dev/null -w "  HTTP %{http_code} (expect 401)\n" \
  -H "Authorization: token deadbeefdeadbee:0000000000badbad" "${API}.auth.test_connection"

hr "test_connection"
curl -s -H "$AUTH" "${API}.auth.test_connection"; echo

hr "get_pos_profiles"
curl -s -H "$AUTH" "${API}.pos_session.get_pos_profiles"; echo

hr "get_open_shift"
curl -s -H "$AUTH" "${API}.pos_session.get_open_shift"; echo

hr "open_shift (fails harmlessly if one is already open)"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"pos_profile\":\"${POS_PROFILE}\",\"opening_amounts\":[{\"mode_of_payment\":\"Cash\",\"opening_amount\":0}]}" \
  "${API}.pos_session.open_shift"; echo

hr "get_items"
curl -s -G -H "$AUTH" --data-urlencode "pos_profile=${POS_PROFILE}" "${API}.items.get_items"; echo

hr "create_invoice"
CART="[{\"item_code\":\"${ITEM_1}\",\"qty\":1}"
if [ -n "$ITEM_2" ]; then CART="${CART},{\"item_code\":\"${ITEM_2}\",\"qty\":2}"; fi
CART="${CART}]"
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"cart_data\":${CART},\"payments\":[{\"mode_of_payment\":\"Cash\"}]}" \
  "${API}.invoices.create_invoice"; echo
