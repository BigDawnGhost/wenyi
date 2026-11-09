"""Absolute import entry for bundled CLI executables."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from wenyi_cli.main import main

    main()
