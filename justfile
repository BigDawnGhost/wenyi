# Wenyi development entry points. Run `just` (or `just --list`) to see all recipes.
# Install just from https://github.com/casey/just#installation.
set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

# List the available recipes.
default:
    @just --list

# Install Python and Node workspace dependencies.
setup:
    uv sync --locked --all-packages --group dev
    pnpm install --frozen-lockfile

# Run Python tests; pass paths to narrow the run, e.g. `just test packages/core/tests/test_config.py`.
test *paths:
    uv run --no-sync pytest -q -n auto {{ paths }}

# Run the Core test suite.
core:
    uv run --no-sync pytest -q -n auto packages/core/tests

# Typecheck and build the Web frontend.
web:
    pnpm -C apps/web typecheck
    pnpm -C apps/web build

# Run Desktop backend tests and typecheck/build the Desktop frontend.
desktop:
    uv run --no-sync pytest -q -n auto apps/desktop/backend/tests
    pnpm -C apps/desktop/frontend typecheck
    pnpm -C apps/desktop/frontend build

# Lint the Python packages with Ruff.
lint:
    uv run --no-sync ruff check packages/core packages/cli packages/backend apps/api apps/desktop/backend scripts

# Format the Python packages with Ruff.
fmt:
    uv run --no-sync ruff format packages/core packages/cli packages/backend apps/api apps/desktop/backend scripts
