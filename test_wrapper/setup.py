import os
from pathlib import Path

import qiskit
from setuptools import Extension, setup

HERE = Path(__file__).resolve().parent

# By default the Glasgow fork is expected to be a sibling of the qiskit checkout:
#
#   qiskit-glasgow-work/
#       qiskit/test_wrapper/
#       glasgow-subgraph-solver/
#
# Override either path with the environment variables below if your checkout differs.
GLASGOW_ROOT = Path(
    os.environ.get(
        "GLASGOW_SOLVER_DIR",
        HERE.parent.parent / "glasgow-subgraph-solver",
    )
).resolve()
GLASGOW_BUILD_DIR = Path(
    os.environ.get("GLASGOW_BUILD_DIR", GLASGOW_ROOT / "build")
).resolve()
def _find_glasgow_library():
    override = os.environ.get("GLASGOW_LIBRARY")
    if override:
        library = Path(override).resolve()
        if library.is_file():
            return library
        raise RuntimeError(f"GLASGOW_LIBRARY does not exist: {library}")

    # CMake creates glasgow_subgraphs in the gss subdirectory, so the
    # normal build output is build/gss/libglasgow_subgraphs.a.  Keep the
    # build-root location as a fallback for manually constructed builds.
    candidates = (
        GLASGOW_BUILD_DIR / "gss" / "libglasgow_subgraphs.a",
        GLASGOW_BUILD_DIR / "libglasgow_subgraphs.a",
    )
    for library in candidates:
        if library.is_file():
            return library

    checked = "\n".join(f"  {candidate}" for candidate in candidates)
    raise RuntimeError(
        "Could not find libglasgow_subgraphs.a. Checked:\n"
        f"{checked}\n"
        "Build the patched Glasgow solver with `cmake --preset release` "
        "and `cmake --build --preset release`, or set GLASGOW_LIBRARY."
    )


GLASGOW_LIBRARY = _find_glasgow_library()

if not (GLASGOW_ROOT / "gss" / "homomorphism.hh").is_file():
    raise RuntimeError(
        "Could not find the Glasgow solver headers. Set GLASGOW_SOLVER_DIR to "
        "your patched glasgow-subgraph-solver checkout."
    )


def _gmp_prefix():
    override = os.environ.get("GMP_PREFIX")
    if override:
        return Path(override).resolve()
    for candidate in (
        Path("/opt/homebrew/opt/gmp"),
        Path("/usr/local/opt/gmp"),
        Path("/usr"),
    ):
        if (candidate / "include" / "gmp.h").exists() and (candidate / "lib").exists():
            return candidate
    raise RuntimeError(
        "Could not find GMP. Set GMP_PREFIX to the GMP installation prefix "
        "(for example /opt/homebrew/opt/gmp)."
    )


GMP_PREFIX = _gmp_prefix()

core_ext = Extension(
    name="glasgow_module._core",
    sources=[
        "src/glasgow_module/_gateway.cc",
        "src/glasgow_module/glasgow.cc",
    ],
    include_dirs=[
        qiskit.capi.get_include(),
        str(GLASGOW_ROOT),
        str(GMP_PREFIX / "include"),
    ],
    library_dirs=[str(GMP_PREFIX / "lib")],
    libraries=["gmpxx", "gmp"],
    extra_objects=[str(GLASGOW_LIBRARY)],
    extra_compile_args=["-std=c++20"],
    extra_link_args=["-pthread"],
    language="c++",
)

setup(ext_modules=[core_ext])
