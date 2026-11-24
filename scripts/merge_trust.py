"""Anade una ruta a trusted_folders.toml de Vibe sin duplicar ni perder entradas.

Uso: python merge_trust.py <ruta> [<ruta> ...]
"""

import os
import re
import sys

VIBE_HOME = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
TARGET = os.path.join(VIBE_HOME, "trusted_folders.toml")


def load():
    if not os.path.isfile(TARGET):
        return [], []
    text = open(TARGET, "r", encoding="utf-8").read()
    out = []
    for name in ("trusted", "untrusted"):
        match = re.search(rf"\b{name}\s*=\s*\[(.*?)\]", text, re.S)
        values = []
        if match:
            for raw in re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1)):
                values.append(raw.replace("\\\\", "\\"))
        out.append(values)
    return out[0], out[1]


def dump(trusted, untrusted):
    def block(name, values):
        if not values:
            return f"{name} = []\n"
        lines = [f"{name} = ["]
        for value in values:
            lines.append('    "%s",' % value.replace("\\", "\\\\"))
        lines.append("]")
        return "\n".join(lines) + "\n"

    return block("trusted", trusted) + block("untrusted", untrusted)


def normalize(path):
    return os.path.normpath(os.path.abspath(path))


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    trusted, untrusted = load()
    seen = {os.path.normcase(normalize(p)) for p in trusted}
    added = []
    for path in argv:
        norm = normalize(path)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        if not os.path.isdir(norm):
            print(f"AVISO: no existe el directorio: {norm}")
            continue
        trusted.append(norm)
        seen.add(key)
        added.append(norm)
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump(trusted, untrusted))
    print(f"trusted_folders.toml: {len(trusted)} entrada(s)")
    for path in added:
        print(f"  + {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
