# uv

## project
uv init [name]                            # Create a new project (pyproject.toml, src layout)
uv add <package>                          # Add a dependency, update pyproject.toml + lockfile
uv add --dev <package>                    # Add a dev-only dependency
uv add --group <name> <package>           # Add to a custom dependency group
uv add --optional <extra> <package>       # Add to an optional extra
uv remove <package>                       # Remove a dependency
uv tree                                   # Show the project's dependency tree
uv version                                # Show the project's (or uv's own) version

## lock & sync
uv lock                                   # Create or update uv.lock
uv lock --upgrade                         # Upgrade every locked package
uv lock --upgrade-package <pkg>           # Upgrade a single locked package
uv sync                                   # Install the environment to match uv.lock
uv sync --frozen                          # Sync without updating the lockfile (CI)
uv sync --locked                          # Fail instead of syncing if the lockfile is stale
uv sync --no-dev                          # Sync without dev dependencies
uv sync --all-extras                      # Sync including all optional extras

## run
uv run <command>                          # Run a command inside the project's environment
uv run python script.py                   # Run a script (auto-syncs the env first)
uv run --with <package> script.py         # Run with an extra one-off dependency
uv run --python 3.12 script.py            # Run using a specific Python version

## tool
uvx <tool>                                # Run a tool once in a throwaway env (= uv tool run)
uvx --from <package> <tool>               # Run a tool whose CLI name differs from the package
uv tool install <tool>                    # Install a tool onto your PATH for reuse
uv tool uninstall <tool>                  # Uninstall a tool
uv tool list                              # List installed tools
uv tool upgrade <tool>                    # Upgrade one installed tool
uv tool upgrade --all                     # Upgrade every installed tool

## python
uv python install 3.12                    # Install a Python version
uv python install 3.11 3.12               # Install several versions at once
uv python list                            # List installed and available versions
uv python pin 3.12                        # Pin this project to a version (.python-version)
uv python pin --global 3.12               # Set the default Python version for new projects
uv python find ">=3.11"                   # Find an interpreter matching a version constraint

## venv & pip
uv venv                                   # Create a .venv in the current directory
uv venv my-env                            # Create a virtual environment with a custom name
uv pip install <package>                  # pip-compatible install into the active env
uv pip list                               # pip-compatible list of installed packages
uv pip compile requirements.in            # Compile a locked requirements.txt

## build & publish
uv build                                  # Build sdist + wheel into dist/
uv publish                                # Publish built distributions to PyPI

## misc
uv self update                            # Update uv itself to the latest version
uv cache clean                            # Clear uv's package cache
uv cache dir                              # Show the cache directory path
