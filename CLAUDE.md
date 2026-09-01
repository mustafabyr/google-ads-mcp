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
3. **Lowering a budget and pausing never need confirmation** — stopping spend
   is always safe.
4. **Removal requires `confirm_remove=True`** and a statement of what is lost.
5. **Every write reads the object back** and returns its real post-write
   state, never the intended state.
6. Spend is gated at the CAMPAIGN level only. Ad groups, ads and keywords are
   created ENABLED underneath a paused campaign — one clear gate, not several.

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
| `campaigns.py` | `campaigns` | budgets, search campaigns, status, geo targeting, ad schedule, location lookup |
| `adgroups.py` | `adgroups` | ad groups, responsive search ads, keywords, negative keywords |
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

## Account setup (in progress)

- Manager (MCC) account: **not created yet** — currency TRY, time zone
  Europe/Istanbul, both permanent once set.
- Developer token: **not applied for yet**. Test access is instant; Basic
  access takes ~5 business days and is enough (15,000 operations/day).
- Test accounts need their own separate test manager hierarchy; they never
  serve ads or spend, and their metrics are always empty.
- Scope: MD Kampüs's own account plus a handful of client accounts (≤5 for
  now). Under $10k/month spend an MCC allows 50 active accounts.

## Still to build

- Test the write tools end-to-end against a test account
- Conversion tracking tools
- Deploy to Cloud Run (`min_instances=0`), add as a custom connector in Claude
- Upstream has a merged PR documenting `login-customer-id` on Cloud Run —
  read it before deploying
