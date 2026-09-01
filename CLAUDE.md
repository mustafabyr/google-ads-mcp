# google-ads-mcp — MD Kampüs fork

Fork of Google's official read-only Google Ads MCP server, extended with
**write (mutate) tools** so campaigns can be built and managed from an AI
assistant instead of the Google Ads UI.

## Why this fork exists

Google's server ships three read tools (`search`, `get_resource_metadata`,
`list_accessible_customers`) but its OAuth scope is already the full
`https://www.googleapis.com/auth/adwords`. Google's own comment in
`ads_mcp/utils.py` says access is restricted "by the tools this server
exposes" — not by the auth layer. So write capability needs no auth changes,
only new tools.

## Safety contract — do not weaken this

Enforced in code, not by convention. Every tool added here must keep it.

1. **Campaigns are always created PAUSED.** They cannot serve, cannot spend.
2. **Enabling a campaign or raising a budget requires `confirm_spend=True`.**
   The refusal is an exception carrying the daily and implied monthly figures,
   so the user can be shown real numbers before agreeing.
3. **Raising any bid requires `confirm_spend=True`** — ad group CPC, keyword
   CPC and device bid modifiers all raise cost per click. So does changing the
   bidding strategy of a campaign that is currently ENABLED, and moving a
   campaign to a larger budget.
4. **Lowering a budget or a bid, and pausing anything, never need
   confirmation** — stopping spend is always safe.
5. **Removal requires `confirm_remove=True`** and a statement of what is lost.
   This includes removing a NEGATIVE keyword, which re-opens blocked traffic,
   and changing a keyword's match type, which Google can only do by removing
   and re-adding the keyword — its history is discarded.
6. **Every write reads the object back** and returns its real post-write
   state, never the intended state. Read-backs return enum NAMES, not the raw
   integers proto-plus defaults to: a model reading `"status": 3` learns
   nothing, and the status gates compare against the name.
7. Creation is gated at the CAMPAIGN level only. Ad groups, ads and keywords
   are created ENABLED underneath a paused campaign — one clear gate, not
   several. Bids are gated separately, because they raise cost per click
   regardless of which campaign they sit under.

## Money units

Google Ads uses **micros**: 1 unit of account currency = 1_000_000.
Tools take the human number (`500` means 500 TRY in a TRY account) and convert
internally. **Never convert currencies** — send the account's own currency.

## Layout

New modules go in `ads_mcp/tools/` as a module defining a `FastMCP("name")`
instance with `@tool`-decorated functions. `coordinator.py` discovers and
mounts them automatically; enable the namespace in `ads_mcp/tools_config.yaml`.

| Module | Namespace | Contents |
|---|---|---|
| `campaigns.py` | `campaigns` | budgets, search campaigns, status, settings, bidding strategy, device bid modifiers, geo targeting, ad schedule, location lookup |
| `reports.py` | `reports` | campaign list, performance, structure, search terms, wasted spend |
| `extensions.py` | `extensions` | sitelinks, callouts, structured snippets |
| `conversions.py` | `conversions` | conversion actions, tag snippets, campaign conversion goals |
| `adgroups.py` | `adgroups` | ad groups, responsive search ads and their text, ad status, keywords and their bids/match types/status, negative keywords |
| `core.py`, `search.py`, `get_resource_metadata.py` | — | Google's originals, unmodified |

## Environment

    GOOGLE_ADS_DEVELOPER_TOKEN      required
    GOOGLE_ADS_LOGIN_CUSTOMER_ID    the MCC id, for multi-account access
    GOOGLE_ADS_MCP_OAUTH_CLIENT_ID
    GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET
    GOOGLE_ADS_MCP_BASE_URL

**This repository is public.** Credentials go in Secret Manager, never in a
file here. `.gitignore` already covers `google-ads.yaml`, `.env`,
`client_secret*.json`, `credentials.json`, `token.json`.

## Account setup (done — 1 September 2026)

| Thing | Value |
|---|---|
| Production manager (MCC) | **<production MCC>** "Bayar Reklam" — TRY, Europe/Istanbul |
| Developer token | Issued, **Test access level**. Basic Access applied for 1 Sep 2026, ~5 business days |
| Google Cloud project | `md-kampus` / number **<cloud project number>** — Google Ads API enabled |
| OAuth consent screen | **In production** (published), External. This removes the 7-day refresh-token expiry that Testing status imposes |
| OAuth client | Web application, created in the same project |
| Test manager | **<test manager>** "Google Ads MCP", under <test-account owner, see private note> |
| Test account | **<test account>** "MD Kampus Test" — TRY, Europe/Istanbul |

Test and production are separate hierarchies and never interact. Against test
accounts you still use the PRODUCTION developer token — that is Google's rule,
not a mistake. Only `GOOGLE_ADS_LOGIN_CUSTOMER_ID` and the OAuth user change:

    # testing today, with the Test-level token
    GOOGLE_ADS_LOGIN_CUSTOMER_ID=<test manager>   # the TEST manager
    customer_id passed to tools = <test account>  # the TEST account
    OAuth sign-in = <test-account owner, see private note>

    # after Basic access arrives
    GOOGLE_ADS_LOGIN_CUSTOMER_ID=<production MCC>   # the production MCC
    OAuth sign-in = <production owner, see private note>

The OAuth client needs `http://localhost:8080/auth/callback` in its authorized
redirect URIs for local runs (FastMCP GoogleProvider's default).

Scope declared to Google in the Basic Access application, which the tools must
not exceed without telling Google first: campaign type **Search only**;
capabilities **Campaign Creation, Campaign Management, Reporting**. Not Account
Creation, not Account Management, not Keyword Planning Services.

## Google Ads API v25 traps, all hit for real

These cost a debugging session each. Check them in every new tool.

- `FieldMask` is a protobuf well-known type, **not** a Google Ads type:
  `client.get_type("FieldMask")` fails. Use
  `field_mask_pb2.FieldMask(paths=[...])`.
- An update mask must name a **leaf** field. Masking a message that has
  subfields (`target_spend`, `maximize_conversions`) is rejected with
  `FIELD_HAS_SUBFIELDS`; mask `target_spend.cpc_bid_ceiling_micros` instead.
- proto-plus messages have no `CopyFrom`. Use `client.copy_from(dst, src)`.
- `campaign.start_date` / `end_date` are now `start_date_time` /
  `end_date_time`, format `"yyyy-MM-dd HH:mm:ss"` in the account's time zone,
  on both the write and the read side.
- `contains_eu_political_advertising` is required on every campaign create.
- A keyword's match type is immutable. Changing it means remove + re-create,
  which loses history and issues a new criterion id.
- `include_in_conversions_metric` cannot be set when creating a conversion
  action; Google derives it from the category.
- A GAQL query must SELECT any field it filters on. `FROM campaign_asset
  WHERE campaign.id = X` fails unless `campaign.id` is also selected.
- Target CPA and Target ROAS are no longer strategies of their own. Target CPA
  is `maximize_conversions.target_cpa_micros`; Target ROAS is
  `maximize_conversion_value.target_roas`. The standalone `target_cpa` /
  `target_roas` messages are rejected on Search campaigns with
  `OPERATION_NOT_PERMITTED_FOR_CONTEXT`.

## Still to build

- Keyword research (phase 5): `KeywordPlanIdeaService`
- Performance Max and other campaign types (phase 6)
- Deploy to Cloud Run (`min_instances=0`), add as a custom connector in Claude
- Upstream has a merged PR documenting `login-customer-id` on Cloud Run —
  read it before deploying
