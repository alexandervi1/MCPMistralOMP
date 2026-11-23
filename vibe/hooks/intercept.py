import json
import re
import sys

READ = re.compile(r"^\s*(cat|head|tail|less|more|type|Get-Content|gc)\s+\S", re.IGNORECASE)
IN_PLACE = re.compile(r"^\s*(sed\s+(-i|--in-place)|perl\s+\S*\s*-[A-Za-z]*i|awk\s+.*inplace)", re.IGNORECASE)
ECHO_WRITE = re.compile(r"^\s*(echo|printf)\s+.*?(?<![0-9&])>\s*(?!&|NUL|/dev/null)\S", re.IGNORECASE)
PS_WRITE = re.compile(r"^\s*(Set-Content|Add-Content|Out-File)\b", re.IGNORECASE)

SHELL_TOOLS = {"bash", "git_bash", "powershell", "process_start"}


def command_of(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd", "script", "input"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def deny(reason):
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    name = str(payload.get("tool_name") or "").split(".")[-1]
    if name not in SHELL_TOOLS:
        return 0

    cmd = command_of(payload.get("tool_input"))
    if not cmd:
        return 0

    if IN_PLACE.search(cmd):
        return deny("No edites archivos con sed/perl/awk -i: usa `edit`, que aplica solo el fragmento y muestra el diff.")

    if READ.search(cmd) and not re.search(r"[|><]|&&|;", cmd):
        return deny("Usa `read_file` en lugar de cat/head/tail/type/Get-Content: pagina por rangos (`archivo:1-120`) y no vuelca el archivo entero al contexto.")

    if ECHO_WRITE.search(cmd) or PS_WRITE.search(cmd):
        return deny("No escribas archivos por redirección: usa `write_file` para el archivo completo o `edit` para insertar. Los sinks nulos (NUL, $null, /dev/null) y `2>&1` no se bloquean.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
