"""Reescribe config.toml y hooks.toml para la plataforma y las rutas actuales.

Uso:
  python3 port_config.py --platform posix   --vibe-home /home/me/.vibe --python python3
  python  port_config.py --platform windows --vibe-home C:/Users/me/.vibe --python python
"""

import argparse
import os
import re


def fix_paths(text, vibe_home):
    home = vibe_home.replace("\\", "/")
    text = re.sub(r'\\?"[^"\n]*[\\/]mcp[\\/]ompx[\\/]server\.py\\?"',
                  f'"{home}/mcp/ompx/server.py"', text)
    text = re.sub(r'\\?"[^"\n]*[\\/]hooks[\\/]intercept\.py\\?"',
                  f'\\"{home}/hooks/intercept.py\\"', text)
    return text


def fix_browse(text, platform):
    blocks = re.split(r"(?m)^(?=\[)", text)
    out = []
    for block in blocks:
        if block.startswith("[[mcp_servers]]") and 'name = "browse"' in block:
            if platform == "posix":
                block = re.sub(r'(?m)^command = "[^"]*"', 'command = "npx"', block, count=1)
                block = re.sub(r"(?s)args = \[.*?\]",
                               'args = [\n    "-y",\n    "@playwright/mcp@latest",\n]',
                               block, count=1)
            else:
                block = re.sub(r'(?m)^command = "[^"]*"', 'command = "cmd"', block, count=1)
                block = re.sub(r"(?s)args = \[.*?\]",
                               'args = [\n    "/c",\n    "npx",\n    "-y",\n    "@playwright/mcp@latest",\n]',
                               block, count=1)
        if block.startswith("[[mcp_servers]]") and 'name = "ompx"' in block:
            wanted = "python3" if platform == "posix" else "python"
            block = re.sub(r'(?m)^command = "[^"]*"', f'command = "{wanted}"', block, count=1)
        out.append(block)
    return "".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["posix", "windows"], required=True)
    parser.add_argument("--vibe-home", required=True)
    parser.add_argument("--python", default="python3")
    args = parser.parse_args()

    config = os.path.join(args.vibe_home, "config.toml")
    hooks = os.path.join(args.vibe_home, "hooks.toml")

    for path in (config, hooks):
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        text = fix_paths(text, args.vibe_home)
        if path == config:
            text = fix_browse(text, args.platform)
        else:
            python = "python3" if args.platform == "posix" else "python"
            text = re.sub(r'(?m)^command = "python3? ', f'command = "{python} ', text)
            text = text.replace('command = "python "', f'command = "{python} "')
            text = text.replace('command = "python3 "', f'command = "{python} "')
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"port_config: {path} ({args.platform})")


if __name__ == "__main__":
    main()
