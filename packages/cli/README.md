# Wenyi CLI

Local command-line application for multilingual long-form translation, powered
by `wenyi-core`. It supports terminology management, contextual translation,
polishing, whole-book review, resumable workflows, and multiple document formats.

Install into an activated Python 3.10+ virtual environment:

```bash
uv pip install wenyi-cli
wenyi --version
wenyi --help
```

The executable is `wenyi`; the module entry point is `python -m wenyi_cli`.
The translation engine is installed automatically. API keys are read from
environment variables; do not put credentials in configuration files.

PDF layout preservation additionally requires an independently installed
`wenyi-babeldoc-bridge` HTTP service. This package does not install BabelDOC,
the Web API, or the Desktop application.

- [CLI guide](https://github.com/BigDawnGhost/wenyi/blob/main/docs/cli.md)
- [Configuration](https://github.com/BigDawnGhost/wenyi/blob/main/docs/configuration.md)
- [Source and license](https://github.com/BigDawnGhost/wenyi)
- [简体中文](https://github.com/BigDawnGhost/wenyi/blob/main/docs/zh/cli.md)
