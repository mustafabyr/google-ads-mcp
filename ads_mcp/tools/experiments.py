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

"""Campaign experiments: the honest way to test a change.

The alternative people reach for -- building a second campaign next to the
first -- does not measure anything cleanly. The two campaigns compete in the
same auctions, one takes the queries the other would have had, and whichever
wins may simply have had the higher Ad Rank on the day. An experiment splits
the SAME traffic between a control and a treatment, so the difference is
attributable.

An experiment costs no extra budget: the split divides the existing campaign's
budget between the arms in the same proportion as the traffic.

The shape of the API, which the tools below hide:
  1. An Experiment holds the test.
  2. Two ExperimentArms hang off it. The CONTROL arm points at the live
     campaign. The TREATMENT arm points at nothing -- Google generates a copy
     of the control campaign for it, and that copy is what gets modified.
  3. schedule_experiment starts it. Until then nothing is live.
The treatment campaign's id only exists after scheduling, so changes to the
treatment are made afterwards, against that new campaign.
"""

import datetime
from typing import Any, Dict, List, Optional

import proto
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

experiments_mcp = FastMCP("experiments")

# Google's own floor for a Search experiment arm.
_MIN_TRAFFIC_SPLIT = 1
_MAX_TRAFFIC_SPLIT = 99


def _clean_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value) if ch.isdigit())
    if not cleaned:
        raise ValueError(f"Not a usable id: {value!r}")
    return cleaned


def _enum(block: Dict[str, Any], field: str) -> Any:
    """Reads an enum field from a proto-plus dict.

    proto-plus renames fields that collide with Python builtins, so the
    `type` field of a message arrives in the dict as `type_`. Reading
    `"type"` therefore silently returns None -- which is worse than an
    error, because a filter written against it quietly matches nothing.
    """
    if block is None:
        return None
    if field in block:
        return block[field]
    return block.get(field + "_")


def _read_back(customer_id: str, query: str) -> List[Dict[str, Any]]:
    ga_service = utils.get_googleads_service("GoogleAdsService")
    rows = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            rows.append(
                proto.Message.to_dict(row, use_integers_for_enums=False)
            )
    return rows


def _experiments(
    customer_id: str, experiment_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    where = ["experiment.status != 'REMOVED'"]
    if experiment_id:
        where.append(f"experiment.experiment_id = {_clean_id(experiment_id)}")
    rows = _read_back(
        customer_id,
        f"""
        SELECT experiment.experiment_id, experiment.name, experiment.status,
               experiment.type, experiment.start_date, experiment.end_date,
               experiment.description, experiment.resource_name
        FROM experiment
        WHERE {' AND '.join(where)}
        """,
    )
    out = []
    for row in rows:
        e = row["experiment"]
        out.append(
            {
                "experiment_id": e.get("experiment_id"),
                "name": e.get("name"),
                "status": e.get("status"),
                "type": _enum(e, "type"),
                "start_date": e.get("start_date"),
                "end_date": e.get("end_date"),
                "description": e.get("description"),
                "resource_name": e.get("resource_name"),
            }
        )
    return out


@experiments_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_experiments(
    customer_id: str,
) -> Dict[str, Any]:
    """Lists the account's campaign experiments and their arms.

    Status is what matters: SETUP means it was created but never scheduled,
    so it is testing nothing. That is the usual way an experiment gets
    forgotten.

    Args:
        customer_id: The Google Ads account id.

    Returns:
        `experiments`, each with its control and treatment campaigns.
    """
    customer_id = _clean_id(customer_id)
    experiments = _experiments(customer_id)

    arms_by_experiment: Dict[str, List[Dict[str, Any]]] = {}
    for row in _read_back(
        customer_id,
        """
        SELECT experiment_arm.experiment, experiment_arm.name,
               experiment_arm.control, experiment_arm.traffic_split,
               experiment_arm.campaigns, experiment_arm.in_design_campaigns
        FROM experiment_arm
        """,
    ):
        arm = row["experiment_arm"]
        arms_by_experiment.setdefault(arm["experiment"], []).append(
            {
                "name": arm.get("name"),
                "is_control": bool(arm.get("control")),
                "traffic_split": arm.get("traffic_split"),
                "campaign_ids": [
                    c.rsplit("/", 1)[-1] for c in arm.get("campaigns") or []
                ],
                "draft_campaign_ids": [
                    c.rsplit("/", 1)[-1]
                    for c in arm.get("in_design_campaigns") or []
                ],
            }
        )

    for experiment in experiments:
        experiment["arms"] = arms_by_experiment.get(
            experiment["resource_name"], []
        )
        if experiment["status"] == "SETUP":
            experiment["warning"] = (
                "Created but never scheduled, so it is not running and not "
                "measuring anything. schedule_experiment starts it."
            )

    return {
        "experiments": experiments,
        "count": len(experiments),
    }


@experiments_mcp.tool
def create_experiment(
    customer_id: str,
    base_campaign_id: str,
    name: str,
    traffic_split: int = 50,
    days: int = 30,
    description: Optional[str] = None,
    start_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a campaign experiment in SETUP, without starting it.

    Nothing is live and nothing is spent until schedule_experiment is called,
    which is deliberate: the treatment campaign that Google generates is what
    you edit, and it does not exist until then.

    A 50/50 split reaches significance soonest. Splits below about 20% take
    so long on a small account that the test finishes after the decision was
    needed.

    Args:
        customer_id: The Google Ads account id.
        base_campaign_id: The live campaign to test against. It keeps running
            as the control.
        name: What is being tested, e.g. "tCPA 150 vs Maximize Conversions".
        traffic_split: Percentage of traffic sent to the TREATMENT arm,
            1-99. The control gets the rest.
        days: How long to run. Under 14 days rarely settles on a small
            account.
        description: Optional note on what the treatment will change.
        start_date: YYYY-MM-DD. Defaults to tomorrow.

    Returns:
        The experiment id and what to do next.
    """
    customer_id = _clean_id(customer_id)
    base_campaign_id = _clean_id(base_campaign_id)
    if not _MIN_TRAFFIC_SPLIT <= int(traffic_split) <= _MAX_TRAFFIC_SPLIT:
        raise ValueError(
            f"traffic_split must be between {_MIN_TRAFFIC_SPLIT} and "
            f"{_MAX_TRAFFIC_SPLIT}."
        )
    if int(days) < 1:
        raise ValueError("days must be at least 1.")

    begins = (
        datetime.date.fromisoformat(start_date)
        if start_date
        else datetime.date.today() + datetime.timedelta(days=1)
    )
    if begins <= datetime.date.today():
        raise ValueError(
            f"start_date must be in the future; got {begins.isoformat()}."
        )
    ends = begins + datetime.timedelta(days=int(days))

    base = _read_back(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type
        FROM campaign WHERE campaign.id = {base_campaign_id}
        """,
    )
    if not base:
        raise ValueError(
            f"No campaign {base_campaign_id} in account {customer_id}."
        )
    base_campaign = base[0]["campaign"]

    client = utils.get_googleads_client()
    experiment_service = utils.get_googleads_service("ExperimentService")
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("ExperimentOperation")
    experiment = operation.create
    experiment.name = str(name).strip()
    experiment.type_ = client.enums.ExperimentTypeEnum.SEARCH_CUSTOM
    experiment.suffix = "[deney]"
    experiment.status = client.enums.ExperimentStatusEnum.SETUP
    experiment.start_date = begins.isoformat()
    experiment.end_date = ends.isoformat()
    if description:
        experiment.description = description

    response = experiment_service.mutate_experiments(
        customer_id=customer_id, operations=[operation]
    )
    experiment_resource = response.results[0].resource_name
    experiment_id = experiment_resource.rsplit("/", 1)[-1]

    arm_service = utils.get_googleads_service("ExperimentArmService")
    arm_operations = []

    control_operation = client.get_type("ExperimentArmOperation")
    control = control_operation.create
    control.experiment = experiment_resource
    control.name = "control"
    control.control = True
    control.traffic_split = 100 - int(traffic_split)
    control.campaigns.append(
        campaign_service.campaign_path(customer_id, base_campaign_id)
    )
    arm_operations.append(control_operation)

    treatment_operation = client.get_type("ExperimentArmOperation")
    treatment = treatment_operation.create
    treatment.experiment = experiment_resource
    treatment.name = "treatment"
    treatment.control = False
    treatment.traffic_split = int(traffic_split)
    arm_operations.append(treatment_operation)

    arm_service.mutate_experiment_arms(
        customer_id=customer_id, operations=arm_operations
    )

    return {
        "experiment_id": experiment_id,
        "resource_name": experiment_resource,
        "name": name,
        "base_campaign": {
            "campaign_id": base_campaign["id"],
            "name": base_campaign["name"],
        },
        "traffic_split_treatment_pct": int(traffic_split),
        "start_date": begins.isoformat(),
        "end_date": ends.isoformat(),
        "status": "SETUP",
        "next_step": (
            "Nothing is running yet. schedule_experiment starts it and "
            "creates the treatment campaign; only then can the treatment be "
            "edited, using the campaign id that list_experiments reports for "
            "the treatment arm."
        ),
        "verified": _experiments(customer_id, experiment_id),
    }


@experiments_mcp.tool
def schedule_experiment(
    customer_id: str,
    experiment_id: str,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Starts a SETUP experiment and creates its treatment campaign.

    From this point the treatment arm serves real ads to a real share of the
    base campaign's traffic. It does not add budget -- the split divides what
    the base campaign already spends -- but it does change what that money
    buys, so it needs confirmation.

    Args:
        customer_id: The Google Ads account id.
        experiment_id: The experiment to start.
        confirm_spend: Required. The treatment arm begins serving.

    Returns:
        The experiment's state, including the new treatment campaign id.
    """
    customer_id = _clean_id(customer_id)
    experiment_id = _clean_id(experiment_id)

    found = _experiments(customer_id, experiment_id)
    if not found:
        raise ValueError(f"No experiment {experiment_id}.")
    experiment = found[0]
    if not confirm_spend:
        raise ValueError(
            f"Scheduling '{experiment['name']}' sends part of the base "
            f"campaign's live traffic to the treatment arm from "
            f"{experiment['start_date']}. Total spend does not rise, but "
            f"what it buys changes. Show this to the user and call again "
            f"with confirm_spend=True."
        )

    service = utils.get_googleads_service("ExperimentService")
    service.schedule_experiment(resource_name=experiment["resource_name"])

    arms = _read_back(
        customer_id,
        f"""
        SELECT experiment_arm.name, experiment_arm.control,
               experiment_arm.campaigns, experiment_arm.in_design_campaigns
        FROM experiment_arm
        WHERE experiment_arm.experiment =
              '{experiment["resource_name"]}'
        """,
    )
    return {
        "experiment_id": experiment_id,
        "scheduled": True,
        "arms": [
            {
                "name": row["experiment_arm"].get("name"),
                "is_control": bool(row["experiment_arm"].get("control")),
                "campaign_ids": [
                    c.rsplit("/", 1)[-1]
                    for c in row["experiment_arm"].get("campaigns") or []
                ],
            }
            for row in arms
        ],
        "next_step": (
            "Edit the TREATMENT arm's campaign to make the change being "
            "tested. Leave the control alone -- changing it invalidates the "
            "comparison."
        ),
        "verified": _experiments(customer_id, experiment_id),
    }


@experiments_mcp.tool
def end_experiment(
    customer_id: str,
    experiment_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Ends a running experiment, discarding the treatment campaign.

    The treatment's traffic returns to the base campaign. Use graduate to
    keep the treatment instead.

    Args:
        customer_id: The Google Ads account id.
        experiment_id: The experiment to end.
        confirm: Required. The treatment campaign and its learning are lost.

    Returns:
        The experiment's state afterwards.
    """
    customer_id = _clean_id(customer_id)
    experiment_id = _clean_id(experiment_id)
    found = _experiments(customer_id, experiment_id)
    if not found:
        raise ValueError(f"No experiment {experiment_id}.")
    if not confirm:
        raise ValueError(
            f"Ending '{found[0]['name']}' discards the treatment campaign "
            f"and everything it learned. If the treatment won, graduate it "
            f"instead. Call again with confirm=True to end it."
        )

    service = utils.get_googleads_service("ExperimentService")
    service.end_experiment(experiment=found[0]["resource_name"])
    return {
        "experiment_id": experiment_id,
        "ended": True,
        "verified": _experiments(customer_id, experiment_id),
    }


@experiments_mcp.tool
def graduate_experiment(
    customer_id: str,
    experiment_id: str,
    campaign_budget_id: str,
    confirm_spend: bool = False,
) -> Dict[str, Any]:
    """Promotes the treatment arm into a permanent campaign of its own.

    Graduating gives the treatment campaign its own budget and takes it out
    of the experiment, so it becomes a normal campaign running alongside the
    base one. That is a real spend increase: the treatment no longer shares
    the base campaign's budget, it has its own.

    Args:
        customer_id: The Google Ads account id.
        experiment_id: The experiment whose treatment should survive.
        campaign_budget_id: The budget the graduated campaign will use.
            create_campaign_budget makes one.
        confirm_spend: Required. The graduated campaign spends its own
            budget on top of the base campaign's.

    Returns:
        The experiment's state afterwards.
    """
    customer_id = _clean_id(customer_id)
    experiment_id = _clean_id(experiment_id)
    campaign_budget_id = _clean_id(campaign_budget_id)

    found = _experiments(customer_id, experiment_id)
    if not found:
        raise ValueError(f"No experiment {experiment_id}.")

    budget_rows = _read_back(
        customer_id,
        f"""
        SELECT campaign_budget.id, campaign_budget.name,
               campaign_budget.amount_micros
        FROM campaign_budget
        WHERE campaign_budget.id = {campaign_budget_id}
        """,
    )
    if not budget_rows:
        raise ValueError(f"No budget {campaign_budget_id}.")
    daily = round(
        int(budget_rows[0]["campaign_budget"]["amount_micros"] or 0) / 1_000_000,
        2,
    )
    if not confirm_spend:
        raise ValueError(
            f"Graduating '{found[0]['name']}' turns the treatment into its "
            f"own campaign spending {daily}/day, about "
            f"{round(daily * 30.4, 2)}/month, ON TOP of the base campaign. "
            f"Show these numbers to the user and call again with "
            f"confirm_spend=True."
        )

    # Graduating maps each experiment campaign to the budget it will own.
    # The treatment campaign only exists once the experiment was scheduled,
    # so read it back rather than assuming an id.
    treatment_campaigns = []
    for row in _read_back(
        customer_id,
        f"""
        SELECT experiment_arm.control, experiment_arm.campaigns
        FROM experiment_arm
        WHERE experiment_arm.experiment = '{found[0]["resource_name"]}'
        """,
    ):
        arm = row["experiment_arm"]
        if not arm.get("control"):
            treatment_campaigns.extend(arm.get("campaigns") or [])
    if not treatment_campaigns:
        raise ValueError(
            "The treatment arm has no campaign yet, which means this "
            "experiment was never scheduled. There is nothing to graduate."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("ExperimentService")
    budget_path = utils.get_googleads_service(
        "CampaignBudgetService"
    ).campaign_budget_path(customer_id, campaign_budget_id)

    mappings = []
    for campaign_resource in treatment_campaigns:
        mapping = client.get_type("CampaignBudgetMapping")
        mapping.experiment_campaign = campaign_resource
        mapping.campaign_budget = budget_path
        mappings.append(mapping)

    service.graduate_experiment(
        experiment=found[0]["resource_name"],
        campaign_budget_mappings=mappings,
    )
    return {
        "experiment_id": experiment_id,
        "graduated": True,
        "daily_budget": daily,
        "graduated_campaigns": [
            c.rsplit("/", 1)[-1] for c in treatment_campaigns
        ],
        "verified": _experiments(customer_id, experiment_id),
    }


@experiments_mcp.tool
def remove_experiment(
    customer_id: str,
    experiment_id: str,
    confirm_remove: bool = False,
) -> Dict[str, Any]:
    """Deletes an experiment.

    Meant for the experiments that never ran: a SETUP experiment created by
    mistake, or one superseded before it started. Deleting a RUNNING
    experiment throws away its treatment campaign and every measurement it
    has collected -- end_experiment or graduate_experiment are the deliberate
    ways to finish one of those.

    Args:
        customer_id: The Google Ads account id.
        experiment_id: The experiment to delete.
        confirm_remove: Required. Not reversible.

    Returns:
        What was deleted.
    """
    customer_id = _clean_id(customer_id)
    experiment_id = _clean_id(experiment_id)

    found = _experiments(customer_id, experiment_id)
    if not found:
        raise ValueError(f"No experiment {experiment_id}.")
    entry = found[0]
    if not confirm_remove:
        extra = (
            " It is not in SETUP, so it may be running: deleting it discards "
            "the treatment campaign and its results. Consider "
            "end_experiment or graduate_experiment instead."
            if entry["status"] != "SETUP"
            else ""
        )
        raise ValueError(
            f"Deleting '{entry['name']}' (status {entry['status']}) is not "
            f"reversible.{extra} Call again with confirm_remove=True."
        )

    client = utils.get_googleads_client()
    service = utils.get_googleads_service("ExperimentService")
    operation = client.get_type("ExperimentOperation")
    operation.remove = entry["resource_name"]
    service.mutate_experiments(
        customer_id=customer_id, operations=[operation]
    )
    return {
        "experiment_id": experiment_id,
        "name": entry["name"],
        "previous_status": entry["status"],
        "removed": True,
    }
