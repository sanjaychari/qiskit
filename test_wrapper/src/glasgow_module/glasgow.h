#define QISKIT_PYTHON_EXTENSION

#include <stdint.h>
#include <Python.h>
#include <qiskit.h>

#ifndef GLASGOW
#define GLASGOW

typedef uint32_t QISKIT_NODE;
typedef QISKIT_NODE* subgraph_vec;

void glasgow_mapping(QkDag *dag, QkTarget *target, subgraph_vec mapping);
void glasgow_mapping_post(QkDag *dag, QkTarget *target, subgraph_vec mapping);

#endif
