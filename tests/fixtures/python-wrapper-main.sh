# Baseline from main 106877f4; retain its generated body and local execution contract.
write_python_wrapper() {
    "$PYTHON" - "$BIN/$1" "$SHARE" "$CFG_DIR" "$CACHE_DIR" \
        "$RELEASE" "$2" <<'PY'
import pathlib, shlex, sys
destination = pathlib.Path(sys.argv[1])
share = pathlib.Path(sys.argv[2])
config = pathlib.Path(sys.argv[3])
cache = pathlib.Path(sys.argv[4])
fallback_release = pathlib.Path(sys.argv[5])
relative = sys.argv[6]
temporary = destination.with_name(f".{destination.name}.next")
temporary.write_text(
    "#!/bin/sh\n"
    "# managed-by-clarp\n"
    f"export CLARP_SHARE_DIR={shlex.quote(str(share))}\n"
    f"export CLARP_CONFIG_DIR={shlex.quote(str(config))}\n"
    f"export CLARP_CACHE_DIR={shlex.quote(str(cache))}\n"
    f"export CLAUDE_PWA_CONFIG={shlex.quote(str(config / 'config.toml'))}\n"
    f"export CLAUDE_PWA_DB={shlex.quote(str(share / 'state.sqlite'))}\n"
    f"active={shlex.quote(str(share / 'current'))}\n"
    f"fallback={shlex.quote(str(fallback_release))}\n"
    f"relative={shlex.quote(relative)}\n"
    'if [ -r "$active/SERVICE_PYTHON" ] && [ -f "$active/$relative" ]; then\n'
    '  code_root=$active\n'
    'else\n'
    '  code_root=$fallback\n'
    'fi\n'
    'export CLARP_CODE_ROOT="$code_root"\n'
    'runtime=$(sed -n \'1p\' "$code_root/SERVICE_PYTHON")\n'
    'script="$code_root/$relative"\n'
    'exec "$runtime" "$script" "$@"\n'
)
temporary.chmod(0o700)
temporary.replace(destination)
PY
}
