# vendor/

Escape hatch for commands that need a vendored pure-Python package that
cannot go through the normal `required_packages` / dependency resolver
pipeline (e.g., forked or patched libraries).

## Convention

```
vendor/<command_name>/<package_name>/
```

The command is responsible for inserting the path into `sys.path` itself
before importing the vendored package.  Vendored packages are **not**
declared in `required_packages` and are **not** subject to dependency
resolution.

## When to use

- You need a patched fork that isn't on PyPI.
- The package is pure Python and tiny enough to check in.
- You want to avoid version conflicts with base requirements.

For everything else, use `required_packages` on your command class and
let `install_command.py` handle resolution and installation.
