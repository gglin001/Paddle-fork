// Copyright (c) 2023 CINN Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include "paddle/cinn/auto_schedule/post_schedule_rule/post_schedule_rule.h"

namespace cinn {
namespace auto_schedule {

/*
 * @brief Rewrite the cooperative_process annotation to actually bind the loop
 * on threadIdx. This rule is used for collaborative data handling of multiple
 * threads within the same block.
 */
class CooperativeProcess : public PostScheduleRule {
 public:
  CooperativeProcess() = default;

  bool Apply(ir::IRSchedule* schedule) final;
};

}  // namespace auto_schedule
}  // namespace cinn
