"""The test tree's module layout is load-bearing: get it wrong and nothing collects.

pytest imports every test module and conftest in prepend mode. It walks up from
the file to the first directory without an `__init__.py` and names the module
after the path below that directory. That means the package boundary of each
test root decides its module names:

* `packages/core/tests` is a package, so its modules are `tests.<name>`.
* Every other test root is not a package, so its modules are plain `<name>`.

Giving a second root an `__init__.py` makes it a package too, and its conftest
becomes `tests.conftest` -- the same name the core root already claimed. pytest
then aborts before collecting anything:

    ImportError while loading conftest 'packages/backend/tests/conftest.py'
    _pytest.pathlib.ImportPathMismatchError:
        ('tests.conftest',
         'packages/core/tests/conftest.py',
         'packages/backend/tests/conftest.py')

The same failure appears for `apps/api/tests` and `apps/desktop/backend/tests`,
and adding all three changes nothing: one extra `__init__.py` is enough to stop
the whole suite. So the asymmetry below is deliberate, and these tests exist so
the next tidy-up that tries to make the five roots uniform fails loudly here
instead of taking collection down for everyone.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# The roots named by `testpaths` in pyproject.toml.
TEST_ROOTS = (
    "packages/core/tests",
    "packages/cli/tests",
    "packages/backend/tests",
    "apps/api/tests",
    "apps/desktop/backend/tests",
)

#: The only root allowed to be a package; see the module docstring.
PACKAGE_ROOT = "packages/core/tests"


def test_only_one_test_root_is_a_package():
    packaged = [name for name in TEST_ROOTS if (ROOT / name / "__init__.py").is_file()]
    assert packaged == [PACKAGE_ROOT], (
        "Exactly one test root may be a package: "
        + repr(packaged)
        + ". A second one gives its conftest the module name tests.conftest, "
        "which the packaged root already owns, and pytest stops collecting."
    )


def test_unpackaged_test_roots_never_share_a_test_module_name():
    # Two plain test modules with the same basename collide the same way one step
    # along: the second import is resolved to the first file and pytest aborts
    # with "which is not the same as the test file we want to collect". conftest
    # is exempt because pytest deletes it from sys.modules before each import,
    # which is exactly how the five roots can each keep their own copy.
    owners: dict[str, str] = {}
    for name in TEST_ROOTS:
        if name == PACKAGE_ROOT:
            continue
        directory = ROOT / name
        files = sorted(path.name for path in directory.glob("*.py") if path.name != "conftest.py")
        assert files, f"An empty module scan is not evidence of a layout: {name}"
        for file in files:
            other = owners.setdefault(file, name)
            assert other == name, (
                f"{file} exists in both {other} and {name}; one module name, two "
                "files, and collection stops at the second one"
            )
