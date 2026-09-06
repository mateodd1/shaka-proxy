from setuptools import Extension, setup
from Cython.Build import cythonize

setup(
    name="proxy-shaka",
    version="1.0.3",
    ext_modules=cythonize(
        [Extension("proxy", ["src/proxy.py"])],
        compiler_directives={
            "language_level": "3",
            "embedsignature": True,
        },
        build_dir="build",
    ),
    zip_safe=False,
)
