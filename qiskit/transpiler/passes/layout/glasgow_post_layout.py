# This code is part of Qiskit.
#
# (C) Copyright IBM 2026.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at https://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Experimental GlasgowPostLayout pass."""

from enum import Enum

from qiskit.transpiler.basepasses import AnalysisPass
from qiskit.transpiler.exceptions import TranspilerError
from qiskit.transpiler.layout import Layout

from ._glasgow_utils import (
    GlasgowMultiQEncountered,
    complete_mapping,
    interaction_graph,
    run_glasgow,
    score_mapping,
    target_graph,
    write_stats,
)


class GlasgowPostLayoutStopReason(Enum):
    """Stop reasons for :class:`GlasgowPostLayout`."""

    SOLUTION_FOUND = "solution found"
    NO_BETTER_SOLUTION_FOUND = "no better solution found"
    NO_SOLUTION_FOUND = "nonexistent solution"
    ABORTED = "search aborted"
    MORE_THAN_2Q = ">2q gates in basis"


class GlasgowPostLayout(AnalysisPass):
    """Search for a lower-error isomorphic placement after routing using Glasgow."""

    def __init__(
        self,
        target=None,
        seed=None,
        call_limit=None,
        time_limit=None,
        strict_direction=True,
        max_trials=0,
    ):
        super().__init__()
        self.target = target
        self.seed = seed
        self.call_limit = call_limit
        self.time_limit = time_limit
        self.strict_direction = strict_direction
        self.max_trials = max_trials

    def run(self, dag):
        if self.target is None:
            raise TranspilerError("A target must be specified")

        coupling_map = self.target.build_coupling_map()
        if coupling_map is None:
            self.property_set["GlasgowPostLayout_stop_reason"] = (
                GlasgowPostLayoutStopReason.NO_BETTER_SOLUTION_FOUND
            )
            return

        try:
            pattern, active = interaction_graph(dag, directed=self.strict_direction)
        except GlasgowMultiQEncountered:
            self.property_set["GlasgowPostLayout_stop_reason"] = (
                GlasgowPostLayoutStopReason.MORE_THAN_2Q
            )
            return

        if not active or dag.is_empty():
            self.property_set["GlasgowPostLayout_stop_reason"] = (
                GlasgowPostLayoutStopReason.NO_BETTER_SOLUTION_FOUND
            )
            return

        num_physical = self.target.num_qubits
        hardware = target_graph(coupling_map, num_physical, directed=self.strict_direction)
        result = run_glasgow(
            pattern,
            hardware,
            max_trials=self.max_trials,
            time_limit=self.time_limit,
        )
        write_stats(self.property_set, "GlasgowPostLayout", result)

        identity = {index: index for index in range(dag.num_qubits())}
        baseline = score_mapping(
            dag, self.target, identity, strict_direction=self.strict_direction
        )
        best = None
        best_score = baseline

        for partial in result.mappings:
            candidate = complete_mapping(
                partial, dag.num_qubits(), num_physical, prefer_identity=True
            )
            if candidate is None:
                continue
            score = score_mapping(
                dag, self.target, candidate, strict_direction=self.strict_direction
            )
            if score + 1e-15 < best_score:
                best = candidate
                best_score = score

        self.property_set["GlasgowPostLayout_baseline_score"] = baseline
        self.property_set["GlasgowPostLayout_score"] = best_score

        if best is None:
            if result.status == "false" and not result.mappings:
                reason = GlasgowPostLayoutStopReason.NO_SOLUTION_FOUND
            elif result.status == "aborted" and not result.mappings:
                reason = GlasgowPostLayoutStopReason.ABORTED
            else:
                reason = GlasgowPostLayoutStopReason.NO_BETTER_SOLUTION_FOUND
            self.property_set["GlasgowPostLayout_stop_reason"] = reason
            return

        layout = Layout({dag.qubits[virtual]: physical for virtual, physical in best.items()})
        for register in dag.qregs.values():
            layout.add_register(register)
        self.property_set["post_layout"] = layout
        self.property_set["GlasgowPostLayout_stop_reason"] = (
            GlasgowPostLayoutStopReason.SOLUTION_FOUND
        )
