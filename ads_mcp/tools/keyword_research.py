# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read-only keyword research: real search volume, competition and bid ranges.

This is the one place in the server that reaches a planning service rather
than the reporting API, so it does not follow the read-back pattern -- there
is nothing written to read back. It writes nothing and needs no confirmation.

The bid figures are what OTHER advertisers pay for the top of the page, in the
account's own currency. They are a market signal, not a recommendation, and
they are the reason to research before adding keywords: a term with high
volume and a bid range far above what the account can afford is a term to skip
deliberately, not to discover after it has spent the budget.
"""

from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

keyword_research_mcp = FastMCP("keyword_research")

_MICROS = 1_000_000
_MAX_SEED_KEYWORDS = 20
_NETWORKS = {"GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"}


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _from_micros(micros: Any) -> Optional[float]:
    if not micros:
        return None
    return round(int(micros) / _MICROS, 2)


@keyword_research_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def research_keywords(
    customer_id: str,
    keywords: Optional[List[str]] = None,
    page_url: Optional[str] = None,
    location_ids: Optional[List[str]] = None,
    language_id: str = "1037",
    network: str = "GOOGLE_SEARCH",
    include_adult: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """Finds keyword ideas with real search volume, competition and bid ranges.

    Give seed keywords, a page URL, or both -- Google mines the page for terms
    when a URL is supplied, which usually beats guessing seeds by hand.

    Volumes are monthly averages for the locations and language requested, so
    the location matters: researching Turkish terms against the default US
    geo returns numbers that describe the wrong market. Pass location_ids from
    find_location_ids.

    Args:
        customer_id: The Google Ads account id.
        keywords: Up to 20 seed terms, e.g. ["yks kursu", "tyt hazirlik"].
        page_url: A page to mine for ideas, e.g. "https://mdkampus.com/yks".
            Google mines its own index, so a page it has not crawled returns
            an empty list rather than an error -- an empty result means "no
            ideas for this page", not "the call failed".
        location_ids: Geo target constant ids from find_location_ids, e.g.
            ["21069"] for Istanbul. Omit to let Google use the account default.
        language_id: Language constant id. 1037 is Turkish, 1000 English.
        network: GOOGLE_SEARCH, or GOOGLE_SEARCH_AND_PARTNERS for a wider and
            usually more optimistic estimate.
        include_adult: True to include adult keywords.
        limit: Maximum ideas to return, ordered by monthly searches.

    Returns:
        `ideas`, each with avg_monthly_searches, competition, and the top-of-
        page bid range other advertisers pay, in the account's own currency.
    """
    customer_id = _clean_id(customer_id)
    if not keywords and not page_url:
        raise ValueError("Give keywords, page_url, or both.")
    if keywords and len(keywords) > _MAX_SEED_KEYWORDS:
        raise ValueError(
            f"Google accepts at most {_MAX_SEED_KEYWORDS} seed keywords, got "
            f"{len(keywords)}."
        )
    if page_url and not str(page_url).startswith(("http://", "https://")):
        raise ValueError("page_url must start with http:// or https://")
    network = network.upper()
    if network not in _NETWORKS:
        raise ValueError(f"network must be one of {sorted(_NETWORKS)}.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.include_adult_keywords = include_adult
    request.keyword_plan_network = getattr(
        client.enums.KeywordPlanNetworkEnum, network
    )
    request.language = client.get_service(
        "GoogleAdsService"
    ).language_constant_path(_clean_id(language_id))
    for location_id in location_ids or []:
        request.geo_target_constants.append(
            f"geoTargetConstants/{_clean_id(location_id)}"
        )

    # Which seed field is set decides how Google reads the request.
    if keywords and page_url:
        request.keyword_and_url_seed.url = page_url
        request.keyword_and_url_seed.keywords.extend(keywords)
    elif keywords:
        request.keyword_seed.keywords.extend(keywords)
    else:
        request.url_seed.url = page_url

    response = service.generate_keyword_ideas(request=request)

    ideas = []
    for result in response:
        m = result.keyword_idea_metrics
        ideas.append(
            {
                "keyword": result.text,
                "avg_monthly_searches": int(m.avg_monthly_searches or 0),
                "competition": m.competition.name,
                "competition_index": int(m.competition_index or 0),
                "top_of_page_bid_low": _from_micros(
                    m.low_top_of_page_bid_micros
                ),
                "top_of_page_bid_high": _from_micros(
                    m.high_top_of_page_bid_micros
                ),
            }
        )
        if len(ideas) >= limit:
            break

    ideas.sort(key=lambda i: i["avg_monthly_searches"], reverse=True)

    return {
        "seed_keywords": keywords or [],
        "seed_url": page_url,
        "location_ids": location_ids or [],
        "language_id": language_id,
        "network": network,
        "count": len(ideas),
        "ideas": ideas,
        "note": (
            "Volumes are monthly averages for the requested locations and "
            "language. The bid range is what other advertisers pay for the "
            "top of the page, in this account's own currency -- a market "
            "signal, not a recommendation."
        ),
    }


@keyword_research_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_keyword_historical_metrics(
    customer_id: str,
    keywords: List[str],
    location_ids: Optional[List[str]] = None,
    language_id: str = "1037",
    network: str = "GOOGLE_SEARCH",
) -> Dict[str, Any]:
    """Gets volume and bid data for keywords you already have in mind.

    Unlike research_keywords this suggests nothing -- it answers "what are
    these specific terms worth?" for a list you already wrote, which is the
    check to run before add_keywords rather than after.

    Args:
        customer_id: The Google Ads account id.
        keywords: The exact terms to look up.
        location_ids: Geo target constant ids from find_location_ids.
        language_id: Language constant id. 1037 is Turkish, 1000 English.
        network: GOOGLE_SEARCH or GOOGLE_SEARCH_AND_PARTNERS.

    Returns:
        One row per keyword. A term Google has no data for comes back with
        avg_monthly_searches 0 rather than being dropped, so a caller can
        tell "no demand" from "not asked about".
    """
    customer_id = _clean_id(customer_id)
    if not keywords:
        raise ValueError("Give at least one keyword.")
    network = network.upper()
    if network not in _NETWORKS:
        raise ValueError(f"network must be one of {sorted(_NETWORKS)}.")

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = customer_id
    request.keywords.extend(keywords)
    request.keyword_plan_network = getattr(
        client.enums.KeywordPlanNetworkEnum, network
    )
    request.language = client.get_service(
        "GoogleAdsService"
    ).language_constant_path(_clean_id(language_id))
    for location_id in location_ids or []:
        request.geo_target_constants.append(
            f"geoTargetConstants/{_clean_id(location_id)}"
        )

    response = service.generate_keyword_historical_metrics(request=request)

    found = {}
    for result in response.results:
        m = result.keyword_metrics
        found[result.text.lower()] = {
            "keyword": result.text,
            "avg_monthly_searches": int(m.avg_monthly_searches or 0),
            "competition": m.competition.name,
            "competition_index": int(m.competition_index or 0),
            "top_of_page_bid_low": _from_micros(m.low_top_of_page_bid_micros),
            "top_of_page_bid_high": _from_micros(m.high_top_of_page_bid_micros),
            "close_variants": list(result.close_variants),
        }

    rows = []
    for keyword in keywords:
        rows.append(
            found.get(
                keyword.lower(),
                {
                    "keyword": keyword,
                    "avg_monthly_searches": 0,
                    "competition": "UNKNOWN",
                    "competition_index": 0,
                    "top_of_page_bid_low": None,
                    "top_of_page_bid_high": None,
                    "close_variants": [],
                    "no_data": True,
                },
            )
        )

    return {
        "location_ids": location_ids or [],
        "language_id": language_id,
        "network": network,
        "keywords": rows,
    }
