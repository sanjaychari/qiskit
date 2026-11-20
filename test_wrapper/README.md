# HOWTO

Every dependency is listed in `pyproject.toml`

```
python -m pip install . --no-build-isolation --no-deps
```

This builds the plugin, and makes it interfaceable with qiskit.

### Testing

You will need to call transpile on some example gates. Here is some example code:

```

from qiskit import QuantumCircuit, transpile
from qiskit.providers.fake_provider import GenericBackendV2

edges = [
    (0, 1), (1, 0),
    (1, 2), (2, 1),
    (2, 3), (3, 2),
    (3, 4), (4, 3),
]

backend = GenericBackendV2(
    num_qubits=5,
    coupling_map=edges,
)

qc = QuantumCircuit(3)
qc.h(0)
qc.cx(0, 1)
qc.cx(1, 2)
qc.measure_all()

result = transpile(
    qc,
    backend=backend,
    optimization_level=3,
    layout_method="glasgow",
    routing_method="glasgow_post",
    seed_transpiler=42,
)

print(result.draw(output="text"))
print("Final layout:", result.layout)
print("Operations:", result.count_ops())

```