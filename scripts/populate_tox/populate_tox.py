"""
This script populates tox.ini automatically using release data from PYPI.
"""

import functools
import os
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from importlib.metadata import metadata
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pathlib import Path
from typing import Optional, Union

# Adding the scripts directory to PATH. This is necessary in order to be able
# to import stuff from the split_tox_gh_actions script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests
from jinja2 import Environment, FileSystemLoader
from sentry_sdk.integrations import _MIN_VERSIONS

from config import TEST_SUITE_CONFIG
from split_tox_gh_actions.split_tox_gh_actions import GROUPS


# Only consider package versions going back this far
CUTOFF = datetime.now() - timedelta(days=365 * 5)

TOX_FILE = Path(__file__).resolve().parent.parent.parent / "tox.ini"
ENV = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent),
    trim_blocks=True,
    lstrip_blocks=True,
)

PYPI_PROJECT_URL = "https://pypi.python.org/pypi/{project}/json"
PYPI_VERSION_URL = "https://pypi.python.org/pypi/{project}/{version}/json"
CLASSIFIER_PREFIX = "Programming Language :: Python :: "


IGNORE = {
    # Do not try auto-generating the tox entries for these. They will be
    # hardcoded in tox.ini.
    #
    # This set should be getting smaller over time as we migrate more test
    # suites over to this script. Some entries will probably stay forever
    # as they don't fit the mold (e.g. common, asgi, which don't have a 3rd party
    # pypi package to install in different versions).
    "common",
    "gevent",
    "opentelemetry",
    "potel",
    "aiohttp",
    "anthropic",
    "ariadne",
    "arq",
    "asgi",
    "asyncpg",
    "aws_lambda",
    "beam",
    "boto3",
    "bottle",
    "celery",
    "chalice",
    "clickhouse_driver",
    "cohere",
    "cloud_resource_context",
    "cohere",
    "django",
    "dramatiq",
    "falcon",
    "fastapi",
    "flask",
    "gcp",
    "gql",
    "graphene",
    "grpc",
    "httpx",
    "huey",
    "huggingface_hub",
    "langchain",
    "langchain_notiktoken",
    "launchdarkly",
    "litestar",
    "loguru",
    "openai",
    "openai_notiktoken",
    "openfeature",
    "pure_eval",
    "pymongo",
    "pyramid",
    "quart",
    "ray",
    "redis",
    "redis_py_cluster_legacy",
    "requests",
    "rq",
    "sanic",
    "spark",
    "starlette",
    "starlite",
    "sqlalchemy",
    "strawberry",
    "tornado",
    "trytond",
    "typer",
    "unleash",
}


@functools.cache
def fetch_package(package: str) -> dict:
    """Fetch package metadata from PyPI."""
    url = PYPI_PROJECT_URL.format(project=package)
    pypi_data = requests.get(url)

    if pypi_data.status_code != 200:
        print(f"{package} not found")

    return pypi_data.json()


@functools.cache
def fetch_release(package: str, version: Version) -> dict:
    url = PYPI_VERSION_URL.format(project=package, version=version)
    pypi_data = requests.get(url)

    if pypi_data.status_code != 200:
        print(f"{package} not found")

    return pypi_data.json()


def _prefilter_releases(integration: str, releases: dict[str, dict]) -> list[Version]:
    """
    Filter `releases`, removing releases that are for sure unsupported.

    This function doesn't guarantee that all releases it returns are supported --
    there are further criteria that will be checked later in the pipeline because
    they require additional API calls to be made. The purpose of this function is
    to slim down the list so that we don't have to make more API calls than
    necessary for releases that are for sure not supported.
    """
    min_supported = _MIN_VERSIONS.get(integration)
    if min_supported is not None:
        min_supported = Version(".".join(map(str, min_supported)))
    else:
        print(
            f"  {integration} doesn't have a minimum version defined in sentry_sdk/integrations/__init__.py. Consider defining one"
        )

    filtered_releases = []

    for release, data in releases.items():
        if not data:
            continue

        meta = data[0]
        if datetime.fromisoformat(meta["upload_time"]) < CUTOFF:
            continue

        if meta["yanked"]:
            continue

        version = Version(release)

        if min_supported and version < min_supported:
            continue

        if version.is_prerelease or version.is_postrelease:
            # TODO: consider the newest prerelease unless obsolete
            # https://github.com/getsentry/sentry-python/issues/4030
            continue

        for i, saved_version in enumerate(filtered_releases):
            if (
                version.major == saved_version.major
                and version.minor == saved_version.minor
                and version.micro > saved_version.micro
            ):
                # Don't save all patch versions of a release, just the newest one
                filtered_releases[i] = version
                break
        else:
            filtered_releases.append(version)

    return sorted(filtered_releases)


def get_supported_releases(integration: str, pypi_data: dict) -> list[Version]:
    """
    Get a list of releases that are currently supported by the SDK.

    This takes into account a handful of parameters (Python support, the lowest
    version we've defined for the framework, the date of the release).
    """
    package = pypi_data["info"]["name"]

    # Get a consolidated list without taking into account Python support yet
    # (because that might require an additional API call for some
    # of the releases)
    releases = _prefilter_releases(integration, pypi_data["releases"])

    # Determine Python support
    expected_python_versions = TEST_SUITE_CONFIG[integration].get("python")
    if expected_python_versions:
        expected_python_versions = SpecifierSet(expected_python_versions)
    else:
        expected_python_versions = SpecifierSet(f">={MIN_PYTHON_VERSION}")

    def _supports_lowest(release: Version) -> bool:
        time.sleep(0.1)  # don't DoS PYPI
        py_versions = determine_python_versions(fetch_release(package, release))
        target_python_versions = TEST_SUITE_CONFIG[integration].get("python")
        if target_python_versions:
            target_python_versions = SpecifierSet(target_python_versions)
        return bool(supported_python_versions(py_versions, target_python_versions))

    if not _supports_lowest(releases[0]):
        i = bisect_left(releases, True, key=_supports_lowest)
        if i != len(releases) and _supports_lowest(releases[i]):
            # we found the lowest version that supports at least some Python
            # version(s) that we do, cut off the rest
            releases = releases[i:]

    return releases


def pick_releases_to_test(releases: list[Version]) -> list[Version]:
    """Pick a handful of releases to test from a sorted list of supported releases."""
    # If the package has majors (or major-like releases, even if they don't do
    # semver), we want to make sure we're testing them all. If not, we just pick
    # the oldest, the newest, and a couple in between.
    has_majors = len(set([v.major for v in releases])) > 1
    filtered_releases = set()

    if has_majors:
        # Always check the very first supported release
        filtered_releases.add(releases[0])

        # Find out the min and max release by each major
        releases_by_major = {}
        for release in releases:
            if release.major not in releases_by_major:
                releases_by_major[release.major] = [release, release]
            if release < releases_by_major[release.major][0]:
                releases_by_major[release.major][0] = release
            if release > releases_by_major[release.major][1]:
                releases_by_major[release.major][1] = release

        for i, (min_version, max_version) in enumerate(releases_by_major.values()):
            filtered_releases.add(max_version)
            if i == len(releases_by_major) - 1:
                # If this is the latest major release, also check the lowest
                # version of this version
                filtered_releases.add(min_version)

    else:
        filtered_releases = {
            releases[0],  # oldest version supported
            releases[len(releases) // 3],
            releases[
                len(releases) // 3 * 2
            ],  # two releases in between, roughly evenly spaced
            releases[-1],  # latest
        }

    return sorted(filtered_releases)


def supported_python_versions(
    package_python_versions: Union[SpecifierSet, list[Version]],
    custom_supported_versions: Optional[SpecifierSet] = None,
) -> list[Version]:
    """
    Get the intersection of Python versions supported by the package and the SDK.

    Optionally, if `custom_supported_versions` is provided, the function will
    return the intersection of Python versions supported by the package, the SDK,
    and `custom_supported_versions`. This is used when a test suite definition
    in `TEST_SUITE_CONFIG` contains a range of Python versions to run the tests
    on.

    Examples:
    - The Python SDK supports Python 3.6-3.13. The package supports 3.5-3.8. This
      function will return [3.6, 3.7, 3.8] as the Python versions supported
      by both.
    - The Python SDK supports Python 3.6-3.13. The package supports 3.5-3.8. We
      have an additional test limitation in place to only test this framework
      on Python 3.7, so we can provide this as `custom_supported_versions`. The
      result of this function will then by the intersection of all three, i.e.,
      [3.7].
    """
    supported = []

    # Iterate through Python versions from MIN_PYTHON_VERSION to MAX_PYTHON_VERSION
    curr = MIN_PYTHON_VERSION
    while curr <= MAX_PYTHON_VERSION:
        if curr in package_python_versions:
            if not custom_supported_versions or curr in custom_supported_versions:
                supported.append(curr)

        # Construct the next Python version (i.e., bump the minor)
        next = [int(v) for v in str(curr).split(".")]
        next[1] += 1
        curr = Version(".".join(map(str, next)))

    return supported


def pick_python_versions_to_test(python_versions: list[Version]) -> list[Version]:
    """
    Given a list of Python versions, pick those that make sense to test on.

    Currently, this is the oldest, the newest, and the second newest Python
    version.
    """
    filtered_python_versions = {
        python_versions[0],
    }

    filtered_python_versions.add(python_versions[-1])
    try:
        filtered_python_versions.add(python_versions[-2])
    except IndexError:
        pass

    return sorted(filtered_python_versions)


def _parse_python_versions_from_classifiers(classifiers: list[str]) -> list[Version]:
    python_versions = []
    for classifier in classifiers:
        if classifier.startswith(CLASSIFIER_PREFIX):
            python_version = classifier[len(CLASSIFIER_PREFIX) :]
            if "." in python_version:
                # We don't care about stuff like
                # Programming Language :: Python :: 3 :: Only,
                # Programming Language :: Python :: 3,
                # etc., we're only interested in specific versions, like 3.13
                python_versions.append(Version(python_version))

    if python_versions:
        python_versions.sort()
        return python_versions


def determine_python_versions(pypi_data: dict) -> Union[SpecifierSet, list[Version]]:
    """
    Given data from PyPI's release endpoint, determine the Python versions supported by the package
    from the Python version classifiers, when present, or from `requires_python` if there are no classifiers.
    """
    try:
        classifiers = pypi_data["info"]["classifiers"]
    except (AttributeError, KeyError):
        # This function assumes `pypi_data` contains classifiers. This is the case
        # for the most recent release in the /{project} endpoint or for any release
        # fetched via the /{project}/{version} endpoint.
        return []

    # Try parsing classifiers
    python_versions = _parse_python_versions_from_classifiers(classifiers)
    if python_versions:
        return python_versions

    # We only use `requires_python` if there are no classifiers. This is because
    # `requires_python` doesn't tell us anything about the upper bound, which
    # depends on when the release first came out
    try:
        requires_python = pypi_data["info"]["requires_python"]
    except (AttributeError, KeyError):
        pass

    if requires_python:
        return SpecifierSet(requires_python)

    return []


def _render_python_versions(python_versions: list[Version]) -> str:
    return (
        "{"
        + ",".join(f"py{version.major}.{version.minor}" for version in python_versions)
        + "}"
    )


def _render_dependencies(integration: str, releases: list[Version]) -> list[str]:
    rendered = []

    if TEST_SUITE_CONFIG[integration].get("deps") is None:
        return rendered

    for constraint, deps in TEST_SUITE_CONFIG[integration]["deps"].items():
        if constraint == "*":
            for dep in deps:
                rendered.append(f"{integration}: {dep}")
        elif constraint.startswith("py3"):
            for dep in deps:
                rendered.append(f"{constraint}-{integration}: {dep}")
        else:
            restriction = SpecifierSet(constraint)
            for release in releases:
                if release in restriction:
                    for dep in deps:
                        rendered.append(f"{integration}-v{release}: {dep}")

    return rendered


def write_tox_file(packages: dict) -> None:
    template = ENV.get_template("tox.jinja")

    context = {"groups": {}}
    for group, integrations in packages.items():
        context["groups"][group] = []
        for integration in integrations:
            context["groups"][group].append(
                {
                    "name": integration["name"],
                    "package": integration["package"],
                    "extra": integration["extra"],
                    "releases": integration["releases"],
                    "dependencies": _render_dependencies(
                        integration["name"], integration["releases"]
                    ),
                }
            )

    rendered = template.render(context)

    with open(TOX_FILE, "w") as file:
        file.write(rendered)
        file.write("\n")


def _get_package_name(integration: str) -> tuple[str, Optional[str]]:
    package = TEST_SUITE_CONFIG[integration]["package"]
    extra = None
    if "[" in package:
        extra = package[package.find("[") + 1 : package.find("]")]
        package = package[: package.find("[")]

    return package, extra


def _compare_min_version_with_defined(
    integration: str, releases: list[Version]
) -> None:
    defined_min_version = _MIN_VERSIONS.get(integration)
    if defined_min_version:
        defined_min_version = Version(".".join([str(v) for v in defined_min_version]))
        if (
            defined_min_version.major != releases[0].major
            or defined_min_version.minor != releases[0].minor
        ):
            print(
                f"  Integration defines {defined_min_version} as minimum "
                f"version, but the effective minimum version is {releases[0]}."
            )


def _add_python_versions_to_release(integration: str, package: str, release: Version):
    release_pypi_data = fetch_release(package, release)
    time.sleep(0.1)  # give PYPI some breathing room

    target_python_versions = TEST_SUITE_CONFIG[integration].get("python")
    if target_python_versions:
        target_python_versions = SpecifierSet(target_python_versions)

    release.python_versions = pick_python_versions_to_test(
        supported_python_versions(
            determine_python_versions(release_pypi_data),
            target_python_versions,
        )
    )

    release.rendered_python_versions = _render_python_versions(release.python_versions)


def main() -> None:
    global MIN_PYTHON_VERSION, MAX_PYTHON_VERSION
    sdk_python_versions = _parse_python_versions_from_classifiers(
        metadata("sentry-sdk").get_all("Classifier")
    )
    MIN_PYTHON_VERSION = sdk_python_versions[0]
    MAX_PYTHON_VERSION = sdk_python_versions[-1]
    print(
        f"The SDK supports Python versions {MIN_PYTHON_VERSION} - {MAX_PYTHON_VERSION}."
    )

    packages = defaultdict(list)

    for group, integrations in GROUPS.items():
        for integration in integrations:
            if integration in IGNORE:
                continue

            print(f"Processing {integration}...")

            # Figure out the actual main package
            package, extra = _get_package_name(integration)

            # Fetch data for the main package
            pypi_data = fetch_package(package)

            # Get the list of all supported releases
            releases = get_supported_releases(integration, pypi_data)
            if not releases:
                print("  Found no supported releases.")
                continue

            _compare_min_version_with_defined(integration, releases)

            # Pick a handful of the supported releases to actually test against
            # and fetch the PYPI data for each to determine which Python versions
            # to test it on
            test_releases = pick_releases_to_test(releases)

            for release in test_releases:
                py_versions = _add_python_versions_to_release(
                    integration, package, release
                )
                if not py_versions:
                    print(f"  Release {release} has no Python versions, skipping.")

            test_releases = [
                release for release in test_releases if release.python_versions
            ]
            if test_releases:
                packages[group].append(
                    {
                        "name": integration,
                        "package": package,
                        "extra": extra,
                        "releases": test_releases,
                    }
                )

    write_tox_file(packages)


if __name__ == "__main__":
    main()
