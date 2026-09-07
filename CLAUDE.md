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

## Claims and thresholds

Every numeric threshold this repo or its skills rely on is registered in
`docs/kaynaklar.md` with a status: DOKUMAN (Google-documented), GOZLEM
(observed API behaviour, with a request id), KOD (read from the client
library), or KONVANSIYON (industry convention, NOT verified against current
Google documentation).

Adding a threshold to a tool docstring or a skill without a row in that file
is not allowed. A KONVANSIYON row is never presented to a client as "Google
says so". Rows older than 12 months get re-verified or demoted.

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
| `campaigns.py` | `campaigns` | budgets, search campaigns, status, settings, bidding strategy, device bid modifiers, geo targeting (location ids + proximity radius + presence-vs-interest), language targeting, final URL suffix and tracking template, ad schedule with bid modifiers, location and language lookup |
| `campaign_types.py` | `campaign_types` | Demand Gen and YouTube campaigns, YouTube video assets |
| `pmax.py` | `pmax` | Performance Max campaigns, asset groups, image assets, signals |
| `keyword_research.py` | `keyword_research` | search volume, competition, top-of-page bid ranges |
| `reports.py` | `reports` | campaign list, performance, structure, search terms, wasted spend |
| `extensions.py` | `extensions` | sitelinks, callouts, structured snippets, call extensions (add + repoint) |
| `conversions.py` | `conversions` | conversion actions (create + update), tag snippets, campaign conversion goals |
| `adgroups.py` | `adgroups` | ad groups, responsive search ads and their text, ad status, keywords and their bids/match types/status, negative keywords |
| `audit.py` | `audit` | the agency checklist as one call: `run_account_audit` for one account, `run_portfolio_audit` for every account under an MCC with findings grouped across accounts |
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

## Account setup

Concrete ids, emails and the developer token live in `.env` and in the private
setup note — **not here.** This repository is public.

| Thing | Where its value lives |
|---|---|
| Production manager (MCC) | private note; used as `GOOGLE_ADS_LOGIN_CUSTOMER_ID` |
| Test manager and test account | private note |
| Developer token | `.env`, `GOOGLE_ADS_DEVELOPER_TOKEN` |
| Google Cloud project / OAuth client | private note; client id and secret in `.env` |

What is worth writing down is the shape, not the numbers:

- Test and production are separate hierarchies and never interact.
- Against test accounts you still use the PRODUCTION developer token. That is
  Google's rule, not a mistake. Only `GOOGLE_ADS_LOGIN_CUSTOMER_ID` and the
  OAuth user change between the two.
- A test account never spends and its metrics stay permanently empty, so
  anything performance-shaped can only be verified for structure there.
- Keep the OAuth consent screen **published**, not in Testing: Testing status
  expires refresh tokens after 7 days.

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
- `include_in_conversions_metric` cannot be set when CREATING a conversion
  action; Google derives it from the category. It can only be changed
  afterwards, with `update_conversion_action`. An action that records fine
  but sits outside that metric starves every conversion-based bidding
  strategy in the account, so this is the first thing to check on an
  inherited account.
- A call extension can sit on `USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION`
  with an EMPTY `call_conversion_action`: the button shows, people call, and
  the call is counted nowhere. Check that pair on every inherited account and
  repoint it with `update_call_extension` -- removing and re-creating the
  asset fixes it too but detaches the extension's history.
- A GOOGLE_HOSTED conversion action (Business Profile local actions: click
  to call, directions, website visits) is owned by Google and cannot be
  mutated at all -- `MUTATE_NOT_ALLOWED` on `type` plus `IMMUTABLE_FIELD` on
  `include_in_conversions_metric`. The only way to make one bid-worthy is to
  make its CATEGORY biddable on the campaign, via campaign conversion goals.
- `CampaignConversionGoalService.mutate` takes no `partial_failure`: a batch
  is all or nothing, so goals are sent one operation per call and refusals
  are collected per goal.
- `campaign_conversion_goal` rows are keyed by (category, ORIGIN), and an
  account can expose rows whose origin is `UNKNOWN`. Those have no valid
  resource name: building one emits the literal "UNKNOWN" and the API
  rejects the entire batch with `BAD_RESOURCE_ID`. Skip them, and send goal
  mutations with `partial_failure=True` so one Google-hosted goal the API
  refuses does not discard the rest.
- Geo targeting for a single-location business is a RADIUS, not a district:
  districts are administrative and routinely reach 15km inland while the
  street two minutes away sits in the next one. Use `set_campaign_proximity`,
  and pass `replace_existing_locations=True` when switching, or the wider
  district criterion stays and wins.
- `campaign.geo_target_type_setting.positive_geo_target_type` defaults to
  PRESENCE_OR_INTEREST, which serves ads to people merely INTERESTED in the
  area from anywhere. For a local business it must be PRESENCE. This is the
  commonest silent leak in an inherited local account, and it applies to
  campaigns this server creates too -- `create_search_campaign` does not set
  it, so fix it with `set_campaign_geo_target_type` right after creating.
- A campaign created through the API carries NO language criterion, and no
  language criterion means ALL languages. A Turkish campaign therefore serves
  against English-language sessions until `set_campaign_languages` is called.
  Neither a campaign report nor `list_campaign_targeting` makes the omission
  look like a problem: the LANGUAGE section is simply absent.
- `TargetImpressionShare.location_fraction_micros` is micros of a WHOLE, not
  the money micros used everywhere else in this repo: 1% is 10,000 and 100%
  is 1,000,000. Passing a percentage straight through as money micros asks
  for a 0.008% impression share.
- `TargetImpressionShare.cpc_bid_ceiling_micros` is REQUIRED, not optional --
  the client library says so in the field's own docstring. Without a ceiling
  the strategy pays whatever holding the position costs, which is how an
  own-brand campaign ends up at several times a brand click's worth.
- `campaign.final_url_suffix` must be a BARE query string: a leading `?` or
  `&` is rejected. It is also per campaign, so a per-campaign `utm_campaign`
  value cannot be set at account level -- and it is the only place to put UTM
  parameters on an existing RSA, whose `final_url` is immutable.
- A GAQL field path cannot wrap across lines. Splitting
  `customer.call_reporting_setting.call_conversion_reporting_enabled` after
  the second dot to fit 79 columns fails with `UNEXPECTED_INPUT: unexpected
  input ..` -- and if the query sits AFTER a mutate, the write has already
  landed while the tool reports failure. Keep long field paths on one line.
- An RSA's `final_url` cannot be edited: `update_ad_text` only replaces
  headlines and descriptions. A wrong landing page therefore costs the whole
  ad -- create a replacement with the right URL, then pause the bad one. So
  NEVER type a domain from memory or from what a business "probably" uses:
  copy the final URL from an already-approved ad in the same account, or
  fetch the page and confirm it resolves, before creating the ad.
- `set_ad_status` builds an UPDATE operation, so status REMOVED is refused
  with `INVALID_ENUM_VALUE: Enum value 'REMOVED' cannot be used`. Removal
  needs a `remove` operation carrying the resource name, not an update.
  Until that is implemented, PAUSED is the reachable end state.
- A new tool module is NOT picked up on its own. `coordinator.py` finds the
  FastMCP sub-server by reflection, but then asks `tools_config.yaml` whether
  that namespace is enabled -- and a namespace missing from the file resolves
  to False. A new module without a line there registers zero tools, with no
  error and no log. Add the namespace to `ads_mcp/tools_config.yaml` in the
  same commit as the module.
- Never call one tool from another through FastMCP's wrapper (`tool.fn`).
  Put the shared logic in a plain private helper and have both call that;
  the wrapper's shape is framework internals and changes between versions.
- `metrics.conversions` counts ONLY conversion actions whose
  `include_in_conversions_metric` is true -- the number Smart Bidding
  optimises against. `metrics.all_conversions` counts everything recorded.
  A report that selects only the first reads as "this account converts
  nothing" on an account doing hundreds a week. Select both, and say so when
  they differ.
- `metrics.search_budget_lost_impression_share` is campaign-only: a budget
  belongs to a campaign, so selecting it FROM ad_group is refused with
  "could not support requested resources: 'AD_GROUP'". Rank-lost share and
  plain impression share ARE available at ad group level.
- `change_event` filters on `change_event.change_date_time`, not
  `segments.date`, and Google keeps only about 30 days of it.
- proto-plus returns UNSET sub-fields as `""`, `0` or `"UNSPECIFIED"` rather
  than omitting them, so one criterion row carries fifteen empty fields for
  the one that is set. Filter before returning, or the output is mostly
  scaffolding -- 13 campaign extensions came back as roughly 8k tokens of it.
- `quality_info.quality_score` of 0 means "not enough traffic to score",
  not "scored badly". Returning the 0 invites pausing a keyword that simply
  has not run.
- Location extensions cannot be created through the API. Google syncs the
  advertiser's Business Profile into an AssetSet (`LOCATION_SYNC`), and the
  API's job is to attach that set to campaigns. If no such asset set exists,
  the Business Profile is not linked and only the Google Ads interface can
  link it.
- proto-plus renames a message field that collides with a Python builtin,
  so a message's `type` arrives in the dict as `type_`. Reading `"type"`
  returns None silently, and a FILTER written against it matches nothing --
  that is how list_location_asset_sets reported "no Business Profile linked"
  on an account that had one. Read enums through the `_enum` helper.
- `Label` has no `description` field; the text lives on
  `label.text_label.description` next to `background_color`.
- `ConversionUploadService` is closed to new integrations. Google answers
  "Usage of ConversionUploadService.UploadClickConversions is limited to
  existing users" and points at the separate Data Manager API. The call
  still returns success, so the per-row `api_errors` must be read before
  telling anyone the conversions were recorded.
- Every tool that CREATES something needs the tool that removes it, in the
  same commit. An experiment, a shared set or a demographic exclusion that
  can be created and not undone leaves the account dirty and leaves the
  next session guessing.
- An ad schedule turns a day OFF by that day being ABSENT, so a tool that
  only adds entries cannot express "not on Sunday" at all: passing
  Monday-to-Saturday leaves an existing Sunday entry serving, and the
  read-back looks fine because the six days asked for are all there.
  `set_campaign_ad_schedule` therefore clears the existing schedule by
  default -- a tool named "set" must set.
- A GAQL query must SELECT any field it filters on. `FROM campaign_asset
  WHERE campaign.id = X` fails unless `campaign.id` is also selected.
- Performance Max has to be built in ONE atomic `GoogleAdsService.mutate`
  with temporary negative resource names. Brand Guidelines is on by default
  and makes Google check, at campaign-create time, that a business name and
  logo are already linked as CampaignAssets — which cannot exist before the
  campaign. Temporary names break that cycle.
- In that atomic request, TEXT assets must be created beforehand with real
  resource names. Google does not resolve a temporary asset name when
  validating the asset group that references it; it reports
  `NOT_ENOUGH_HEADLINE_ASSET` instead, which points at the wrong problem.
- `url_expansion_opt_out` no longer exists on Campaign in v25.
- Demand Gen looks like Performance Max but does NOT use asset groups —
  `CANNOT_ADD_ASSET_GROUP_FOR_CAMPAIGN_TYPE`. It keeps ad groups, with the
  creative in one `demand_gen_multi_asset_ad`.
- VIDEO campaigns reject `MAXIMIZE_CONVERSIONS` (use `target_cpm` /
  `target_cpa`) and, on the test account, reject creation outright with
  `MUTATE_NOT_ALLOWED`.
- Target CPA and Target ROAS are no longer strategies of their own. Target CPA
  is `maximize_conversions.target_cpa_micros`; Target ROAS is
  `maximize_conversion_value.target_roas`. The standalone `target_cpa` /
  `target_roas` messages are rejected on Search campaigns with
  `OPERATION_NOT_PERMITTED_FOR_CONTEXT`.

## The audit is where a lesson goes to live

A mistake found in one account is worth almost nothing as a note in a
report someone read once. It is worth something as a check in
`audit.py`, because then it runs against every account, forever, without
anyone remembering to look.

So: when a real problem is found in a live account — not a hypothesis, a
problem that cost money or reach — the fix is not finished until there
is a check for it in `audit.py`. Every check in that file has a live
account behind it, and none of them were invented from a best-practices
list. Keep it that way; a checklist padded with plausible-sounding
checks stops being read.

## Still to build

- Confirm on a PRODUCTION account whether `create_youtube_campaign` works.
  Google returns `MUTATE_NOT_ALLOWED` for VIDEO campaign creation on the test
  account, which could be a test-account restriction or the API closing video
  creation as Video Action moves into Demand Gen. Everything except the
  campaign create is exercised.
- Audience creation — `add_pmax_audience_signal` consumes audiences but
  nothing here makes them, and the test account has none, so that one path is
  verified only as far as the API error.
- Deploy to Cloud Run (`min_instances=0`), add as a custom connector in Claude
- Upstream has a merged PR documenting `login-customer-id` on Cloud Run —
  read it before deploying
