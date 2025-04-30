#define PY_SSIZE_T_CLEAN  // Ensure PyArg_ParseTuple with '#' uses Py_ssize_t
#include <Python.h>
#include <stdint.h>

// decimate: simple 3:1 down-sampler by averaging
static PyObject* decimate(PyObject* self, PyObject* args) {
    const char* in_buf;
    Py_ssize_t in_len;
    if (!PyArg_ParseTuple(args, "y#", &in_buf, &in_len)) {
        return NULL;
    }
    // Must be multiple of 6 bytes (3 samples × 2 bytes/sample)
    Py_ssize_t n_in_samples = in_len / 2;
    Py_ssize_t n_out_samples = n_in_samples / 3;
    Py_ssize_t out_len = n_out_samples * 2;

    const int16_t* src = (const int16_t*)in_buf;
    // Allocate result bytes
    PyObject* out_bytes = PyBytes_FromStringAndSize(NULL, out_len);
    if (!out_bytes) return NULL;
    int16_t* dst = (int16_t*)PyBytes_AS_STRING(out_bytes);

    for (Py_ssize_t i = 0; i < n_out_samples; i++) {
        int32_t sum = src[3*i] + src[3*i + 1] + src[3*i + 2];
        dst[i] = (int16_t)(sum / 3);
    }
    return out_bytes;
}

static PyMethodDef ResamplerMethods[] = {
    {"decimate", decimate, METH_VARARGS, "Downsample by 3 (48k→16k)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef resampler_module = {
    PyModuleDef_HEAD_INIT,
    "c_resampler",
    "Simple C decimator",
    -1,
    ResamplerMethods
};

PyMODINIT_FUNC PyInit_c_resampler(void) {
    return PyModule_Create(&resampler_module);
}
