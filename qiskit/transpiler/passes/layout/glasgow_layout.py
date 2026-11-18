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

"""Experimental GlasgowLayout pass."""

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


class GlasgowLayoutStopReason(Enum):
    """Stop reasons for :class:`GlasgowLayout`."""

    SOLUTION_FOUND = "solution found"
    NO_SOLUTION_FOUND = "nonexistent solution"
    ABORTED = "search aborted"
    MORE_THAN_2Q = ">2q gates in basis"


class GlasgowLayout(AnalysisPass):
    """Choose a zero-SWAP initial layout using the Glasgow Subgraph Solver.

    The external solver path is read from ``QISKIT_GLASGOW_EXECUTABLE`` or from
    ``glasgow_subgraph_solver`` on ``PATH``.  The pass is intentionally API-compatible with the
    arguments used by the default VF2 layout stage; ``call_limit`` and ``seed`` are accepted for
    drop-in construction but do not have direct Glasgow equivalents.
    """

    def __init__(
        self,
        coupling_map=None,
        strict_direction=False,
        seed=None,
        call_limit=None,
        time_limit=None,
        max_trials=None,
        target=None,
    ):
        super().__init__()
        self.target = target
        self.coupling_map = coupling_map
        self.strict_direction = strict_direction
        self.seed = seed
        self.call_limit = call_limit
        self.time_limit = time_limit
        self.max_trials = max_trials

    def run(self, dag):
        if self.target is None and self.coupling_map is None:
            raise TranspilerError("coupling_map or target must be specified.")

        coupling_map = self.target.build_coupling_map() if self.target is not None else None
        if coupling_map is None:
            coupling_map = self.coupling_map
        if coupling_map is None:
            raise TranspilerError("GlasgowLayout requires a constrained coupling map")

        num_physical = self.target.num_qubits if self.target is not None else coupling_map.size()
        if dag.num_qubits() > num_physical:
            self.property_set["GlasgowLayout_stop_reason"] = (
                GlasgowLayoutStopReason.NO_SOLUTION_FOUND
            )
            return

        try:
            pattern, active = interaction_graph(dag, directed=self.strict_direction)
        except GlasgowMultiQEncountered:
            self.property_set["GlasgowLayout_stop_reason"] = GlasgowLayoutStopReason.MORE_THAN_2Q
            return

        if not active:
            mapping = complete_mapping(
                {}, dag.num_qubits(), num_physical, prefer_identity=False
            )
            result = None
        else:
            hardware = target_graph(coupling_map, num_physical, directed=self.strict_direction)
            result = run_glasgow(
                pattern,
                hardware,
                max_trials=self.max_trials,
                time_limit=self.time_limit,
            )
            write_stats(self.property_set, "GlasgowLayout", result)

            best = None
            best_score = float("inf")
            for partial in result.mappings:
                candidate = complete_mapping(
                    partial, dag.num_qubits(), num_physical, prefer_identity=False
                )
                if candidate is None:
                    continue
                score = score_mapping(
                    dag, self.target, candidate, strict_direction=self.strict_direction
                )
                if score < best_score:
                    best = candidate
                    best_score = score
            mapping = best
            if best is not None:
                self.property_set["GlasgowLayout_score"] = best_score

        if mapping is None:
            reason = (
                GlasgowLayoutStopReason.NO_SOLUTION_FOUND
                if result is not None and result.status == "false"
                else GlasgowLayoutStopReason.ABORTED
            )
            self.property_set["GlasgowLayout_stop_reason"] = reason
            return

        layout = Layout({dag.qubits[virtual]: physical for virtual, physical in mapping.items()})
        for register in dag.qregs.values():
            layout.add_register(register)
        self.property_set["layout"] = layout
        self.property_set["GlasgowLayout_stop_reason"] = GlasgowLayoutStopReason.SOLUTION_FOUND
