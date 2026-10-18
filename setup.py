from setuptools import Extension, setup
from Cython.Build import cythonize

setup(
    name="proxy-shaka",
    version="1.2.0",
    ext_modules=cythonize(
        [
            Extension("proxy", ["src/proxy.py"]),
            Extension("smooth_streaming", ["src/smooth_streaming.py"]),
            Extension("subtitles", ["src/subtitles.py"]),
            Extension("media_mux", ["src/media_mux.py"]),
        ],
        compiler_directives={
            "language_level": "3",
            "embedsignature": True,
        },
        build_dir="build",
    ),
    zip_safe=False,
)
