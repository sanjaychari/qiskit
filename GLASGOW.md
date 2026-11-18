# Glasgow Subgraph Solver Integration

This repository contains an experimental integration of the
[Glasgow Subgraph Solver](https://github.com/ciaranm/glasgow-subgraph-solver)
into Qiskit's layout pipeline.

The integration adds two passes:

- `GlasgowLayout`, an alternative to `VF2Layout` for initial layout search.
- `GlasgowPostLayout`, an alternative to `VF2PostLayout` for post-routing layout improvement.

The existing Qiskit behavior is unchanged by default. `VF2Layout` and
`VF2PostLayout` remain the default choices. The two stages can be selected
independently through the `transpile()` API.

LightSABRE is not modified by this integration.

## Repository layout

A convenient directory layout is:

```text
qiskit-glasgow-work/
├── qiskit/
└── glasgow-subgraph-solver/
```

The Qiskit checkout contains the modified Qiskit source and the benchmark
script. The Glasgow solver is built separately and invoked as an external
executable.

## 1. Build Glasgow

Glasgow requires a C++20 compiler, CMake, and GMP.

On macOS with Homebrew:

```bash
brew install cmake gmp
```

Clone Glasgow if it is not already present:

```bash
git clone https://github.com/ciaranm/glasgow-subgraph-solver.git
cd glasgow-subgraph-solver
```

Configure and build:

```bash
cmake -S . -B build
cmake --build build -j
```

The executable should be:

```text
build/glasgow_subgraph_solver
```

Check that it runs:

```bash
./build/glasgow_subgraph_solver --help
```

If CMake does not find GMP on macOS, configure with the Homebrew prefix:

```bash
cmake -S . -B build \
    -DCMAKE_PREFIX_PATH="$(brew --prefix gmp)"
cmake --build build -j
```

## 2. Configure Qiskit to find Glasgow

The Qiskit integration locates Glasgow through the
`QISKIT_GLASGOW_EXECUTABLE` environment variable.

From the `glasgow-subgraph-solver` directory:

```bash
export QISKIT_GLASGOW_EXECUTABLE="$PWD/build/glasgow_subgraph_solver"
```

Alternatively, from the Qiskit checkout in the directory layout shown above:

```bash
export QISKIT_GLASGOW_EXECUTABLE="$PWD/../glasgow-subgraph-solver/build/glasgow_subgraph_solver"
```

Verify the path:

```bash
echo "$QISKIT_GLASGOW_EXECUTABLE"
"$QISKIT_GLASGOW_EXECUTABLE" --help
```

Two optional environment variables control the current prototype:

```bash
export QISKIT_GLASGOW_TIMEOUT=60
export QISKIT_GLASGOW_MAX_SOLUTIONS=256
```

`QISKIT_GLASGOW_TIMEOUT` is the external solver timeout in seconds.

`QISKIT_GLASGOW_MAX_SOLUTIONS` limits the number of complete Glasgow
mappings considered by the Qiskit integration when candidate enumeration is
used. A larger value can improve the best mapping found, but the current
prototype may spend substantial time scoring those mappings in Python.

## 3. Install the modified Qiskit checkout

Activate the Python environment intended for the experiment and install the
Qiskit checkout in editable mode:

```bash
cd qiskit
pip install -e .
```

For the IBM-backend benchmark, also install IBM Runtime:

```bash
pip install qiskit-ibm-runtime
```

Verify that Python is loading the local checkout:

```bash
python - <<'PY'
import qiskit

print(qiskit.__version__)
print(qiskit.__file__)
PY
```

The path should point into this Qiskit source tree rather than a separate
site-packages installation.

Verify that the Glasgow passes are importable:

```bash
python - <<'PY'
from qiskit.transpiler.passes import GlasgowLayout, GlasgowPostLayout

print(GlasgowLayout)
print(GlasgowPostLayout)
PY
```

## 4. Qiskit API usage

The integration adds two independent `transpile()` arguments:

```python
initial_layout_search_method="vf2"
post_layout_search_method="vf2"
```

Both default to `"vf2"`, so ordinary Qiskit behavior is unchanged unless a
different value is explicitly requested.

A minimal example using Glasgow for both stages is:

```python
from qiskit import transpile
from qiskit.circuit.library import quantum_volume
from qiskit_ibm_runtime import QiskitRuntimeService

service = QiskitRuntimeService(name="<account-name>")
backend = service.backend("ibm_<your backend>")

circuit = quantum_volume(
    30,
    depth=30,
    seed=12345,
)

transpiled = transpile(
    circuit,
    backend=backend,
    optimization_level=2,
    seed_transpiler=1234,
    initial_layout_search_method="glasgow",
    post_layout_search_method="glasgow",
)

print(transpiled.depth())
```

The four combinations used in the comparison experiments are:

```python
# Existing Qiskit behavior.
transpile(
    circuit,
    backend=backend,
    initial_layout_search_method="vf2",
    post_layout_search_method="vf2",
)

# Glasgow initial layout, VF2 post-layout.
transpile(
    circuit,
    backend=backend,
    initial_layout_search_method="glasgow",
    post_layout_search_method="vf2",
)

# VF2 initial layout, Glasgow post-layout.
transpile(
    circuit,
    backend=backend,
    initial_layout_search_method="vf2",
    post_layout_search_method="glasgow",
)

# Glasgow at both stages.
transpile(
    circuit,
    backend=backend,
    initial_layout_search_method="glasgow",
    post_layout_search_method="glasgow",
)
```

The two arguments are intentionally separate. This allows the initial-layout
and post-layout algorithms to be evaluated independently while keeping
LightSABRE unchanged.

## 5. Run Glasgow directly

The Qiskit integration communicates with Glasgow through its JSON graph
format. A minimal direct test can be run as follows:

```bash
cat > /tmp/pattern.json <<'EOF'
{
  "format": "gss-graph",
  "version": 1,
  "directed": false,
  "vertices": 2,
  "edges": [[0, 1]]
}
EOF

cat > /tmp/target.json <<'EOF'
{
  "format": "gss-graph",
  "version": 1,
  "directed": false,
  "vertices": 3,
  "edges": [[0, 1], [1, 2]]
}
EOF

"$QISKIT_GLASGOW_EXECUTABLE" \
    --format json \
    /tmp/pattern.json \
    /tmp/target.json
```

For a satisfiable instance, Glasgow prints a status and a mapping. Its output
also contains search statistics such as runtime, nodes, and propagations.

## 6. Benchmark Glasgow against VF2

The benchmark script is:

```text
benchmark_glasgow_vf2.sh
```

It uses an IBM Quantum Volume circuit and compares all four combinations:

```text
VF2Layout     -> LightSABRE -> VF2PostLayout
GlasgowLayout -> LightSABRE -> VF2PostLayout
VF2Layout     -> LightSABRE -> GlasgowPostLayout
GlasgowLayout -> LightSABRE -> GlasgowPostLayout
```

The script loads one backend target and reuses it for all variants so that
the compared runs use the same connectivity and calibration snapshot.

It also warms the Glasgow executable once before measured runs to avoid
charging executable and dynamic-library startup costs only to the first
Glasgow configuration.

Make the script executable:

```bash
chmod +x benchmark_glasgow_vf2.sh
```

Set the IBM account used by `qiskit-ibm-runtime`:

```bash
export ACCOUNT_NAME="<account-name>"
```

Optionally select the backend:

```bash
export BACKEND_NAME="ibm_rensselaer"
```

Then run a one-repeat smoke test:

```bash
REPEATS=1 ./benchmark_glasgow_vf2.sh
```

For timing experiments, use multiple repeats:

```bash
REPEATS=5 ./benchmark_glasgow_vf2.sh
```

The default circuit is a 30-qubit, depth-30 Quantum Volume circuit. Its size
can be changed without editing the script:

```bash
NUM_QUBITS=40 \
QV_DEPTH=40 \
REPEATS=5 \
./benchmark_glasgow_vf2.sh
```

A full example with explicit Glasgow controls is:

```bash
ACCOUNT_NAME="<account-name>" \
BACKEND_NAME="ibm_rensselaer" \
NUM_QUBITS=30 \
QV_DEPTH=30 \
REPEATS=5 \
TRANSPILER_SEED=1234 \
QISKIT_GLASGOW_TIMEOUT=60 \
QISKIT_GLASGOW_MAX_SOLUTIONS=256 \
./benchmark_glasgow_vf2.sh
```

Results are written by default to:

```text
glasgow_vf2_results/
```

The output directory can be changed with:

```bash
OUTDIR=my_results ./benchmark_glasgow_vf2.sh
```

## 7. Benchmark output

For each configuration, the script records:

- total transpilation time,
- initial-layout pass time,
- LightSABRE time,
- post-layout pass time,
- initial- and post-layout stop reasons,
- output depth,
- output size,
- two-qubit gate count,
- an estimated calibration-based error score,
- Glasgow status,
- Glasgow search nodes,
- Glasgow propagations,
- Glasgow solver runtime,
- Glasgow subprocess wall time,
- number of Glasgow mappings examined,
- Glasgow layout score information.

The most useful timing fields are:

```text
initial_pass_s
sabre_layout_s
post_pass_s
total_transpile_s
glasgow_initial_solver_runtime_ms
glasgow_initial_wall_runtime_ms
glasgow_post_solver_runtime_ms
glasgow_post_wall_runtime_ms
```

These values measure different things.

`initial_pass_s` and `post_pass_s` are the Qiskit pass runtimes. They include
all integration overhead.

`glasgow_*_wall_runtime_ms` measures the external Glasgow subprocess call.

`glasgow_*_solver_runtime_ms` is the runtime reported internally by Glasgow.

These should not be treated as interchangeable. In particular, the current
`GlasgowPostLayout` prototype may spend much more time scoring returned
candidate mappings in Python than Glasgow spends solving the subgraph
isomorphism problem itself.

## 8. Glasgow solution-limit sweep

The number of returned mappings can be varied to measure the cost of
candidate enumeration and scoring:

```bash
QISKIT_GLASGOW_MAX_SOLUTIONS=1 \
REPEATS=5 \
./benchmark_glasgow_vf2.sh

QISKIT_GLASGOW_MAX_SOLUTIONS=16 \
REPEATS=5 \
./benchmark_glasgow_vf2.sh

QISKIT_GLASGOW_MAX_SOLUTIONS=64 \
REPEATS=5 \
./benchmark_glasgow_vf2.sh

QISKIT_GLASGOW_MAX_SOLUTIONS=256 \
REPEATS=5 \
./benchmark_glasgow_vf2.sh
```

When Glasgow stops because the configured solution limit was reached, its raw
status may be reported as `aborted`. In this context, that does not
necessarily indicate a failed search; it can mean that the requested number
of complete mappings was produced and enumeration was intentionally stopped.

## 9. Current implementation notes

This integration is an experimental research prototype.

Important current properties are:

1. Glasgow is an external executable rather than an in-process Qiskit/Rust
   implementation. Serialization, temporary-file I/O, process startup, and
   output parsing are therefore included in the Qiskit pass runtime.

2. Glasgow itself solves the structural subgraph-isomorphism problem. Qiskit
   error information is used separately to select among returned mappings.

3. `GlasgowPostLayout` currently enumerates complete mappings and scores them
   in Python. On large routed circuits, this Python-side scoring can dominate
   the pass runtime. Therefore a raw `GlasgowPostLayout` versus
   `VF2PostLayout` pass-time comparison is an end-to-end implementation
   comparison, not a pure solver-algorithm comparison.

4. The benchmark's `estimated_error_nll` is a calibration-derived proxy, not
   a measured hardware fidelity. Lower is intended to be better. If a target
   instruction is reported with error greater than or equal to 1.0, the
   current diagnostic metric returns infinity.

5. Optimization level 2 is used in the benchmark so that the normal
   initial-layout, LightSABRE, and post-layout workflow is exercised without
   the additional strict post-layout optimization used at optimization level
   3.

## 10. Reproducibility checklist

Before recording benchmark results, verify:

```bash
git rev-parse HEAD
python - <<'PY'
import qiskit
print(qiskit.__version__)
print(qiskit.__file__)
PY

"$QISKIT_GLASGOW_EXECUTABLE" --help
```

Record the following with each experiment:

- Qiskit commit,
- Glasgow commit,
- Qiskit version,
- Python version,
- compiler/build configuration,
- backend name,
- target snapshot time,
- circuit family,
- circuit width and depth,
- circuit seed,
- transpiler seed,
- optimization level,
- Glasgow timeout,
- Glasgow solution limit,
- number of repeats.

This makes solver and integration changes distinguishable from changes in
backend calibration data or benchmark parameters.
