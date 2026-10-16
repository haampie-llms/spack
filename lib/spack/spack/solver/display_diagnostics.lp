% Copyright Spack Project Developers. See COPYRIGHT file for details.
%
% SPDX-License-Identifier: (Apache-2.0 OR MIT)

%=============================================================================-
% Display facts needed for error causation
%
% Only added, as a second solve step, when the best model contains errors: showing these on
% every model would turn hundreds of thousands of atoms into Python objects per model.
%==============================================================================

% for error causation
#show condition_reason/2.

% For error messages to use later
#show pkg_fact/2.
#show condition_holds/2.
#show imposed_constraint/3.
#show imposed_constraint/4.
#show imposed_constraint/5.
#show imposed_constraint/6.
#show condition_requirement/3.
#show condition_requirement/4.
#show condition_requirement/5.
#show condition_requirement/6.
#show node_has_variant/3.
#show build/1.
#show external/1.
#show trigger_and_effect/3.
#show unification_set/2.
#show provider/2.
#show condition_nodes/2.
#show trigger_node/3.
#show imposed_nodes/2.
#show variant_single_value/2.
#show has_provider/1.

% debug
