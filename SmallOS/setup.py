from pathlib import Path

from setuptools import find_packages, setup


README = Path(__file__).resolve().parent / "README.md"

try:
    from setuptools_scm import get_version
except ImportError:
    VERSION = "0.0.0"
else:
    VERSION = get_version(root=".", fallback_version="0.0.0")

setup(
    name="SmallPackage",
    version=VERSION,
    packages=find_packages(),
    include_package_data=True,
    package_data={"SmallPackage.clients": ["README.md"]},
    install_requires=[],
    author="Michael E",
    description="Concurrent and Priority oriented Task Management System",
    long_description=README.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/MikiEEE/SmallOS",
    project_urls={
        "Documentation": "https://github.com/MikiEEE/SmallOS",
        "Source": "https://github.com/MikiEEE/SmallOS",
        "Issues": "https://github.com/MikiEEE/SmallOS/issues",
    },
    license="MIT",
    license_files=("LICENSE",),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)
