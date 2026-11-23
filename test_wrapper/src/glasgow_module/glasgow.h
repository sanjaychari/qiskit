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
 * Limits use -1 for Qiskit's ``None``.  ``max_trials == 0`` means unbounded,
 * matching VF2PassConfiguration's normalized legacy semantics.
 */
bool glasgow_mapping(
    QkDag *dag,
    QkTarget *target,
    subgraph_vec mapping,
    bool strict_direction,
    int64_t first_call_limit,
    int64_t after_call_limit,
    int64_t max_trials);

bool glasgow_mapping_post(
    QkDag *dag,
    QkTarget *target,
    subgraph_vec mapping,
    bool strict_direction,
    int64_t first_call_limit,
    int64_t after_call_limit,
    int64_t max_trials);

#endif
