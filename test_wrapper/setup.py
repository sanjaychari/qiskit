import qiskit
from setuptools import setup, Extension

core_ext = Extension(

    name="glasgow_module._core",
    sources=["src/glasgow_module/_gateway.c", "src/glasgow_module/glasgow.c"],
    include_dirs=[qiskit.capi.get_include()],
)
setup(ext_modules=[core_ext])