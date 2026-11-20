#include "glasgow.h"

int glasgow_qiskit_import(void)
{
    return qk_import();
}

void glasgow_mapping(QkDag *dag, QkTarget *target, subgraph_vec mapping)
{
    printf("HELLO FROM C...glasgow\n");

    QISKIT_NODE n = qk_dag_num_qubits(dag);

    for (QISKIT_NODE i = 0; i < n; i++) {
        mapping[i] = i;
    }

    return;
}

void glasgow_mapping_post(QkDag *dag, QkTarget *target, subgraph_vec mapping)
{
    printf("HELLO FROM C...glasgow_post\n");

    QISKIT_NODE n = qk_dag_num_qubits(dag);

    for (QISKIT_NODE i = 0; i < n; i++) {
        mapping[i] = i;
    }

    return;
}
