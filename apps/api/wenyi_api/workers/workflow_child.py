"""Private entry point for the workflow worker's project writer."""

from wenyi_backend.context import use_context
from wenyi_backend.execution import process, runner
from wenyi_backend.execution.bindings import workflow_dependencies
from wenyi_core.observability import configure_logging

from ..adapters import create_context, postgres_repository
from ..config import Settings


def execute(request, options, send):
    configure_logging(service="wenyi-workflow")
    context = create_context(Settings(**options["settings"]))
    repository = postgres_repository(context)
    repository.start(migrate=False)
    try:
        with use_context(context):
            return runner.execute(request, workflow_dependencies(context), preclaimed=True)
    finally:
        repository.close()


if __name__ == "__main__":
    process.run_child(execute)
