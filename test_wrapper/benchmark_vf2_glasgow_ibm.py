#!/usr/bin/env python3
"""Benchmark Qiskit's VF2 layout search against Glasgow on an IBM BackendV2 Target.

This harness measures four pipelines at optimization_level=3:

    vf2_vf2
        VF2Layout + VF2PostLayout (stock Qiskit)

    vf2_glasgow
        VF2Layout + GlasgowPostLayout

    glasgow_vf2
        GlasgowLayout + VF2PostLayout

    glasgow_glasgow
        GlasgowLayout + GlasgowPostLayout

The same transpiler seeds are used for all four configurations.  Results are
aggregated by circuit and configuration across transpiler seeds; the raw
per-seed results are retained.

The script uses a single Target object fetched from the selected IBM backend at
startup.  It does not submit hardware jobs.  It benchmarks compilation against
the live backend topology, instruction set, and calibration/error data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import qiskit
from qiskit import QuantumCircuit
from qiskit.circuit.library import GraphStateGate, QFTGate, quantum_volume, real_amplitudes
from qiskit.transpiler import AnalysisPass, PassManager, generate_preset_pass_manager
from qiskit.transpiler.passes.layout.vf2_layout import VF2LayoutStopReason
from qiskit_ibm_runtime import QiskitRuntimeService

from glasgow_module import GlasgowLayoutStopReason


# ---------------------------------------------------------------------------
# Experiment definition
# ---------------------------------------------------------------------------

OPTIMIZATION_LEVEL = 3
CONFIGS = (
    "vf2_vf2",
    "vf2_glasgow",
    "glasgow_vf2",
    "glasgow_glasgow",
)

RAW_FIELDS = [
    "timestamp_utc",
    "backend",
    "circuit_id",
    "family",
    "num_qubits",
    "circuit_seed",
    "transpiler_seed",
    "configuration",
    "layout_solver",
    "postlayout_solver",
    "status",
    "error",
    "pm_build_ms",
    "run_wall_ms",
    "callback_pass_ms",
    "layout_ms",
    "sabre_layout_ms",
    "sabre_swap_ms",
    "routing_post_attempted",
    "routing_post_ms",
    "routing_post_success",
    "routing_post_status",
    "strict_post_attempted",
    "strict_post_ms",
    "strict_post_success",
    "strict_post_status",
    "layout_success",
    "layout_status",
    "depth",
    "size",
    "twoq_count",
    "nll_score",
    "estimated_fidelity",
    "estimated_error",
    "missing_error_count",
    "final_mapping_json",
    "pass_trace_json",
]


@dataclass(frozen=True)
class CircuitCase:
    circuit_id: str
    family: str
    circuit: QuantumCircuit
    circuit_seed: int | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Mixed-pipeline compatibility bridge
# ---------------------------------------------------------------------------

class LayoutStopReasonBridge(AnalysisPass):
    """Translate the initial-layout stop reason for a mixed Layout/PostLayout run.

    Qiskit's stock routing-stage VF2PostLayout condition checks
    ``VF2Layout_stop_reason``.  The Glasgow routing-stage condition checks
    ``GlasgowLayout_stop_reason``.  When the layout stage and PostLayout stage
    come from different solvers, this bridge makes the downstream condition see
    the equivalent upstream result.
    """

    def __init__(self, direction: str):
        super().__init__()
        if direction not in {"vf2_to_glasgow", "glasgow_to_vf2"}:
            raise ValueError(direction)
        self.direction = direction

    def run(self, dag):
        if self.direction == "vf2_to_glasgow":
            reason = self.property_set["VF2Layout_stop_reason"]
            if reason is None:
                translated = None
            elif reason is VF2LayoutStopReason.SOLUTION_FOUND:
                translated = GlasgowLayoutStopReason.SOLUTION_FOUND
            else:
                translated = GlasgowLayoutStopReason.NO_SOLUTION_FOUND
            self.property_set["GlasgowLayout_stop_reason"] = translated

        else:
            reason = self.property_set["GlasgowLayout_stop_reason"]
            if reason is None:
                translated = None
            elif reason is GlasgowLayoutStopReason.SOLUTION_FOUND:
                translated = VF2LayoutStopReason.SOLUTION_FOUND
            else:
                translated = VF2LayoutStopReason.NO_SOLUTION_FOUND
            self.property_set["VF2Layout_stop_reason"] = translated

        return None


def build_pipeline(target, transpiler_seed: int, configuration: str):
    """Build one of the four controlled pipelines.

    The stock and all-Glasgow preset managers are built with the same Target,
    optimization level, and transpiler seed.  We then replace only the layout,
    routing, and optimization stages needed for the selected factorial cell.
    """

    if configuration not in CONFIGS:
        raise ValueError(f"Unknown configuration: {configuration}")

    stock = generate_preset_pass_manager(
        optimization_level=OPTIMIZATION_LEVEL,
        target=target,
        seed_transpiler=transpiler_seed,
        # Intentionally leave layout/routing/optimization methods as None.
    )

    glasgow = generate_preset_pass_manager(
        optimization_level=OPTIMIZATION_LEVEL,
        target=target,
        seed_transpiler=transpiler_seed,
        layout_method="glasgow",
        routing_method="glasgow",
        optimization_method="glasgow",
    )

    # Start with stock Qiskit for every non-solver stage.
    pm = stock

    use_glasgow_layout = configuration.startswith("glasgow_")
    use_glasgow_post = configuration.endswith("_glasgow")

    if use_glasgow_layout:
        pm.layout = glasgow.layout

    if use_glasgow_post:
        pm.routing = glasgow.routing
        pm.optimization = glasgow.optimization

    # Mixed pipelines need the equivalent layout result under the key expected
    # by their routing-stage PostLayout condition.
    if configuration == "vf2_glasgow":
        pm.post_layout = PassManager([LayoutStopReasonBridge("vf2_to_glasgow")])
    elif configuration == "glasgow_vf2":
        pm.post_layout = PassManager([LayoutStopReasonBridge("glasgow_to_vf2")])

    return pm


# ---------------------------------------------------------------------------
# Circuit generation
# ---------------------------------------------------------------------------

def _adjacency_matrix(num_nodes: int, edges: Iterable[tuple[int, int]]) -> np.ndarray:
    matrix = np.zeros((num_nodes, num_nodes), dtype=int)
    for left, right in edges:
        if left == right:
            continue
        matrix[left, right] = 1
        matrix[right, left] = 1
    return matrix


def _decompose_to_two_qubit(circuit: QuantumCircuit, max_rounds: int = 12) -> QuantumCircuit:
    out = circuit
    for _ in range(max_rounds):
        has_large_op = any(
            inst.operation.num_qubits > 2
            and not getattr(inst.operation, "_directive", False)
            for inst in out.data
        )
        if not has_large_op:
            return out
        out = out.decompose()
    remaining = [
        (inst.operation.name, inst.operation.num_qubits)
        for inst in out.data
        if inst.operation.num_qubits > 2
        and not getattr(inst.operation, "_directive", False)
    ]
    raise RuntimeError(f"Could not decompose circuit to <=2q operations: {remaining[:10]}")


def make_graph_state(adjacency: np.ndarray, name: str) -> QuantumCircuit:
    n = adjacency.shape[0]
    qc = QuantumCircuit(n, name=name)
    qc.append(GraphStateGate(adjacency), range(n))
    qc = _decompose_to_two_qubit(qc)
    qc.name = name
    qc.measure_all()
    return qc


def path_graph(n: int) -> np.ndarray:
    return _adjacency_matrix(n, ((i, i + 1) for i in range(n - 1)))


def cycle_graph(n: int) -> np.ndarray:
    edges = [(i, i + 1) for i in range(n - 1)]
    edges.append((n - 1, 0))
    return _adjacency_matrix(n, edges)


def ladder_graph(n: int) -> np.ndarray:
    if n % 2:
        raise ValueError("ladder graph requires an even number of vertices")
    width = n // 2
    edges: list[tuple[int, int]] = []
    for i in range(width - 1):
        edges.append((i, i + 1))
        edges.append((width + i, width + i + 1))
    for i in range(width):
        edges.append((i, width + i))
    return _adjacency_matrix(n, edges)


def _largest_component_bfs_order(target) -> list[int]:
    coupling = target.build_coupling_map()
    if coupling is None:
        raise RuntimeError("Target has no finite coupling map")

    num_qubits = target.num_qubits
    neighbors = {q: set() for q in range(num_qubits)}
    for left, right in coupling.get_edges():
        if left == right:
            continue
        neighbors[int(left)].add(int(right))
        neighbors[int(right)].add(int(left))

    unseen = set(range(num_qubits))
    components: list[list[int]] = []
    while unseen:
        start = min(unseen)
        queue = deque([start])
        unseen.remove(start)
        component: list[int] = []
        while queue:
            node = queue.popleft()
            component.append(node)
            for nxt in sorted(neighbors[node]):
                if nxt in unseen:
                    unseen.remove(nxt)
                    queue.append(nxt)
        components.append(component)

    component = max(components, key=lambda comp: (len(comp), -min(comp)))
    component_set = set(component)

    # Start from a high-degree qubit to grow a compact connected subgraph.
    root = min(
        component,
        key=lambda q: (-len(neighbors[q] & component_set), q),
    )

    order: list[int] = []
    seen = {root}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        order.append(node)
        candidates = sorted(
            (q for q in neighbors[node] if q in component_set and q not in seen),
            key=lambda q: (-len(neighbors[q] & component_set), q),
        )
        for nxt in candidates:
            seen.add(nxt)
            queue.append(nxt)

    if len(order) != len(component):
        raise RuntimeError("Internal error while traversing backend coupling graph")
    return order


def backend_subgraph(target, n: int) -> tuple[np.ndarray, list[int]]:
    order = _largest_component_bfs_order(target)
    if n > len(order):
        raise ValueError(
            f"Requested backend subgraph size {n}, but largest connected component has {len(order)}"
        )

    selected = order[:n]
    selected_set = set(selected)
    logical_for_physical = {physical: logical for logical, physical in enumerate(selected)}

    coupling = target.build_coupling_map()
    undirected_edges: set[tuple[int, int]] = set()
    for left, right in coupling.get_edges():
        left = int(left)
        right = int(right)
        if left in selected_set and right in selected_set and left != right:
            a = logical_for_physical[left]
            b = logical_for_physical[right]
            if a > b:
                a, b = b, a
            undirected_edges.add((a, b))

    return _adjacency_matrix(n, sorted(undirected_edges)), selected


def make_suite(target, suite_name: str) -> list[CircuitCase]:
    cases: list[CircuitCase] = []
    max_qubits = target.num_qubits

    if suite_name == "smoke":
        path_sizes = [8, 16]
        ra_sizes = [8, 16]
        cycle_sizes = [12]
        ladder_sizes = [12]
        backend_sizes = [min(30, max_qubits)]
        qv_sizes = [8]
        qv_seeds = [11]
        qft_sizes = [8]
    elif suite_name == "full":
        path_sizes = [8, 16, 32, 64]
        ra_sizes = [8, 16, 32, 64]
        cycle_sizes = [12, 24, 36]
        ladder_sizes = [12, 24, 36]
        backend_sizes = [30, 60, 90, 108]
        qv_sizes = [8, 16, 24]
        qv_seeds = [11, 22, 33]
        qft_sizes = [8, 12, 16]
    else:
        raise ValueError(suite_name)

    def valid_sizes(sizes):
        return [n for n in sizes if n <= max_qubits]

    for n in valid_sizes(path_sizes):
        cid = f"graph_path_n{n}"
        cases.append(CircuitCase(cid, "graph_path", make_graph_state(path_graph(n), cid)))

    for n in valid_sizes(ra_sizes):
        cid = f"real_amplitudes_linear_n{n}_r2"
        qc = real_amplitudes(n, entanglement="linear", reps=2)
        qc.name = cid
        qc.measure_all()
        cases.append(CircuitCase(cid, "real_amplitudes_linear", qc))

    for n in valid_sizes(cycle_sizes):
        cid = f"graph_cycle_n{n}"
        cases.append(CircuitCase(cid, "graph_cycle", make_graph_state(cycle_graph(n), cid)))

    for n in valid_sizes(ladder_sizes):
        cid = f"graph_ladder_n{n}"
        cases.append(CircuitCase(cid, "graph_ladder", make_graph_state(ladder_graph(n), cid)))

    largest_component = len(_largest_component_bfs_order(target))
    for n in backend_sizes:
        if n > largest_component:
            continue
        adjacency, known_embedding = backend_subgraph(target, n)
        cid = f"graph_backend_subgraph_n{n}"
        cases.append(
            CircuitCase(
                cid,
                "graph_backend_subgraph",
                make_graph_state(adjacency, cid),
                metadata={"known_embedding": known_embedding},
            )
        )

    for n in valid_sizes(qv_sizes):
        for circuit_seed in qv_seeds:
            cid = f"quantum_volume_n{n}_d{n}_cseed{circuit_seed}"
            qc = quantum_volume(num_qubits=n, depth=n, seed=circuit_seed)
            qc.name = cid
            qc.measure_all()
            cases.append(CircuitCase(cid, "quantum_volume", qc, circuit_seed=circuit_seed))

    for n in valid_sizes(qft_sizes):
        cid = f"qft_n{n}"
        qc = QuantumCircuit(n, name=cid)
        qc.append(QFTGate(n), range(n))
        qc = _decompose_to_two_qubit(qc)
        qc.name = cid
        qc.measure_all()
        cases.append(CircuitCase(cid, "qft", qc))

    return cases


# ---------------------------------------------------------------------------
# Target and final-circuit scoring
# ---------------------------------------------------------------------------

def neg_log_fidelity(error: float | None) -> float:
    if error is None:
        return 0.0
    error = float(error)
    if math.isnan(error) or error <= 0.0:
        return 0.0
    if error >= 1.0:
        return math.inf
    return -math.log1p(-error)


def aggregate_error_metrics(circuit: QuantumCircuit, target) -> dict[str, Any]:
    terms: list[float] = []
    missing = 0

    for instruction in circuit.data:
        op = instruction.operation
        if getattr(op, "_directive", False):
            continue
        qargs = tuple(circuit.find_bit(q).index for q in instruction.qubits)
        if not qargs:
            continue

        props_map = target.get(op.name)
        if props_map is None:
            raise RuntimeError(f"Final circuit operation {op.name!r} is absent from Target")

        if qargs in props_map:
            props = props_map[qargs]
        elif None in props_map:
            props = props_map[None]
        else:
            raise RuntimeError(f"Final operation {op.name}{qargs} is unsupported by Target")

        error = None if props is None else props.error
        if error is None or math.isnan(float(error)):
            missing += 1
            error = 0.0
        terms.append(neg_log_fidelity(error))

    nll = math.fsum(terms)
    fidelity = 0.0 if math.isinf(nll) else math.exp(-nll)
    return {
        "nll_score": nll,
        "estimated_fidelity": fidelity,
        "estimated_error": 1.0 - fidelity,
        "missing_error_count": missing,
    }


def snapshot_target(target) -> dict[str, Any]:
    operations: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(target.operation_names):
        entries = []
        props_map = target[name]
        for qargs, props in props_map.items():
            entries.append(
                {
                    "qargs": None if qargs is None else list(qargs),
                    "error": None if props is None else props.error,
                    "duration": None if props is None else props.duration,
                }
            )
        entries.sort(key=lambda x: (-1,) if x["qargs"] is None else tuple(x["qargs"]))
        operations[name] = entries

    coupling = target.build_coupling_map()
    edges = [] if coupling is None else [list(map(int, edge)) for edge in coupling.get_edges()]
    return {
        "num_qubits": target.num_qubits,
        "dt": target.dt,
        "operation_names": sorted(target.operation_names),
        "coupling_edges": edges,
        "operations": operations,
    }


# ---------------------------------------------------------------------------
# Callback collection and pipeline validation
# ---------------------------------------------------------------------------

def _enum_name(value) -> str | None:
    return None if value is None else getattr(value, "name", str(value))


def make_callback():
    state: dict[str, Any] = {
        "trace": [],
        "layout_events": [],
        "post_events": [],
    }

    def callback(**cb):
        pass_obj = cb["pass_"]
        name = pass_obj.__class__.__name__
        elapsed_ms = float(cb["time"]) * 1000.0
        ps = cb["property_set"]

        event: dict[str, Any] = {
            "name": name,
            "ms": elapsed_ms,
        }

        if name == "VF2Layout":
            status = _enum_name(ps["VF2Layout_stop_reason"])
            event["status"] = status
            state["layout_events"].append(event.copy())
        elif name == "GlasgowLayout":
            status = _enum_name(ps["GlasgowLayout_stop_reason"])
            event["status"] = status
            state["layout_events"].append(event.copy())

        if name in {"VF2PostLayout", "GlasgowPostLayout"}:
            strict = bool(getattr(pass_obj, "strict_direction", False))
            event["strict"] = strict

            if name == "VF2PostLayout":
                status = _enum_name(ps["VF2PostLayout_stop_reason"])
                success = status == "SOLUTION_FOUND"
            else:
                found = ps["GlasgowPostLayout_found"]
                success = bool(found)
                status = "SOLUTION_FOUND" if success else "NO_BETTER_OR_NO_SOLUTION"

            event["status"] = status
            event["success"] = success
            state["post_events"].append(event.copy())

        state["trace"].append(event)

    return callback, state


def validate_trace(configuration: str, state: dict[str, Any]) -> None:
    expected_layout = "GlasgowLayout" if configuration.startswith("glasgow_") else "VF2Layout"
    expected_post = "GlasgowPostLayout" if configuration.endswith("_glasgow") else "VF2PostLayout"

    layout_names = [e["name"] for e in state["layout_events"]]
    if layout_names != [expected_layout]:
        raise RuntimeError(
            f"{configuration}: expected exactly [{expected_layout}] layout pass, got {layout_names}"
        )

    post_events = state["post_events"]
    unexpected = [e["name"] for e in post_events if e["name"] != expected_post]
    if unexpected:
        raise RuntimeError(
            f"{configuration}: unexpected PostLayout solver(s): {unexpected}; trace={state['trace']}"
        )

    strict_events = [e for e in post_events if e.get("strict")]
    routing_events = [e for e in post_events if not e.get("strict")]
    if len(strict_events) != 1:
        raise RuntimeError(
            f"{configuration}: expected exactly one strict PostLayout, got {len(strict_events)}"
        )
    if len(routing_events) > 1:
        raise RuntimeError(
            f"{configuration}: expected at most one routing PostLayout, got {len(routing_events)}"
        )

    saw_sabre_layout = any(e["name"] == "SabreLayout" for e in state["trace"])
    expected_routing_count = 1 if saw_sabre_layout else 0
    if len(routing_events) != expected_routing_count:
        raise RuntimeError(
            f"{configuration}: SabreLayout present={saw_sabre_layout}, but routing PostLayout "
            f"count={len(routing_events)}; expected {expected_routing_count}"
        )

    # A successful PostLayout must be immediately followed by ApplyLayout among
    # actually executed passes.  This catches the plugin bug where a mapping was
    # found but Qiskit's VF2-specific apply condition prevented it being applied.
    trace = state["trace"]
    for index, event in enumerate(trace):
        if event["name"] != expected_post or not event.get("success"):
            continue
        if index + 1 >= len(trace) or trace[index + 1]["name"] != "ApplyLayout":
            raise RuntimeError(
                f"{configuration}: successful {expected_post} was not immediately followed by "
                f"ApplyLayout; local trace={trace[max(0, index - 1): index + 3]}"
            )


# ---------------------------------------------------------------------------
# One measured run
# ---------------------------------------------------------------------------

def run_one(case: CircuitCase, target, backend_name: str, seed: int, configuration: str) -> dict[str, Any]:
    layout_solver = "glasgow" if configuration.startswith("glasgow_") else "vf2"
    post_solver = "glasgow" if configuration.endswith("_glasgow") else "vf2"

    row: dict[str, Any] = {field: "" for field in RAW_FIELDS}
    row.update(
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "backend": backend_name,
            "circuit_id": case.circuit_id,
            "family": case.family,
            "num_qubits": case.circuit.num_qubits,
            "circuit_seed": "" if case.circuit_seed is None else case.circuit_seed,
            "transpiler_seed": seed,
            "configuration": configuration,
            "layout_solver": layout_solver,
            "postlayout_solver": post_solver,
            "status": "error",
        }
    )

    try:
        build_start = time.perf_counter()
        pm = build_pipeline(target, seed, configuration)
        row["pm_build_ms"] = (time.perf_counter() - build_start) * 1000.0

        callback, state = make_callback()
        run_start = time.perf_counter()
        out = pm.run(case.circuit, callback=callback)
        row["run_wall_ms"] = (time.perf_counter() - run_start) * 1000.0

        validate_trace(configuration, state)

        layout_event = state["layout_events"][0]
        post_events = state["post_events"]
        routing_post = next((e for e in post_events if not e["strict"]), None)
        strict_post = next((e for e in post_events if e["strict"]), None)

        row["callback_pass_ms"] = sum(float(e["ms"]) for e in state["trace"])
        row["layout_ms"] = layout_event["ms"]
        row["layout_status"] = layout_event.get("status", "")
        row["layout_success"] = layout_event.get("status") == "SOLUTION_FOUND"
        row["sabre_layout_ms"] = sum(e["ms"] for e in state["trace"] if e["name"] == "SabreLayout")
        row["sabre_swap_ms"] = sum(e["ms"] for e in state["trace"] if e["name"] == "SabreSwap")

        row["routing_post_attempted"] = routing_post is not None
        if routing_post is not None:
            row["routing_post_ms"] = routing_post["ms"]
            row["routing_post_success"] = routing_post["success"]
            row["routing_post_status"] = routing_post["status"]

        row["strict_post_attempted"] = strict_post is not None
        if strict_post is not None:
            row["strict_post_ms"] = strict_post["ms"]
            row["strict_post_success"] = strict_post["success"]
            row["strict_post_status"] = strict_post["status"]

        row["depth"] = out.depth()
        row["size"] = out.size()
        row["twoq_count"] = sum(
            1
            for inst in out.data
            if inst.operation.num_qubits == 2
            and not getattr(inst.operation, "_directive", False)
        )

        error_metrics = aggregate_error_metrics(out, target)
        row.update(error_metrics)

        final_mapping = None
        if out.layout is not None:
            final_mapping = out.layout.final_index_layout(filter_ancillas=True)
        row["final_mapping_json"] = json.dumps(final_mapping)
        row["pass_trace_json"] = json.dumps(state["trace"], separators=(",", ":"))
        row["status"] = "ok"
        row["error"] = ""

    except Exception as exc:  # Keep the raw experiment resumable after a single failure.
        row["error"] = f"{type(exc).__name__}: {exc}"

    return row


# ---------------------------------------------------------------------------
# CSV persistence and aggregation
# ---------------------------------------------------------------------------

def append_raw(path: Path, row: dict[str, Any]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def load_latest_rows(path: Path) -> dict[tuple[str, str, int], dict[str, str]]:
    latest: dict[tuple[str, str, int], dict[str, str]] = {}
    if not path.exists():
        return latest
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row["circuit_id"], row["configuration"], int(row["transpiler_seed"]))
            latest[key] = row
    return latest


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _stats(values: list[float], prefix: str) -> dict[str, Any]:
    if not values:
        return {
            f"{prefix}_n": 0,
            f"{prefix}_median": "",
            f"{prefix}_mean": "",
            f"{prefix}_std": "",
            f"{prefix}_q1": "",
            f"{prefix}_q3": "",
            f"{prefix}_iqr": "",
            f"{prefix}_min": "",
            f"{prefix}_max": "",
        }

    arr = np.asarray(values, dtype=float)
    q1, q3 = np.percentile(arr, [25, 75])
    return {
        f"{prefix}_n": len(values),
        f"{prefix}_median": float(np.median(arr)),
        f"{prefix}_mean": float(np.mean(arr)),
        f"{prefix}_std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        f"{prefix}_q1": float(q1),
        f"{prefix}_q3": float(q3),
        f"{prefix}_iqr": float(q3 - q1),
        f"{prefix}_min": float(np.min(arr)),
        f"{prefix}_max": float(np.max(arr)),
    }


def write_summary(raw_path: Path, summary_path: Path) -> None:
    latest = load_latest_rows(raw_path)
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in latest.values():
        groups[(row["circuit_id"], row["configuration"])].append(row)

    summary_rows: list[dict[str, Any]] = []
    metric_fields = [
        ("run_wall_ms", "run_ms"),
        ("pm_build_ms", "pm_build_ms"),
        ("layout_ms", "layout_ms"),
        ("routing_post_ms", "routing_post_ms"),
        ("strict_post_ms", "strict_post_ms"),
        ("sabre_layout_ms", "sabre_layout_ms"),
        ("sabre_swap_ms", "sabre_swap_ms"),
        ("depth", "depth"),
        ("twoq_count", "twoq"),
        ("nll_score", "nll"),
        ("estimated_error", "estimated_error"),
    ]

    for (circuit_id, configuration), rows in sorted(groups.items()):
        ok_rows = [row for row in rows if row["status"] == "ok"]
        first = rows[0]
        out: dict[str, Any] = {
            "circuit_id": circuit_id,
            "family": first["family"],
            "num_qubits": first["num_qubits"],
            "circuit_seed": first["circuit_seed"],
            "configuration": configuration,
            "layout_solver": first["layout_solver"],
            "postlayout_solver": first["postlayout_solver"],
            "requested_runs": len(rows),
            "successful_runs": len(ok_rows),
            "failed_runs": len(rows) - len(ok_rows),
        }

        for raw_field, prefix in metric_fields:
            values = [v for row in ok_rows if (v := _to_float(row[raw_field])) is not None]
            out.update(_stats(values, prefix))

        layout_success = [
            value
            for row in ok_rows
            if (value := _to_bool(row["layout_success"])) is not None
        ]
        routing_attempted = [
            row for row in ok_rows if _to_bool(row["routing_post_attempted"]) is True
        ]
        routing_success = [
            value
            for row in routing_attempted
            if (value := _to_bool(row["routing_post_success"])) is not None
        ]
        strict_attempted = [
            row for row in ok_rows if _to_bool(row["strict_post_attempted"]) is True
        ]
        strict_success = [
            value
            for row in strict_attempted
            if (value := _to_bool(row["strict_post_success"])) is not None
        ]

        out["layout_success_rate"] = (
            sum(layout_success) / len(layout_success) if layout_success else ""
        )
        out["routing_post_attempt_rate"] = (
            len(routing_attempted) / len(ok_rows) if ok_rows else ""
        )
        out["routing_post_success_rate_when_attempted"] = (
            sum(routing_success) / len(routing_success) if routing_success else ""
        )
        out["strict_post_success_rate_when_attempted"] = (
            sum(strict_success) / len(strict_success) if strict_success else ""
        )
        out["unique_final_mappings"] = len(
            {row["final_mapping_json"] for row in ok_rows if row["final_mapping_json"]}
        )

        summary_rows.append(out)

    if not summary_rows:
        return

    all_fields: list[str] = []
    for row in summary_rows:
        for key in row:
            if key not in all_fields:
                all_fields.append(key)

    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=all_fields)
        writer.writeheader()
        writer.writerows(summary_rows)


# ---------------------------------------------------------------------------
# Reproducibility metadata
# ---------------------------------------------------------------------------

def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_commit(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_metadata(output_dir: Path, backend, target, suite: list[CircuitCase], args) -> None:
    try:
        import glasgow_module

        module_path = Path(glasgow_module.__file__).resolve()
        core_candidates = sorted(module_path.parent.glob("_core*"))
        core_path = core_candidates[0] if core_candidates else None
    except Exception:
        module_path = None
        core_path = None

    script_dir = Path(__file__).resolve().parent
    # Expected local layout when the script is placed in qiskit/test_wrapper:
    #   qiskit-glasgow-work/qiskit/test_wrapper/benchmark_vf2_glasgow_ibm.py
    #   qiskit-glasgow-work/glasgow-subgraph-solver/
    wrapper_repo = script_dir.parent
    solver_repo = script_dir.parent.parent / "glasgow-subgraph-solver"

    experiment = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "backend": backend.name,
        "optimization_level": OPTIMIZATION_LEVEL,
        "suite": args.suite,
        "transpiler_seed_start": args.seed_start,
        "transpiler_seed_count": args.seeds,
        "configurations": list(CONFIGS),
        "python": sys.version,
        "platform": platform.platform(),
        "qiskit": qiskit.__version__,
        "qiskit_ibm_runtime": _package_version("qiskit-ibm-runtime"),
        "glasgow_layout_package": _package_version("glasgow_layout"),
        "glasgow_module": None if module_path is None else str(module_path),
        "glasgow_core_sha256": None if core_path is None else _sha256(core_path),
        "wrapper_git_commit": _git_commit(wrapper_repo),
        "solver_git_commit": _git_commit(solver_repo),
    }

    (output_dir / "experiment.json").write_text(
        json.dumps(experiment, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output_dir / "backend_snapshot.json").write_text(
        json.dumps(
            {
                "backend": backend.name,
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "target": snapshot_target(target),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    suite_json = []
    for case in suite:
        suite_json.append(
            {
                "circuit_id": case.circuit_id,
                "family": case.family,
                "num_qubits": case.circuit.num_qubits,
                "depth_before_transpile": case.circuit.depth(),
                "size_before_transpile": case.circuit.size(),
                "twoq_before_transpile": sum(
                    1
                    for inst in case.circuit.data
                    if inst.operation.num_qubits == 2
                    and not getattr(inst.operation, "_directive", False)
                ),
                "circuit_seed": case.circuit_seed,
                "metadata": case.metadata or {},
            }
        )
    (output_dir / "suite.json").write_text(
        json.dumps(suite_json, indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="ibm_rensselaer")
    parser.add_argument(
        "--account-name",
        default=None,
        help="Saved QiskitRuntimeService account name. Omit to use the default saved account.",
    )
    parser.add_argument("--suite", choices=("smoke", "full"), default="full")
    parser.add_argument("--seeds", type=int, default=30, help="Number of transpiler seeds")
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory. Reusing an existing directory resumes completed runs.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Optional comma-separated circuit-id substrings to select a subset.",
    )
    parser.add_argument("--no-warmup", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.seeds <= 0:
        raise SystemExit("--seeds must be positive")

    service = (
        QiskitRuntimeService(name=args.account_name)
        if args.account_name
        else QiskitRuntimeService()
    )
    backend = service.backend(args.backend)

    # Fetch once and reuse this same Target object for the entire invocation.
    target = backend.target
    if target is None or target.num_qubits is None:
        raise RuntimeError(f"Backend {args.backend} did not provide a usable Target")

    suite = make_suite(target, args.suite)
    if args.only:
        needles = [item.strip() for item in args.only.split(",") if item.strip()]
        suite = [case for case in suite if any(token in case.circuit_id for token in needles)]
    if not suite:
        raise RuntimeError("Circuit selection is empty")

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = Path(f"results/vf2_glasgow_{args.backend}_{stamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_path = output_dir / "raw_results.csv"
    summary_path = output_dir / "summary.csv"

    write_metadata(output_dir, backend, target, suite, args)

    print(f"Backend: {backend.name}")
    print(f"Physical qubits: {target.num_qubits}")
    print(f"Suite: {args.suite} ({len(suite)} circuit instances)")
    print(f"Transpiler seeds: {args.seed_start}..{args.seed_start + args.seeds - 1}")
    print(f"Configurations: {', '.join(CONFIGS)}")
    print(f"Output: {output_dir}")
    print()

    # Warm all four code paths once.  This is intentionally excluded from raw_results.csv.
    if not args.no_warmup:
        warm_case = min(suite, key=lambda case: (case.circuit.num_qubits, case.circuit.size()))
        warm_seed = args.seed_start + 100_000_003
        print(f"Warm-up circuit: {warm_case.circuit_id}")
        for configuration in CONFIGS:
            print(f"  warm-up {configuration} ...", end="", flush=True)
            row = run_one(warm_case, target, backend.name, warm_seed, configuration)
            if row["status"] != "ok":
                raise RuntimeError(
                    f"Warm-up failed for {configuration}: {row['error']}"
                )
            print(" ok")
        print()

    latest = load_latest_rows(raw_path)
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    total_requested = len(suite) * len(seeds) * len(CONFIGS)
    successful_before = sum(1 for row in latest.values() if row["status"] == "ok")
    print(f"Requested measured runs: {total_requested}")
    if latest:
        print(f"Existing successful runs found: {successful_before}")
    print()

    completed_now = 0
    failures_now = 0

    for case_index, case in enumerate(suite, start=1):
        print(
            f"[{case_index:02d}/{len(suite):02d}] {case.circuit_id} "
            f"({case.circuit.num_qubits}q, depth={case.circuit.depth()}, size={case.circuit.size()})"
        )

        for seed in seeds:
            # Rotate and periodically reverse the order so no configuration is
            # systematically first or last across the seed sweep.
            offset = (seed - args.seed_start) % len(CONFIGS)
            order = list(CONFIGS[offset:] + CONFIGS[:offset])
            if ((seed - args.seed_start) // len(CONFIGS)) % 2:
                order.reverse()

            for configuration in order:
                key = (case.circuit_id, configuration, seed)
                existing = latest.get(key)
                if existing is not None and existing["status"] == "ok":
                    continue

                print(
                    f"  seed={seed:3d}  {configuration:15s} ... ",
                    end="",
                    flush=True,
                )

                row = run_one(case, target, backend.name, seed, configuration)
                append_raw(raw_path, row)
                latest[key] = {k: str(v) for k, v in row.items()}

                if row["status"] == "ok":
                    completed_now += 1
                    print(
                        f"{float(row['run_wall_ms']):9.3f} ms  "
                        f"layout={float(row['layout_ms']):8.3f} ms  "
                        f"depth={int(row['depth'])}  2q={int(row['twoq_count'])}  "
                        f"NLL={float(row['nll_score']):.6f}"
                    )
                else:
                    failures_now += 1
                    print(f"FAILED: {row['error']}")

            # Keep a usable partial summary if the process is interrupted.
            write_summary(raw_path, summary_path)

        print()

    write_summary(raw_path, summary_path)

    latest = load_latest_rows(raw_path)
    successful = sum(1 for row in latest.values() if row["status"] == "ok")
    failed = sum(1 for row in latest.values() if row["status"] != "ok")

    print("Finished.")
    print(f"Successful latest runs: {successful}/{total_requested}")
    print(f"Failed latest runs: {failed}")
    print(f"Raw results: {raw_path}")
    print(f"Summary: {summary_path}")
    print(f"Backend snapshot: {output_dir / 'backend_snapshot.json'}")
    print(f"Suite definition: {output_dir / 'suite.json'}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
