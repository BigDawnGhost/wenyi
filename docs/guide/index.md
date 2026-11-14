# Getting started with Wenyi

Wenyi translates books and long-form writing using your chosen model service.
Start with the interface that fits your work; each interface keeps its own project
state rather than sharing a live project database.

## Choose an interface

| Interface | Start here | Best suited to |
| --- | --- | --- |
| Desktop | [Install and set up](desktop.md) | Local projects, native file selection and an integrated proofreading workspace |
| Web | [Deploy a browser workspace](web.md) | A self-hosted service backed by PostgreSQL and Redis |
| CLI | [Install and translate](cli.md) | Terminal workflows and automation without a server |

Desktop installers include the translation engine and Python runtime. You do not
need the source-development environment to use a release.

## Translate and revise a book

For Desktop and Web, the [workspace guide](workspace.md) follows a project from
import and model selection through translation, proofreading, terminology, Review
and export. Platform-specific installation, credentials and backups stay in the
Desktop and Web guides.

Read the [translation pipeline](pipeline.md) to understand preparation, context,
three-draft translation, Review and resumable progress. These choices affect both
quality and cost; model output still needs human review.

## Look up a setting or format

- [Configuration reference](../reference/configuration.md): providers, model routes,
  pipeline options and defaults.
- [Format reference](../reference/formats.md): supported inputs and outputs, PDF
  backends, layout limitations and bilingual exports.

Translation sends book text to your configured model provider unless you use a local
model. PDF parsing can also use an external service. Check the provider's privacy
terms and your rights to the text before sending a book.

Developing or packaging Wenyi? Use the separate [development documentation](../development/index.md).
