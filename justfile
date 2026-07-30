# NixOS has no FHS /usr/lib, but camoufox's downloaded Firefox binary (and
# greenlet's compiled wheel) expect one (libgtk-3, libstdc++, libnss3, ...).
# steam-run provides that FHS sandbox.

run *args:
    NIXPKGS_ALLOW_UNFREE=1 nix-shell -p steam-run --run "TMPDIR=/tmp steam-run env TMPDIR=/tmp uv run {{args}}" --impure

setup:
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    just run camoufox fetch
