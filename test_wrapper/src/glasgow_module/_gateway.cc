#include "glasgow.h"

#include <cstdlib>

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

static PyObject *py_glasgow_mapping(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;

    static char dag_kw[] = "dag";
    static char target_kw[] = "target";
    static char strict_direction_kw[] = "strict_direction";
    static char first_call_limit_kw[] = "first_call_limit";
    static char after_call_limit_kw[] = "after_call_limit";
    static char max_trials_kw[] = "max_trials";
    static char *const kwlist[] = {
        dag_kw,
        target_kw,
        strict_direction_kw,
        first_call_limit_kw,
        after_call_limit_kw,
        max_trials_kw,
        NULL,
    };

    PyObject *dag_pyobj;
    QkDag *dag;
    QkTarget *target;
    int strict_direction = 0;
    long long first_call_limit = -1;
    long long after_call_limit = -1;
    long long max_trials = -1;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO&|pLLL",
            kwlist,
            &dag_pyobj,
            qk_target_convert_from_python,
            &target,
            &strict_direction,
            &first_call_limit,
            &after_call_limit,
            &max_trials)) {
        return NULL;
    }

    dag = qk_dag_borrow_from_python(dag_pyobj);
    const QISKIT_NODE n = qk_dag_num_qubits(dag);
    subgraph_vec mapping = (subgraph_vec)malloc(n * sizeof(QISKIT_NODE));
    if (!mapping)
        return PyErr_NoMemory();

    if (!glasgow_mapping(
            dag,
            target,
            mapping,
            strict_direction != 0,
            first_call_limit,
            after_call_limit,
            max_trials)) {
        free(mapping);
        if (PyErr_Occurred())
            return NULL;
        Py_RETURN_NONE;
    }

    PyObject *out = make_python_layout(dag_pyobj, mapping, n);
    free(mapping);
    return out;
}

static PyObject *py_glasgow_mapping_post(PyObject *self, PyObject *args, PyObject *kwargs){
    (void)self;

    static char dag_kw[] = "dag";
    static char target_kw[] = "target";
    static char strict_direction_kw[] = "strict_direction";
    static char first_call_limit_kw[] = "first_call_limit";
    static char after_call_limit_kw[] = "after_call_limit";
    static char max_trials_kw[] = "max_trials";
    static char *const kwlist[] = {
        dag_kw,
        target_kw,
        strict_direction_kw,
        first_call_limit_kw,
        after_call_limit_kw,
        max_trials_kw,
        NULL,
    };

    PyObject *dag_pyobj;
    QkDag *dag;
    QkTarget *target;
    int strict_direction = 0;
    long long first_call_limit = -1;
    long long after_call_limit = -1;
    long long max_trials = -1;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO&|pLLL",
            kwlist,
            &dag_pyobj,
            qk_target_convert_from_python,
            &target,
            &strict_direction,
            &first_call_limit,
            &after_call_limit,
            &max_trials)) {
        return NULL;
    }

    dag = qk_dag_borrow_from_python(dag_pyobj);
    const QISKIT_NODE n = qk_dag_num_qubits(dag);
    subgraph_vec mapping = (subgraph_vec)malloc(n * sizeof(QISKIT_NODE));
    if (!mapping)
        return PyErr_NoMemory();

    if (!glasgow_mapping_post(
            dag,
            target,
            mapping,
            strict_direction != 0,
            first_call_limit,
            after_call_limit,
            max_trials)) {
        free(mapping);
        if (PyErr_Occurred())
            return NULL;
        Py_RETURN_NONE;
    }

    PyObject *out = make_python_layout(dag_pyobj, mapping, n);
    free(mapping);
    return out;
}

static PyMethodDef core_methods[] = {
    {"glasgow_mapping",
     (PyCFunction)(void (*)(void))py_glasgow_mapping,
     METH_VARARGS | METH_KEYWORDS, ""},
    {"glasgow_mapping_post",
     (PyCFunction)(void (*)(void))py_glasgow_mapping_post,
     METH_VARARGS | METH_KEYWORDS, ""},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef core_module = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "_core",
    .m_methods = core_methods,
};

PyMODINIT_FUNC PyInit__core(void) {
    if (qk_import() < 0)
        return NULL;
    if (glasgow_qiskit_import() < 0)
        return NULL;
    return PyModuleDef_Init(&core_module);
}
