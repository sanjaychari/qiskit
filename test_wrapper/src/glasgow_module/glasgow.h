#ifndef GLASGOW_QISKIT_PLUGIN_H
#define GLASGOW_QISKIT_PLUGIN_H

#define QISKIT_PYTHON_EXTENSION

#include <Python.h>
#include <qiskit.h>
#include <stdbool.h>
#include <stdint.h>

typedef uint32_t QISKIT_NODE;
typedef QISKIT_NODE *subgraph_vec;

/* qk_import() has per-compilation-unit state. */
int glasgow_qiskit_import(void);

/*
 * Return true and fill mapping[0:qk_dag_num_qubits(dag)] when Glasgow found a
 * mapping. Return false without setting a Python exception for the normal
 * no-solution/no-improvement cases. On an internal error, return false with a
 * Python exception set.
 */
bool glasgow_mapping(QkDag *dag, QkTarget *target, subgraph_vec mapping);
bool glasgow_mapping_post(QkDag *dag, QkTarget *target, subgraph_vec mapping, bool strict_direction);

#endif
