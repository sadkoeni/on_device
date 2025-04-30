from setuptools import setup, Extension

ext = Extension(
    "c_resampler",
    sources=["c_resampler.c"],
)

setup(
    name="c_resampler",
    version="0.1",
    ext_modules=[ext],
)
