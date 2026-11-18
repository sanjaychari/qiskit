#!/usr/bin/env bash

set -euo pipefail

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

BACKEND_NAME="${BACKEND_NAME:-ibm_rensselaer}"
ACCOUNT_NAME="${ACCOUNT_NAME:-CSCI-4620-dedicated}"

# A reasonably large Quantum Volume circuit for an initial test.
NUM_QUBITS="${NUM_QUBITS:-30}"
QV_DEPTH="${QV_DEPTH:-30}"
CIRCUIT_SEED="${CIRCUIT_SEED:-12345}"

# Optimization level 2 is intentional:
# it exercises the normal layout -> LightSABRE -> post-layout path
# without the extra strict post-layout optimization used at level 3.
OPT_LEVEL="${OPT_LEVEL:-2}"

# Start with one repeat to verify everything works.
# Increase to 3 or 5 for the actual timing experiment.
REPEATS="${REPEATS:-1}"
TRANSPILER_SEED="${TRANSPILER_SEED:-1234}"

# Glasgow controls.
export QISKIT_GLASGOW_TIMEOUT="${QISKIT_GLASGOW_TIMEOUT:-60}"
export QISKIT_GLASGOW_MAX_SOLUTIONS="${QISKIT_GLASGOW_MAX_SOLUTIONS:-256}"

# If the executable was not explicitly provided, assume the directory
# layout:
#
# qiskit-glasgow-work/
#   qiskit/
#   glasgow-subgraph-solver/
#
if [[ -z "${QISKIT_GLASGOW_EXECUTABLE:-}" ]]; then
    DEFAULT_GLASGOW="../glasgow-subgraph-solver/build/glasgow_subgraph_solver"
    if [[ -x "${DEFAULT_GLASGOW}" ]]; then
        export QISKIT_GLASGOW_EXECUTABLE="$(cd "$(dirname "${DEFAULT_GLASGOW}")" && pwd)/$(basename "${DEFAULT_GLASGOW}")"
    fi
fi

if [[ -z "${QISKIT_GLASGOW_EXECUTABLE:-}" ]]; then
    echo "ERROR: QISKIT_GLASGOW_EXECUTABLE is not set."
    exit 1
fi

if [[ ! -x "${QISKIT_GLASGOW_EXECUTABLE}" ]]; then
    echo "ERROR: Glasgow executable is not executable:"
    echo "  ${QISKIT_GLASGOW_EXECUTABLE}"
    exit 1
fi

OUTDIR="${OUTDIR:-glasgow_vf2_results}"
mkdir -p "${OUTDIR}"

export BACKEND_NAME
export ACCOUNT_NAME
export NUM_QUBITS
export QV_DEPTH
export CIRCUIT_SEED
export OPT_LEVEL
export REPEATS
export TRANSPILER_SEED
export OUTDIR

echo "============================================================"
echo "Qiskit VF2 / Glasgow comparison"
echo "============================================================"
echo "Backend:                  ${BACKEND_NAME}"
echo "Account:                  ${ACCOUNT_NAME}"
echo "Circuit:                  quantum_volume"
echo "Qubits:                   ${NUM_QUBITS}"
echo "Depth parameter:          ${QV_DEPTH}"
echo "Optimization level:       ${OPT_LEVEL}"
echo "Repeats:                  ${REPEATS}"
echo "Glasgow executable:       ${QISKIT_GLASGOW_EXECUTABLE}"
echo "Glasgow timeout:          ${QISKIT_GLASGOW_TIMEOUT} s"
echo "Glasgow max solutions:    ${QISKIT_GLASGOW_MAX_SOLUTIONS}"
echo "Output directory:         ${OUTDIR}"
echo "============================================================"

python - <<'PY'
import csv
import inspect
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone

import qiskit
from qiskit import transpile
from qiskit.circuit.library import quantum_volume
from qiskit.transpiler.passes import GlasgowLayout, GlasgowPostLayout

try:
    from qiskit_ibm_runtime import QiskitRuntimeService
except ImportError:
    print(
        "ERROR: qiskit-ibm-runtime is not installed.\n"
        "Run: pip install qiskit-ibm-runtime",
        file=sys.stderr,
    )
    raise


BACKEND_NAME = os.environ["BACKEND_NAME"]
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "")
NUM_QUBITS = int(os.environ["NUM_QUBITS"])
QV_DEPTH = int(os.environ["QV_DEPTH"])
CIRCUIT_SEED = int(os.environ["CIRCUIT_SEED"])
OPT_LEVEL = int(os.environ["OPT_LEVEL"])
REPEATS = int(os.environ["REPEATS"])
TRANSPILER_SEED = int(os.environ["TRANSPILER_SEED"])
OUTDIR = os.environ["OUTDIR"]


# ------------------------------------------------------------
# Verify that the patched Qiskit API is actually being used.
# ------------------------------------------------------------

signature = inspect.signature(transpile)

for argument in (
    "initial_layout_search_method",
    "post_layout_search_method",
):
    if argument not in signature.parameters:
        raise RuntimeError(
            f"Patched Qiskit API not found: transpile() has no {argument!r} argument.\n"
            f"Loaded Qiskit from: {qiskit.__file__}"
        )

print()
print("Qiskit version:", qiskit.__version__)
print("Qiskit path:   ", qiskit.__file__)
print("GlasgowLayout: ", GlasgowLayout)
print("GlasgowPost:   ", GlasgowPostLayout)


# ------------------------------------------------------------
# Get ONE backend target.
#
# All transpilation variants below use this same Target instance,
# avoiding calibration changes between variants during this script.
# ------------------------------------------------------------

print()
print("Loading IBM backend...")

if ACCOUNT_NAME:
    service = QiskitRuntimeService(name=ACCOUNT_NAME)
else:
    service = QiskitRuntimeService()

backend = service.backend(BACKEND_NAME)
target = backend.target

snapshot_time = datetime.now(timezone.utc).isoformat()

print("Backend:       ", backend.name)
print("Target qubits: ", target.num_qubits)
print("Snapshot time: ", snapshot_time)

if NUM_QUBITS > target.num_qubits:
    raise RuntimeError(
        f"Circuit uses {NUM_QUBITS} qubits but backend only has "
        f"{target.num_qubits} qubits."
    )


# ------------------------------------------------------------
# IBM circuit-library benchmark
# ------------------------------------------------------------

print()
print("Building Quantum Volume circuit...")

circuit = quantum_volume(
    NUM_QUBITS,
    depth=QV_DEPTH,
    seed=CIRCUIT_SEED,
)

print("Input qubits:", circuit.num_qubits)
print("Input depth: ", circuit.depth())
print("Input size:  ", circuit.size())


# ------------------------------------------------------------
# Warm Glasgow before measured runs
# ------------------------------------------------------------

print()
print("Warming Glasgow executable...")

with tempfile.TemporaryDirectory() as tmp:
    pattern = {
        "format": "gss-graph",
        "version": 1,
        "directed": False,
        "vertices": 2,
        "edges": [[0, 1]],
    }
    hardware = {
        "format": "gss-graph",
        "version": 1,
        "directed": False,
        "vertices": 3,
        "edges": [[0, 1], [1, 2]],
    }

    pattern_path = os.path.join(tmp, "pattern.json")
    target_path = os.path.join(tmp, "target.json")

    with open(pattern_path, "w", encoding="utf8") as f:
        json.dump(pattern, f)

    with open(target_path, "w", encoding="utf8") as f:
        json.dump(hardware, f)

    subprocess.run(
        [
            os.environ["QISKIT_GLASGOW_EXECUTABLE"],
            "--format",
            "json",
            pattern_path,
            target_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

print("Glasgow warm-up complete.")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

INTERESTING_PASSES = {
    "VF2Layout",
    "GlasgowLayout",
    "SabreLayout",
    "SabreSwap",
    "VF2PostLayout",
    "GlasgowPostLayout",
}


def normalize_reason(value):
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def property_value(property_set, name):
    try:
        value = property_set[name]
    except Exception:
        return None
    return value


def serialize_post_layout(layout, dag):
    """Return deterministic JSON for a post-layout mapping and its physical subgraph.

    The mapping is stored as ``[[dag_qubit, physical_qubit], ...]`` so that the
    exact VF2 and Glasgow mappings can be compared directly.  The second value
    is the sorted set of physical qubits used by that mapping.
    """
    if layout is None:
        return "", ""

    qubit_indices = {qubit: index for index, qubit in enumerate(dag.qubits)}
    mapping = sorted(
        (qubit_indices[virtual], int(physical))
        for virtual, physical in layout.get_virtual_bits().items()
        if virtual in qubit_indices
    )
    physical_qubits = sorted({physical for _, physical in mapping})

    return (
        json.dumps(mapping, separators=(",", ":")),
        json.dumps(physical_qubits, separators=(",", ":")),
    )


def estimated_error_score(transpiled_circuit):
    """Approximate -log(success probability) from target gate errors.

    Lower is better.  This is not a hardware result; it is only a
    calibration-based proxy for comparing mappings.
    """
    score = 0.0
    counted = 0

    for instruction in transpiled_circuit.data:
        operation = instruction.operation

        if operation.name in {"barrier", "delay"}:
            continue

        qargs = tuple(
            transpiled_circuit.find_bit(qubit).index
            for qubit in instruction.qubits
        )

        try:
            instruction_map = target[operation.name]
        except KeyError:
            continue

        try:
            properties = instruction_map.get(qargs, None)
        except Exception:
            continue

        if properties is None or properties.error is None:
            continue

        error = float(properties.error)

        if error <= 0.0:
            continue
        if error >= 1.0:
            return float("inf"), counted + 1

        score += -math.log1p(-error)
        counted += 1

    return score, counted


def two_qubit_count(transpiled_circuit):
    return sum(
        1
        for instruction in transpiled_circuit.data
        if len(instruction.qubits) == 2
    )


def make_callback():
    pass_times = defaultdict(float)
    pass_counts = defaultdict(int)
    details = {}

    def callback(**kwargs):
        pass_object = kwargs["pass_"]
        elapsed = float(kwargs["time"])
        property_set = kwargs["property_set"]
        dag = kwargs["dag"]

        name = pass_object.__class__.__name__

        pass_times[name] += elapsed
        pass_counts[name] += 1

        if name not in INTERESTING_PASSES:
            return

        if name == "VF2Layout":
            details["initial_stop_reason"] = normalize_reason(
                property_value(property_set, "VF2Layout_stop_reason")
            )

        elif name == "GlasgowLayout":
            details["initial_stop_reason"] = normalize_reason(
                property_value(property_set, "GlasgowLayout_stop_reason")
            )

            for key in (
                "status",
                "nodes",
                "propagations",
                "solver_runtime_ms",
                "wall_runtime_ms",
                "mappings_examined",
                "score",
            ):
                details[f"glasgow_initial_{key}"] = property_value(
                    property_set,
                    f"GlasgowLayout_{key}",
                )

        elif name == "VF2PostLayout":
            details["post_stop_reason"] = normalize_reason(
                property_value(property_set, "VF2PostLayout_stop_reason")
            )
            (
                details["post_layout_mapping"],
                details["post_layout_physical_qubits"],
            ) = serialize_post_layout(
                property_value(property_set, "post_layout"),
                dag,
            )

        elif name == "GlasgowPostLayout":
            details["post_stop_reason"] = normalize_reason(
                property_value(property_set, "GlasgowPostLayout_stop_reason")
            )
            (
                details["post_layout_mapping"],
                details["post_layout_physical_qubits"],
            ) = serialize_post_layout(
                property_value(property_set, "post_layout"),
                dag,
            )

            for key in (
                "status",
                "nodes",
                "propagations",
                "solver_runtime_ms",
                "wall_runtime_ms",
                "mappings_examined",
                "scoring_runtime_ms",
                "baseline_score",
                "score",
            ):
                details[f"glasgow_post_{key}"] = property_value(
                    property_set,
                    f"GlasgowPostLayout_{key}",
                )

    return callback, pass_times, pass_counts, details


# ------------------------------------------------------------
# Four permutations
# ------------------------------------------------------------

variants = [
    ("vf2_vf2", "vf2", "vf2"),
    ("glasgow_vf2", "glasgow", "vf2"),
    ("vf2_glasgow", "vf2", "glasgow"),
    ("glasgow_glasgow", "glasgow", "glasgow"),
]


rows = []

print()
print("============================================================")
print("Running transpilation comparisons")
print("============================================================")

for repeat in range(REPEATS):
    seed = TRANSPILER_SEED + repeat

    print()
    print(f"--- repeat {repeat + 1}/{REPEATS}, transpiler seed={seed} ---")

    for variant_name, initial_method, post_method in variants:
        print(
            f"{variant_name:18s} "
            f"initial={initial_method:7s} "
            f"post={post_method:7s}",
            flush=True,
        )

        callback, pass_times, pass_counts, details = make_callback()

        start = time.perf_counter()

        result = transpile(
            circuit,
            target=target,
            optimization_level=OPT_LEVEL,
            seed_transpiler=seed,
            initial_layout_search_method=initial_method,
            post_layout_search_method=post_method,
            callback=callback,
            num_processes=1,
        )

        total_seconds = time.perf_counter() - start

        error_score, error_gate_count = estimated_error_score(result)

        initial_pass_name = (
            "GlasgowLayout"
            if initial_method == "glasgow"
            else "VF2Layout"
        )

        post_pass_name = (
            "GlasgowPostLayout"
            if post_method == "glasgow"
            else "VF2PostLayout"
        )

        row = {
            "variant": variant_name,
            "repeat": repeat,
            "seed_transpiler": seed,
            "initial_method": initial_method,
            "post_method": post_method,

            "total_transpile_s": total_seconds,

            "initial_pass_runs": pass_counts.get(initial_pass_name, 0),
            "initial_pass_s": pass_times.get(initial_pass_name, 0.0),
            "initial_stop_reason": details.get(
                "initial_stop_reason", ""
            ),

            "sabre_layout_runs": pass_counts.get("SabreLayout", 0),
            "sabre_layout_s": pass_times.get("SabreLayout", 0.0),

            "sabre_swap_runs": pass_counts.get("SabreSwap", 0),
            "sabre_swap_s": pass_times.get("SabreSwap", 0.0),

            "post_pass_runs": pass_counts.get(post_pass_name, 0),
            "post_pass_s": pass_times.get(post_pass_name, 0.0),
            "post_stop_reason": details.get(
                "post_stop_reason", ""
            ),
            "post_layout_mapping": details.get(
                "post_layout_mapping", ""
            ),
            "post_layout_physical_qubits": details.get(
                "post_layout_physical_qubits", ""
            ),

            "output_depth": result.depth(),
            "output_size": result.size(),
            "output_2q_count": two_qubit_count(result),

            "estimated_error_nll": error_score,
            "estimated_error_nll_is_finite": math.isfinite(error_score),
            "estimated_error_gate_count": error_gate_count,

            "glasgow_initial_status": details.get(
                "glasgow_initial_status", ""
            ),
            "glasgow_initial_nodes": details.get(
                "glasgow_initial_nodes", ""
            ),
            "glasgow_initial_propagations": details.get(
                "glasgow_initial_propagations", ""
            ),
            "glasgow_initial_solver_runtime_ms": details.get(
                "glasgow_initial_solver_runtime_ms", ""
            ),
            "glasgow_initial_wall_runtime_ms": details.get(
                "glasgow_initial_wall_runtime_ms", ""
            ),
            "glasgow_initial_mappings_examined": details.get(
                "glasgow_initial_mappings_examined", ""
            ),
            "glasgow_initial_score": details.get(
                "glasgow_initial_score", ""
            ),

            "glasgow_post_status": details.get(
                "glasgow_post_status", ""
            ),
            "glasgow_post_nodes": details.get(
                "glasgow_post_nodes", ""
            ),
            "glasgow_post_propagations": details.get(
                "glasgow_post_propagations", ""
            ),
            "glasgow_post_solver_runtime_ms": details.get(
                "glasgow_post_solver_runtime_ms", ""
            ),
            "glasgow_post_wall_runtime_ms": details.get(
                "glasgow_post_wall_runtime_ms", ""
            ),
            "glasgow_post_mappings_examined": details.get(
                "glasgow_post_mappings_examined", ""
            ),
            "glasgow_post_scoring_runtime_ms": details.get(
                "glasgow_post_scoring_runtime_ms", ""
            ),
            "glasgow_post_baseline_score": details.get(
                "glasgow_post_baseline_score", ""
            ),
            "glasgow_post_score": details.get(
                "glasgow_post_score", ""
            ),
        }

        rows.append(row)

        print(
            f"    total={total_seconds:.3f}s "
            f"initial={row['initial_pass_s']:.4f}s "
            f"LightSABRE={row['sabre_layout_s']:.4f}s "
            f"post={row['post_pass_s']:.4f}s "
            f"depth={row['output_depth']} "
            f"2q={row['output_2q_count']} "
            f"error_nll={row['estimated_error_nll']:.6f}"
        )

        print(
            f"    initial result: "
            f"{row['initial_stop_reason'] or 'pass not executed'}"
        )

        print(
            f"    post result:    "
            f"{row['post_stop_reason'] or 'pass not executed'}"
        )


# ------------------------------------------------------------
# Save raw CSV
# ------------------------------------------------------------

timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
csv_path = os.path.join(
    OUTDIR,
    f"glasgow_vf2_qv_{NUM_QUBITS}q_{QV_DEPTH}d_{timestamp}.csv",
)

with open(csv_path, "w", newline="", encoding="utf8") as file:
    writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


# ------------------------------------------------------------
# Summary
# ------------------------------------------------------------

print()
print("============================================================")
print("Summary")
print("============================================================")

for variant_name, _, _ in variants:
    subset = [row for row in rows if row["variant"] == variant_name]

    mean_total = statistics.mean(
        row["total_transpile_s"] for row in subset
    )
    mean_initial = statistics.mean(
        row["initial_pass_s"] for row in subset
    )
    mean_sabre = statistics.mean(
        row["sabre_layout_s"] for row in subset
    )
    mean_post = statistics.mean(
        row["post_pass_s"] for row in subset
    )
    mean_depth = statistics.mean(
        row["output_depth"] for row in subset
    )
    mean_2q = statistics.mean(
        row["output_2q_count"] for row in subset
    )
    error_values = [
        row["estimated_error_nll"] for row in subset
    ]
    finite_error_values = [
        value for value in error_values if math.isfinite(value)
    ]
    mean_error = (
        statistics.mean(finite_error_values)
        if finite_error_values
        else float("nan")
    )
    nonfinite_error_runs = len(error_values) - len(finite_error_values)

    print(
        f"{variant_name:18s} "
        f"total={mean_total:8.3f}s  "
        f"initial={mean_initial:8.4f}s  "
        f"sabre={mean_sabre:8.4f}s  "
        f"post={mean_post:8.4f}s  "
        f"depth={mean_depth:8.1f}  "
        f"2q={mean_2q:8.1f}  "
        f"error_nll_finite_mean={mean_error:.6f}  "
        f"nonfinite_error_runs={nonfinite_error_runs}/{len(error_values)}"
    )

print()
print("Raw results:", csv_path)
print("Backend snapshot time:", snapshot_time)

# Make it obvious if post-layout never ran.
if all(row["post_pass_runs"] == 0 for row in rows):
    print()
    print(
        "WARNING: no post-layout pass executed. "
        "This usually means the initial layout was already perfect. "
        "Use a denser/deeper circuit to exercise GlasgowPostLayout/VF2PostLayout."
    )

PY
