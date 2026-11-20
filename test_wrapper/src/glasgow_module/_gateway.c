#include "glasgow.h" // TODO: This is where we will work 

// This API slop comes from here https://quantum.cloud.ibm.com/docs/en/qiskit/c-extension-for-python

static bool init_python_vars(PyObject **dag, QkTarget **target, PyObject *args, PyObject *kwargs){
    static char *const kwlist[] = {"dag", "target", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OO&", kwlist,
                                     dag,
                                     qk_target_convert_from_python, target)) {
        // An error has occurred. The Python exception state will already
        // be set, so we need to return the error indicator.
        return false;
    }
    return true;
}

static PyObject *make_python_layout(
    PyObject *dag,
    const subgraph_vec mapping,
    QISKIT_NODE n
) {
    PyObject *qubits = PyObject_GetAttrString(dag, "qubits");
    if (!qubits) return NULL;

    PyObject *items = PySequence_Fast(qubits, "Expected qubit sequence");
    Py_DECREF(qubits);
    if (!items) return NULL;

    PyObject *dict = PyDict_New();
    if (!dict) {
        Py_DECREF(items);
        return NULL;
    }

    for (uint32_t i = 0; i < n; i++) {
        PyObject *physical = PyLong_FromUnsignedLong(mapping[i]);

        if (!physical ||
            PyDict_SetItem(
                dict,
                PySequence_Fast_GET_ITEM(items, i),
                physical
            ) < 0) {
            Py_XDECREF(physical);
            Py_DECREF(dict);
            Py_DECREF(items);
            return NULL;
        }

        Py_DECREF(physical);
    }

    Py_DECREF(items);

    PyObject *module = PyImport_ImportModule("qiskit.transpiler");
    if (!module) {
        Py_DECREF(dict);
        return NULL;
    }

    PyObject *cls = PyObject_GetAttrString(module, "Layout");
    Py_DECREF(module);
    if (!cls) {
        Py_DECREF(dict);
        return NULL;
    }

    PyObject *result = PyObject_CallOneArg(cls, dict);

    Py_DECREF(cls);
    Py_DECREF(dict);

    return result;
}

static PyObject *py_glasgow_mapping_post(PyObject *self, PyObject *args, PyObject *kwargs){
    
    PyObject *dag_pyobj;
    QkDag *dag;

    QkTarget *target;
    subgraph_vec mapping;
    PyObject *out;
    QISKIT_NODE N;

    if (!init_python_vars(&dag_pyobj, &target, args, kwargs)) {
        return NULL;
    }

    dag = qk_dag_borrow_from_python(dag_pyobj);
    N = qk_dag_num_qubits(dag);

    // Now we have C-native types, we can delegate to our C logic.

    mapping = (subgraph_vec)malloc(qk_target_num_qubits(target) * sizeof(QISKIT_NODE));
    if (!mapping) {
        return PyErr_NoMemory();
    }
    glasgow_mapping_post(dag, target, mapping);
    out = make_python_layout(dag_pyobj, mapping, N);

    free(mapping);

    return out;
}

static PyObject *py_glasgow_mapping(PyObject *self, PyObject *args, PyObject *kwargs) {
    // Define space to hold the C-native handles we will parse out of the
    // Python-space inputs.
    PyObject *dag_pyobj;
    QkDag *dag;

    QkTarget *target;
    subgraph_vec mapping;
    PyObject *out;
    QISKIT_NODE N;

    if (!init_python_vars(&dag_pyobj, &target, args, kwargs)) {
        return NULL;
    }

    dag = qk_dag_borrow_from_python(dag_pyobj);
    N = qk_dag_num_qubits(dag);

    // Now we have C-native types, we can delegate to our C logic.

    mapping = (subgraph_vec)malloc(qk_target_num_qubits(target) * sizeof(QISKIT_NODE));
    if (!mapping) {
        return PyErr_NoMemory();
    }
    glasgow_mapping(dag, target, mapping);
    out = make_python_layout(dag_pyobj, mapping, N);

    free(mapping);

    return out;
}

static PyMethodDef core_methods[] = {
    // This entry is our function, cast to the correct type.
    {"glasgow_mapping",
     (PyCFunction)(void (*)(void))py_glasgow_mapping,
     METH_VARARGS | METH_KEYWORDS, ""},
    {"glasgow_mapping_post",
     (PyCFunction)(void (*)(void))py_glasgow_mapping_post,
     METH_VARARGS | METH_KEYWORDS, ""},
    // A sentinel marking the end of the list.
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef core_module = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "_core",
    .m_methods = core_methods,
};

PyMODINIT_FUNC PyInit__core(void) {
    /*
     * qk_import() must be called once for every compilation unit that
     * directly uses the Qiskit C API.
     */

    /* Initialize the Qiskit C API table used by _gateway.c. */
    if (qk_import() < 0) {
        return NULL;
    }

    /* Initialize the separate Qiskit C API table used by glasgow.c. */
    if (glasgow_qiskit_import() < 0) {
        return NULL;
    }

    return PyModuleDef_Init(&core_module);
}
