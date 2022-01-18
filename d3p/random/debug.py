# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: © 2019- d3p Developers and their Assignees

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings
from d3p.random._internal_jax_rng_wrapper import KeyRandomnessInBytes, PRNGState, PRNGKey,\
    convert_to_jax_rng_key, split, fold_in, random_bits, uniform, normal

warnings.warn(
    "d3p is currently using a non-cryptographic random number generator!\n"
    "This is intended for debugging only! Please make sure to switch to using d3p.random to"
    " ensure privacy guarantees hold!",
    stacklevel=2
)

__all__ = [
    KeyRandomnessInBytes,
    PRNGState,
    PRNGKey,
    convert_to_jax_rng_key,
    split,
    fold_in,
    random_bits,
    uniform,
    normal
]
