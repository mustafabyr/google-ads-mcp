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

"""The safety layer that sits between every tool and the Google Ads API.

Nothing in here knows about campaigns or keywords. It knows four things:

  * which CLASS a tool belongs to (``classes``): READ, PROTECTIVE, SPEND or
    DESTRUCTIVE -- the single registry every other decision reads;
  * what an account is allowed to do (``limits``): ceilings per account,
    without which nothing that can spend is written at all;
  * how a write happens (``session``, ``plans``): a SPEND or DESTRUCTIVE
    tool produces a plan first and the plan is applied as a second step,
    after checking that the account still looks the way the plan assumed;
  * and that every write leaves a line behind (``audit_log``).

``install`` wires all of that onto the tool servers at mount time, so a
tool module never has to remember to opt in.
"""
