# Publishing Python packages

The `Publish Python packages to PyPI` workflow
(`.github/workflows/publish-pypi.yml`) publishes **`wenyi-core` and `wenyi-cli`**.
The repository root is a virtual workspace, not a third release package. Web and
Desktop packages are not uploaded by this workflow.

Publishing a GitHub Release triggers the workflow. Pushing a tag, opening a PR,
and using **Run workflow** do not upload to PyPI. PR and manual runs only validate
builds and retain the distributions as Actions artifacts.

## One-time configuration

### GitHub environment

In [the Wenyi repository environments](https://github.com/BigDawnGhost/wenyi/settings/environments),
create **`pypi`** for Core and **`pypi-cli`** for CLI.

- No PyPI token or repository secret is required.
- For fully automatic release uploads, do not require a manual environment review.
  If required reviewers are configured, the upload waits for their approval.
- If deployment restrictions are enabled, allow the release **tags**, for example
  `v*`. Allowing only the `main` branch would block tag-based release jobs.

### PyPI Trusted Publishers

Create a PyPI account, enable two-factor authentication, and open
[account publishing](https://pypi.org/manage/account/publishing/).
For packages that do not yet exist, add **two pending publishers**:

| Field | Core | CLI |
| --- | --- | --- |
| PyPI project name | `wenyi-core` | `wenyi-cli` |
| Owner | `BigDawnGhost` | `BigDawnGhost` |
| Repository name | `wenyi` | `wenyi` |
| Workflow name | `publish-pypi.yml` | `publish-pypi.yml` |
| Environment name | `pypi` | `pypi-cli` |

Use the workflow **filename**, not its display name or full `.github/workflows/`
path. Both packages need their own publisher even though one workflow publishes
them. The environment names must differ: PyPI does not allow two pending projects
to share the same owner/repository/workflow/environment identity. Keep an already
registered Core publisher on `pypi`; use `pypi-cli` for the CLI publisher and its
GitHub environment. The first successful upload creates each project and converts
its pending publisher into a normal publisher. Pending publishers do not reserve names.
For a project you already own, configure the same fields under its
**Manage → Publishing** page instead.

The separately licensed bridge needs its own publisher in
`BigDawnGhost/wenyi-babeldoc-bridge`; see
[its publishing guide](https://github.com/BigDawnGhost/wenyi-babeldoc-bridge/blob/main/docs/publishing.md).
Do not add it as a dependency of the [GPLv3-licensed](../../LICENSE) CLI/Core packages.

## Releasing a version

1. Land the intended changes and the workflow on `main` through the normal PR
   process. Finish the applicable tests before releasing.
2. Choose a new tag at that verified commit, such as `v1.4.2` or `v1.4.2rc1`
   (examples, not a requirement to use these versions). CLI and Core versions
   come from Git tags; do not edit static version numbers or set SCM overrides.
3. Optionally open **Actions → Publish Python packages to PyPI → Run workflow**,
   enter the existing tag, and verify the build without uploading anything.
4. In **Releases → Draft a new release**, select that tag and publish the release.
   Saving a draft does not trigger an upload. A prerelease tag should also be
   marked as a GitHub prerelease.
5. Open the resulting Actions run. `build`, `publish-core`, and `publish-cli` must succeed.
   The existing CLI/Desktop binary workflows run separately.

The build job checks tag/checkout/version consistency, packaging contracts,
strict distribution metadata, and bundled language/prompt resources. It installs
the wheels and source distributions into separate clean Python 3.12 environments,
checks installed versions and dependency isolation, and runs CLI help/version/
language-list smoke tests. These checks do not call a model or translate a PDF.

Only the separate `publish-core` and `publish-cli` jobs receive `id-token: write`.
Each downloads the verified artifacts without checking out or executing
repository build code, then uses its package-specific environment identity to
upload exactly one package. CLI waits for Core to finish successfully. Fork
releases cannot use this repository's publish jobs.

## Verify and recover

After PyPI has indexed the release, use a fresh virtual environment:

```bash
uv venv .venv-pypi-verify --python 3.12
uv pip install --python .venv-pypi-verify/bin/python "wenyi-cli==1.4.2"
.venv-pypi-verify/bin/wenyi --version
.venv-pypi-verify/bin/wenyi translate --help
```

Replace `1.4.2` with the released version. On Windows, use `Scripts/python.exe`
and `Scripts/wenyi.exe` instead of `bin/python` and `bin/wenyi`.

- **Trusted Publisher rejected:** check all five fields, especially the
  environment and workflow filename. Configure both PyPI projects.
- **Pending publisher already registered for a different project name:** keep
  Core on `pypi` and register CLI with `pypi-cli`; create the matching GitHub
  environment too. Do not delete the valid Core registration.
- **Environment waiting:** approve the deployment or adjust its review/tag rules.
- **Tag/version mismatch:** create a correctly versioned new release; do not move
  an already published tag or use a version override to hide the mismatch.
- **Partial upload or network failure:** use **Re-run failed jobs**. The explicit
  PyPI check URL lets uv skip artifacts already present with matching hashes.
  Publication across packages is not atomic; Core may already exist if CLI fails.
- **Same filename, different contents:** PyPI does not allow replacement. Release
  a new version instead. Do not delete or rewrite published releases to bypass it.

Official references: [PyPI pending publishers](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [uv Trusted Publishing](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).
