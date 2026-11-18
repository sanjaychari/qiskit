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

"""Helpers shared by the experimental Glasgow layout passes."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from time import perf_counter

from qiskit.transpiler.exceptions import TranspilerError


class GlasgowMultiQEncountered(Exception):
    """Raised when an interaction graph contains an operation on more than two qubits."""


@dataclass
class GlasgowResult:
    """Result returned by the external Glasgow Subgraph Solver."""

    status: str | None
    mappings: list[dict[int, int]]
    nodes: int | None
    propagations: int | None
    solver_runtime_ms: int | None
    wall_runtime_ms: float


def _glasgow_executable() -> str:
    override = os.getenv("QISKIT_GLASGOW_EXECUTABLE")
    if override:
        executable = os.path.expanduser(override)
        if os.path.isfile(executable) and os.access(executable, os.X_OK):
            return executable
        raise TranspilerError(
            "QISKIT_GLASGOW_EXECUTABLE does not point to an executable file: "
            f"{executable!r}"
        )

    executable = shutil.which("glasgow_subgraph_solver")
    if executable is None:
        raise TranspilerError(
            "Glasgow Subgraph Solver was requested, but 'glasgow_subgraph_solver' was not "
            "found on PATH. Build Glasgow and either add its build directory to PATH or set "
            "QISKIT_GLASGOW_EXECUTABLE to the solver executable."
        )
    return executable


def _solution_limit(max_trials: int | None) -> int:
    """Return the bounded number of Glasgow embeddings to score."""
    env_limit = int(os.getenv("QISKIT_GLASGOW_MAX_SOLUTIONS", "256"))
    if env_limit <= 0:
        raise TranspilerError("QISKIT_GLASGOW_MAX_SOLUTIONS must be a positive integer")
    if max_trials in (None, 0):
        return env_limit
    return max(1, min(int(max_trials), env_limit))


def _timeout(time_limit: float | None) -> int | None:
    if time_limit is None:
        value = os.getenv("QISKIT_GLASGOW_TIMEOUT")
        if value is None:
            return None
        time_limit = float(value)
    if time_limit <= 0:
        return None
    return max(1, math.ceil(time_limit))


def _graph_json(vertices: list[str], edges: set[tuple[str, str]], directed: bool) -> dict:
    return {
        "format": "gss-graph",
        "version": 1,
        "directed": directed,
        "vertices": [{"name": vertex} for vertex in vertices],
        "edges": [list(edge) for edge in sorted(edges)],
    }


def interaction_graph(dag, directed: bool) -> tuple[dict, set[int]]:
    """Build the structural 2q interaction graph used for Glasgow matching."""
    active: set[int] = set()
    edges: set[tuple[str, str]] = set()

    for node in dag.op_nodes():
        if getattr(node.op, "name", None) == "barrier":
            continue
        qargs = node.qargs
        if len(qargs) > 2:
            raise GlasgowMultiQEncountered
        if len(qargs) != 2:
            continue
        left = dag.find_bit(qargs[0]).index
        right = dag.find_bit(qargs[1]).index
        active.update((left, right))
        if directed:
            edges.add((f"q{left}", f"q{right}"))
        else:
            a, b = sorted((left, right))
            edges.add((f"q{a}", f"q{b}"))

    vertices = [f"q{index}" for index in sorted(active)]
    return _graph_json(vertices, edges, directed), active


def target_graph(coupling_map, num_qubits: int, directed: bool) -> dict:
    """Build a Glasgow graph from a Qiskit coupling map."""
    edges: set[tuple[str, str]] = set()
    for left, right in coupling_map.get_edges():
        left = int(left)
        right = int(right)
        if directed:
            edges.add((f"p{left}", f"p{right}"))
        else:
            a, b = sorted((left, right))
            edges.add((f"p{a}", f"p{b}"))
    vertices = [f"p{index}" for index in range(num_qubits)]
    return _graph_json(vertices, edges, directed)


def run_glasgow(
    pattern: dict,
    target: dict,
    *,
    max_trials: int | None,
    time_limit: float | None,
) -> GlasgowResult:
    """Run Glasgow and return all mappings printed before its solution bound or timeout."""
    executable = _glasgow_executable()
    solution_limit = _solution_limit(max_trials)
    timeout = _timeout(time_limit)

    with tempfile.TemporaryDirectory(prefix="qiskit-glasgow-") as temp_dir:
        pattern_path = os.path.join(temp_dir, "pattern.json")
        target_path = os.path.join(temp_dir, "target.json")
        with open(pattern_path, "w", encoding="utf8") as pattern_file:
            json.dump(pattern, pattern_file)
        with open(target_path, "w", encoding="utf8") as target_file:
            json.dump(target, target_file)

        command = [
            executable,
            "--format",
            "json",
            "--print-all-solutions",
            "--solution-limit",
            str(solution_limit),
        ]
        if timeout is not None:
            command.extend(("--timeout", str(timeout)))
        command.extend((pattern_path, target_path))

        start = perf_counter()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=None if timeout is None else timeout + 10,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            returncode = None
        wall_runtime_ms = (perf_counter() - start) * 1000.0

    if returncode not in (None, 0):
        raise TranspilerError(
            "Glasgow Subgraph Solver failed with exit code "
            f"{returncode}: {stderr.strip() or stdout.strip()}"
        )

    status = None
    nodes = None
    propagations = None
    solver_runtime_ms = None
    mappings: list[dict[int, int]] = []
    mapping_re = re.compile(r"\((q\d+) -> (p\d+)\)")

    for line in stdout.splitlines():
        if line.startswith("status = "):
            status = line.split("=", 1)[1].strip()
        elif line.startswith("nodes = "):
            nodes = int(line.split("=", 1)[1].strip())
        elif line.startswith("propagations = "):
            propagations = int(line.split("=", 1)[1].strip())
        elif line.startswith("runtime = "):
            solver_runtime_ms = int(line.split("=", 1)[1].strip())
        elif line.startswith("mapping = "):
            mapping: dict[int, int] = {}
            for virtual, physical in mapping_re.findall(line):
                mapping[int(virtual[1:])] = int(physical[1:])
            if mapping:
                mappings.append(mapping)

    if returncode is None:
        status = "aborted"

    return GlasgowResult(
        status=status,
        mappings=mappings,
        nodes=nodes,
        propagations=propagations,
        solver_runtime_ms=solver_runtime_ms,
        wall_runtime_ms=wall_runtime_ms,
    )


def complete_mapping(
    partial: dict[int, int],
    num_virtual: int,
    num_physical: int,
    *,
    prefer_identity: bool,
) -> dict[int, int] | None:
    """Extend a structural Glasgow mapping to all virtual qubits."""
    if num_virtual > num_physical:
        return None

    mapping = dict(partial)
    used = set(mapping.values())
    free = [physical for physical in range(num_physical) if physical not in used]

    for virtual in range(num_virtual):
        if virtual in mapping:
            continue
        if prefer_identity and virtual < num_physical and virtual not in used:
            physical = virtual
            free.remove(physical)
        else:
            if not free:
                return None
            physical = free.pop(0)
        mapping[virtual] = physical
        used.add(physical)
    return mapping


def _neg_log_fidelity(error: float | None) -> float:
    if error is None or math.isnan(error) or error <= 0.0:
        return 0.0
    if error >= 1.0:
        return float("inf")
    return -math.log1p(-error)


def _average_error(target, qargs: tuple[int, ...]) -> float | None:
    try:
        names = target.operation_names_for_qargs(qargs)
    except KeyError:
        return None
    if not names:
        return None

    total = 0.0
    for name in sorted(names):
        props = target[name].get(qargs, None)
        total += 0.0 if props is None or props.error is None else props.error
    return total / len(names)


def score_mapping(dag, target, mapping: dict[int, int], *, strict_direction: bool) -> float:
    """Approximate Qiskit's VF2 error objective for a complete mapping."""
    if target is None:
        return 0.0

    score = 0.0
    for node in dag.op_nodes():
        if getattr(node.op, "name", None) == "barrier":
            continue
        qargs = node.qargs
        if len(qargs) > 2:
            return float("inf")
        if not qargs:
            continue

        physical = tuple(mapping[dag.find_bit(qubit).index] for qubit in qargs)
        if strict_direction:
            if not target.instruction_supported(operation_name=node.op.name, qargs=physical):
                return float("inf")
            props = target[node.op.name].get(physical, None)
            error = 0.0 if props is None or props.error is None else props.error
        else:
            error = _average_error(target, physical)
            if error is None and len(physical) == 2:
                error = _average_error(target, tuple(reversed(physical)))
            if error is None:
                return float("inf")
        score += _neg_log_fidelity(error)
    return score


def write_stats(property_set, prefix: str, result: GlasgowResult) -> None:
    property_set[f"{prefix}_status"] = result.status
    property_set[f"{prefix}_nodes"] = result.nodes
    property_set[f"{prefix}_propagations"] = result.propagations
    property_set[f"{prefix}_solver_runtime_ms"] = result.solver_runtime_ms
    property_set[f"{prefix}_wall_runtime_ms"] = result.wall_runtime_ms
    property_set[f"{prefix}_mappings_examined"] = len(result.mappings)
