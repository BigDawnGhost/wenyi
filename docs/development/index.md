# Develop and maintain Wenyi

This section is for contributors and maintainers. To install a release or translate
a book, use the user guides rather than the source-development setup.

## Find the owning layer

| Task | Documentation |
| --- | --- |
| Understand packages, dependencies and storage ports | [Architecture](architecture.md) |
| Change checkpoints, recovery, language policies or prompts | [Pipeline contracts](pipeline.md) |
| Run or change the API, workers and Web host | [Web development](web.md) |
| Run or change the native app and local engine | [Desktop development](desktop.md) |
| Change shared UI, routes, loading or interaction conventions | [Frontend conventions](frontend.md) |
| Build and release Desktop installers and updates | [Desktop releases](releasing.md) |
| Publish CLI and Core to PyPI | [Python package publishing](publishing.md) |

Start source work with the [contribution guide](../../CONTRIBUTING.md), which owns
branch/review policy, CLI source setup and Python regression-test commands.
The [repository guide](../../AGENTS.md) records safety constraints and task entry points
for coding agents. Use fake clients and temporary data for local validation; do not
run tests against production projects or services.

## Documentation layout

```text
docs/
  index.md          # Documentation site home
  guide/            # Installation, use and deployment
  reference/        # Configuration and format lookup
  development/      # Architecture, development, tests and releases
  zh/               # The same topics in Simplified Chinese
  images/           # Shared documentation assets
  .vitepress/       # Site configuration
```

Describe current behavior at its owning topic. Update the relevant section in place
and remove superseded text; do not append UI-movement reports or bugfix diaries.
Shared Desktop/Web workflows belong in `guide/workspace.md`, not two parallel manuals.
Keep English and Chinese versions aligned and link to the authoritative explanation
instead of copying it. Completed design discussions and test-run evidence belong in
Git/PR/release history; retain only enduring decisions in the relevant development topic.
The full maintenance rules are in [AGENTS.md](../../AGENTS.md#文档维护).

## Documentation preview

With Node 22 and pnpm 9, run from the repository root:

```bash
pnpm install --frozen-lockfile
pnpm docs:dev
```

Open `http://127.0.0.1:5175/wenyi/` for English or
`http://127.0.0.1:5175/wenyi/zh/` for Simplified Chinese.
To validate the static site:

```bash
pnpm docs:build
pnpm docs:preview
```

The static preview uses port `4175`. Both servers bind only to `127.0.0.1`;
restart the static preview after rebuilding. These commands do not publish the site.
Repository-only files and source-code links open on GitHub. Mermaid diagrams remain
code blocks.

After moving pages, update navigation, repository entry points, package documentation
links and source comments as well as Markdown links. Check heading anchors, both
languages, search results and `git diff --check`; a successful build alone does not
prove every link reaches the intended section.

## GitHub Pages deployment

The documentation site is published at
[bigdawnghost.github.io/wenyi](https://bigdawnghost.github.io/wenyi/);
the language menu switches between English and Simplified Chinese. Keep the
VitePress `base` set to `/wenyi/` for this project-site address.

The [Documentation workflow](../../.github/workflows/docs.yml) builds with Node 22,
pnpm 9, the frozen lockfile and `pnpm docs:build`. Pull requests validate the site
without publishing. Relevant changes merged into `main` build and deploy
automatically; maintainers can also run **Actions → Documentation → Run workflow**
with `main` selected.

In repository **Settings → Pages**, set **Build and deployment → Source** to
**GitHub Actions**. The deployment uses the `github-pages` environment, which should
allow only `main`; retain any configured approval requirements. Only the deployment
job receives `pages: write` and `id-token: write`. No personal token or extra secret
is needed, and in-progress production deployments are not canceled by newer pushes.

Only `docs/.vitepress/dist/` is uploaded. Never upload the repository root, local
preview caches, `.env` files or application workspaces. This publishes static
documentation, not the Wenyi Web application or API. After deployment, verify both
homepages, a nested documentation URL, assets, search and the language menu on the
public site. Diagnose failures in the workflow and Pages settings rather than
committing generated output or bypassing deployment protections.
