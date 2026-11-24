#!/usr/bin/env python
"""ompx — MCP stdio que devuelve a Vibe las capacidades que no trae.

Tools: glob (git ls-files), grep, read (rangos + guarda de secretos),
eval_python (kernel persistente), lsp_query (pyright / typescript-language-server).

Sin dependencias: JSON-RPC 2.0 delimitado por lineas sobre stdin/stdout.
"""

import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsp_client  # noqa: E402  (modulo hermano, mismo directorio)

SERVER = {"name": "ompx", "version": "1.0.0"}
MAX_BYTES = 120_000
DEBUG = bool(os.environ.get("OMPX_DEBUG"))
TRACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace")


def dbg(message):
    if not DEBUG and not os.path.exists(TRACE_PATH):
        return
    try:
        with open(os.path.join(os.path.dirname(TRACE_PATH), "ompx-trace.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} {message}\n")
    except OSError:
        pass


SENSITIVE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(^|[\\/])\.env(\..*)?$",
        r"(^|[\\/])\.envrc(\..*)?$",
        r"[\\/]secrets[\\/]",
        r"\.pem$",
        r"\.key$",
        r"[\\/]\.ssh[\\/]",
    )
]

KERNEL_SRC = r'''
import sys, json, io, traceback, contextlib
ns = {"__name__": "__main__"}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(req.get("code", ""), "<eval>", "exec"), ns)
        res = {"ok": True, "out": buf.getvalue()}
    except BaseException:
        res = {"ok": False, "out": buf.getvalue() + traceback.format_exc()}
    sys.__stdout__.write(json.dumps(res) + "\n")
    sys.__stdout__.flush()
'''

JS_KERNEL = r'''
const vm = require('vm');
const readline = require('readline');
const ctx = vm.createContext({require, process, Buffer, console, setTimeout, clearTimeout, URL, TextEncoder, TextDecoder, fetch, structuredClone});
const rl = readline.createInterface({input: process.stdin});
rl.on('line', async (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch (e) { return; }
  const logs = [];
  const cap = (...a) => logs.push(a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '));
  ctx.console = {log: cap, info: cap, warn: cap, error: cap, debug: cap};
  let ok = true;
  let err = '';
  try {
    try {
      const script = new vm.Script(req.code, {filename: '<eval>'});
      const value = script.runInContext(ctx);
      if (value && typeof value.then === 'function') await value;
    } catch (inner) {
      if (inner instanceof SyntaxError && String(inner.message).includes('await')) {
        await vm.runInContext('(async () => {' + req.code + '\n})()', ctx, {filename: '<eval>'});
      } else {
        throw inner;
      }
    }
  } catch (e) {
    ok = false;
    err = (e && e.stack) ? e.stack : String(e);
  }
  process.stdout.write(JSON.stringify({ok, out: logs.join('\n') + (err ? '\n' + err : '')}) + '\n');
});
'''

TOOLS = [
    {
        "name": "glob",
        "description": "Lista rutas que casan con un patron glob, respetando .gitignore. Sustituye a find/fd/Get-ChildItem.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob, p.ej. '**/*.ts' o 'src/**/*.tsx'."},
                "path": {"type": "string", "description": "Directorio base o archivo. Por defecto el cwd."},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Busca una regex en archivos, respetando .gitignore. Devuelve 'ruta:linea: texto' con paginacion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex de Python."},
                "path": {"type": "string"},
                "glob": {"type": "string", "description": "Filtro de archivos, p.ej. '*.py'."},
                "case_insensitive": {"type": "boolean", "default": False},
                "max_matches": {"type": "integer", "default": 100},
                "skip": {"type": "integer", "default": 0, "description": "Coincidencias a saltar (paginacion)."},
                "context": {"type": "integer", "default": 0, "description": "Lineas de contexto antes y despues (0-10)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read",
        "description": "Lee un archivo de texto por rangos de linea, numerados. Rechaza .env, secrets, *.pem, *.key y .ssh.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 1, "description": "Primera linea (1-based)."},
                "limit": {"type": "integer", "default": 150},
                "member": {"type": "string", "description": "Solo .zip: entrada a leer. Sin member, lista el contenido."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "eval_python",
        "description": "Ejecuta Python en un kernel persistente: variables e imports sobreviven entre llamadas. Devuelve stdout y stderr.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {"type": "number", "default": 30},
                "restart": {"type": "boolean", "default": False, "description": "Reinicia el kernel antes de ejecutar."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "lsp_query",
        "description": "Consulta el language server del archivo (.py via pyright, .ts/.js via typescript-language-server, .php via intelephense): definiciones, referencias, hover, diagnosticos, simbolos, rename, code actions, formato y simbolos del workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definitions", "type_definition", "implementation", "references", "hover",
                             "diagnostics", "symbols", "workspace_symbols", "rename", "code_actions", "format"],
                },
                "path": {"type": "string"},
                "line": {"type": "integer", "default": 1, "description": "Linea 1-based."},
                "column": {"type": "integer", "default": 1, "description": "Columna 1-based."},
                "new_name": {"type": "string", "description": "rename: nombre nuevo."},
                "query": {"type": "string", "description": "workspace_symbols: texto a buscar."},
                "apply": {"type": "boolean", "default": False, "description": "rename/format/code_actions: escribir cambios."},
                "apply_index": {"type": "integer", "description": "code_actions: indice de la accion a aplicar."},
                "tab_size": {"type": "integer", "default": 2},
                "timeout": {"type": "number", "default": 20},
            },
            "required": ["action", "path"],
        },
    },
    {
        "name": "eval_js",
        "description": "Ejecuta JavaScript en un kernel Node persistente: el estado en `globalThis` y las variables `var` sobreviven entre llamadas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {"type": "number", "default": 30},
                "restart": {"type": "boolean", "default": False},
            },
            "required": ["code"],
        },
    },
    {
        "name": "ast_grep",
        "description": "Busca por estructura con ast-grep (no por texto): patrones de codigo con metavariables $A. Devuelve archivo:linea y el nodo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Patron ast-grep, p.ej. 'console.log($ARG)' o 'function $F($$$) { $$$ }'."},
                "lang": {"type": "string", "description": "Lenguaje (ts, tsx, js, py, php, ...). Si se omite, se infiere por extension."},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "limit": {"type": "integer", "default": 80},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "ast_replace",
        "description": "Reescribe por estructura con ast-grep. Por defecto solo muestra el diff; con apply=true escribe los archivos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "rewrite": {"type": "string", "description": "Reescritura con las mismas metavariables, p.ej. 'logger.info($ARG)'."},
                "lang": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["pattern", "rewrite"],
        },
    },
    {
        "name": "sessions",
        "description": "Busca texto en el historial de sesiones guardadas de Vibe (~/.vibe/logs/session). Devuelve sesion, fecha y fragmento.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sessions_get",
        "description": "Lee una sesion guardada por su id y devuelve los ultimos fragmentos de texto (prompts y respuestas).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "apply_patch",
        "description": "Aplica un diff unificado con git apply. Por defecto solo comprueba (--check); con apply=true escribe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "Diff unificado completo (---/+++/@@)."},
                "path": {"type": "string", "description": "Raiz del repo. Por defecto el cwd."},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["diff"],
        },
    },
    {
        "name": "ompx_info",
        "description": "Introspeccion: version, rutas de python/node/ast-grep, language servers disponibles y lista de tools.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_kernels = {"python": {"proc": None}, "js": {"proc": None}}
_kernel_lock = threading.Lock()
_send_lock = threading.Lock()


def send(obj):
    payload = json.dumps(obj, ensure_ascii=True)
    dbg(f"send id={obj.get('id')} bytes={len(payload)}")
    with _send_lock:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def run_git(args, cwd):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None


SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", "dist", "build", "htmlcov", ".idea", ".vscode", "target",
    ".next", ".nuxt", ".cache", "coverage",
}


def walk_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace("\\", "/"))
    return out


def iter_files(root):
    """Archivos del arbol: git ls-files si es repo (gitignore exacto), si no os.walk."""
    proc = run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    if proc is not None and proc.returncode == 0:
        files = [f for f in proc.stdout.decode("utf-8", "replace").split("\0") if f]
        if files:
            return files
    return walk_files(root)


def matches(rel, pattern):
    rel = rel.replace("\\", "/")
    if fnmatch.fnmatch(rel, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
        return True
    return False


def base_dir(path):
    return os.path.abspath(path) if path else os.getcwd()


def split_root(path):
    """Devuelve (directorio, [basename]) si path es un archivo, o (directorio, None)."""
    target = base_dir(path)
    if os.path.isfile(target):
        return os.path.dirname(target), [os.path.basename(target)]
    return target, None


def tool_glob(args):
    root, single = split_root(args.get("path"))
    limit = max(1, int(args.get("limit", 200)))
    names = single if single else iter_files(root)
    files = sorted(rel for rel in names if matches(rel, args["pattern"]))
    if not files:
        return text_result("(sin coincidencias)")
    shown = files[:limit]
    note = "" if len(files) <= limit else f"\n[... {len(files) - limit} rutas mas]"
    return text_result("\n".join(shown) + note)


def tool_grep(args):
    root, single = split_root(args.get("path"))
    flags = re.IGNORECASE if args.get("case_insensitive") else 0
    rx = re.compile(args["pattern"], flags)
    file_glob = args.get("glob")
    skip = max(0, int(args.get("skip", 0)))
    limit = max(1, int(args.get("max_matches", 100)))
    context = max(0, min(10, int(args.get("context", 0))))
    hits, found, accepted, more = [], 0, 0, False
    for rel in (single if single else iter_files(root)):
        if file_glob and not matches(rel, file_glob):
            continue
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > 2_000_000:
                continue
            with open(full, "rb") as probe:
                if b"\0" in probe.read(8192):
                    continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.readlines()
        except (OSError, ValueError):
            continue
        last_emitted = -1
        for index, line in enumerate(content):
            if not rx.search(line):
                continue
            if found < skip:
                found += 1
                continue
            if accepted >= limit:
                more = True
                break
            if context:
                first = max(0, index - context)
                last = min(len(content), index + context + 1)
                for offset in range(first, last):
                    if offset <= last_emitted:
                        continue
                    last_emitted = offset
                    mark = ":" if offset == index else "-"
                    hits.append(f"{rel}{mark}{offset + 1}{mark} {content[offset].rstrip()[:400]}")
            else:
                hits.append(f"{rel}:{index + 1}: {line.rstrip()[:400]}")
            accepted += 1
            found += 1
        if more:
            break
    if not hits:
        return text_result("(sin coincidencias)")
    tail = f"\n[... mas; usa skip={skip + limit}]" if more else ""
    return text_result("\n".join(hits) + tail)


def strip_html(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def read_url(url):
    import ssl

    note = ""
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl._create_unverified_context()
        note = "[TLS sin verificar: instala certifi]\n"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ompx)"})
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            ctype = response.headers.get("Content-Type", "")
            data = response.read(MAX_BYTES * 2)
    except Exception as exc:
        return text_result(f"No pude descargar {url}: {exc}", True)
    body = data.decode("utf-8", "replace")
    if "html" in ctype.lower():
        body = strip_html(body)
    return text_result(f"# {url}\n[{ctype}]\n{note}\n" + body[:MAX_BYTES])


def read_pdf(path, offset, limit):
    try:
        from pypdf import PdfReader
    except Exception:
        return text_result("PDF: falta la dependencia. Instala con: python -m pip install pypdf", True)
    try:
        reader = PdfReader(path)
    except Exception as exc:
        return text_result(f"PDF ilegible: {exc}", True)
    lines = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            content = page.extract_text() or ""
        except Exception:
            content = ""
        for line in content.splitlines():
            lines.append(f"p{number}\t{line}")
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return text_result(f"(vacio desde la linea {offset}; {len(lines)} lineas extraidas)")
    return text_result(f"{len(lines)} lineas extraidas\n" + "\n".join(chunk)[:MAX_BYTES])


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _docx_lines(xml):
    root = ElementTree.fromstring(xml)
    out = []
    for para in root.iter():
        if _local(para.tag) != "p":
            continue
        text = "".join(node.text or "" for node in para.iter() if _local(node.tag) == "t")
        out.append(text)
    return out


def _pptx_lines(xml):
    root = ElementTree.fromstring(xml)
    out = []
    for para in root.iter():
        if _local(para.tag) != "p":
            continue
        text = "".join(node.text or "" for node in para.iter() if _local(node.tag) == "t")
        if text.strip():
            out.append(text)
    return out


def _xlsx_lines(xml, shared):
    root = ElementTree.fromstring(xml)
    out = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells = []
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            ref = cell.get("r", "")
            ctype = cell.get("t")
            value = ""
            for node in cell.iter():
                tag = _local(node.tag)
                if tag == "v" and node.text is not None:
                    value = node.text
                    if ctype == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                elif tag == "t" and ctype == "inlineStr" and node.text:
                    value += node.text
            if value:
                cells.append(f"{ref}={value}")
        if cells:
            out.append(" | ".join(cells))
    return out


def read_office(path, offset, limit):
    import zipfile

    ext = path.lower().rsplit(".", 1)[-1]
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if ext == "docx":
                lines = _docx_lines(archive.read("word/document.xml"))
            elif ext == "pptx":
                slides = sorted(name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name))
                lines = []
                for name in slides:
                    lines.append(f"--- {name.rsplit('/', 1)[-1]} ---")
                    lines.extend(_pptx_lines(archive.read(name)))
            else:
                shared = []
                if "xl/sharedStrings.xml" in names:
                    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if _local(item.tag) == "si":
                            shared.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
                sheets = sorted(name for name in names if re.match(r"xl/worksheets/sheet\d+\.xml$", name))
                lines = []
                for name in sheets:
                    lines.append(f"--- {name.rsplit('/', 1)[-1]} ---")
                    lines.extend(_xlsx_lines(archive.read(name), shared))
    except Exception as exc:
        return text_result(f"Office ilegible: {exc}", True)
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return text_result(f"(vacio desde la linea {offset}; {len(lines)} lineas)")
    return text_result(f"{len(lines)} lineas extraidas\n" + "\n".join(chunk)[:MAX_BYTES])


def read_zip(path, member, offset, limit):
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            if not member:
                entries = [f"{item.filename}\t{item.file_size}" for item in archive.infolist()]
                return text_result(f"{len(entries)} entrada(s)\n" + "\n".join(entries[:200]))
            data = archive.read(member)
    except Exception as exc:
        return text_result(f"ZIP ilegible: {exc}", True)
    lines = data.decode("utf-8", "replace").splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    return text_result(f"{member} ({len(lines)} lineas)\n" + "\n".join(chunk)[:MAX_BYTES])


def tool_read(args):
    raw = str(args.get("path") or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        return read_url(raw)
    path = os.path.abspath(raw)
    offset = max(1, int(args.get("offset", 1)))
    limit = max(1, int(args.get("limit", 150)))
    if path.lower().endswith(".pdf"):
        return read_pdf(path, offset, limit)
    lowered = path.lower()
    if lowered.endswith((".docx", ".pptx", ".xlsx")):
        return read_office(path, offset, limit)
    if lowered.endswith(".zip"):
        return read_zip(path, args.get("member"), offset, limit)
    if any(rx.search(path) for rx in SENSITIVE):
        return text_result("Archivo sensible: lo bloqueo en el servidor. Pide autorizacion explicita al usuario.", True)
    if not os.path.isfile(path):
        return text_result(f"No existe o no es archivo: {path}", True)
    out, total_bytes = [], 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, start=1):
            if number < offset:
                continue
            if number >= offset + limit:
                break
            chunk = f"{number}\t{line.rstrip()}"
            total_bytes += len(chunk)
            if total_bytes > MAX_BYTES:
                out.append("[... corte por tamano]")
                break
            out.append(chunk)
    if not out:
        return text_result(f"(vacio desde la linea {offset})")
    return text_result("\n".join(out))


NODE = shutil.which("node") or "node"

KERNELS = {
    "python": lambda: [sys.executable, "-u", "-c", KERNEL_SRC],
    "js": lambda: [NODE, "-e", JS_KERNEL],
}


def kernel_start(kind):
    proc = subprocess.Popen(
        KERNELS[kind](),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _kernels[kind]["proc"] = proc
    return proc


def kernel_stop(kind):
    proc = _kernels[kind].get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    _kernels[kind]["proc"] = None


def tool_eval(args, kind="python"):
    with _kernel_lock:
        return _eval_locked(args, kind)


def _eval_locked(args, kind):
    if args.get("restart"):
        kernel_stop(kind)
    proc = _kernels[kind].get("proc")
    if proc is None or proc.poll() is not None:
        proc = kernel_start(kind)
    timeout = float(args.get("timeout", 30))
    box = {}

    def reader():
        box["line"] = proc.stdout.readline()

    try:
        proc.stdin.write(json.dumps({"code": args["code"]}) + "\n")
        proc.stdin.flush()
    except Exception as exc:
        kernel_stop(kind)
        return text_result(f"Kernel no disponible: {exc}. Reintenta.", True)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        kernel_stop(kind)
        return text_result(f"Timeout tras {timeout:g}s: kernel reiniciado.", True)

    line = box.get("line", "")
    if not line:
        kernel_stop(kind)
        return text_result("El kernel murio sin respuesta. Reiniciado.", True)
    try:
        res = json.loads(line)
    except Exception:
        return text_result(f"Respuesta ilegible del kernel: {line[:300]}", True)
    body = res.get("out", "")
    return text_result(body if body.strip() else "(sin salida)", not res.get("ok", False))


SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
SYMBOL_KIND = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
    12: "function", 13: "variable", 14: "constant", 15: "string", 16: "number",
    17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null",
    22: "enum-member", 23: "struct", 24: "event", 25: "operator", 26: "type-parameter",
}


def short_path(path):
    try:
        rel = os.path.relpath(path)
        return path if rel.startswith("..") else rel
    except ValueError:
        return path


def fmt_location(loc):
    uri = loc.get("uri") or loc.get("targetUri") or ""
    rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
    start = rng.get("start") or {}
    return f"{short_path(lsp_client.uri_to_path(uri))}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1}"


def flatten_symbols(items, depth=0):
    out = []
    for item in items or []:
        kind = SYMBOL_KIND.get(item.get("kind"), "?")
        start = (item.get("range") or {}).get("start") or (item.get("location") or {}).get("range", {}).get("start") or {}
        out.append(f"{'  ' * depth}{kind} {item.get('name', '?')}  [{start.get('line', 0) + 1}:{start.get('character', 0) + 1}]")
        out.extend(flatten_symbols(item.get("children"), depth + 1))
    return out


def collect_workspace_edits(edit):
    """Normaliza un WorkspaceEdit a {uri: [TextEdit]}."""
    out = {}
    if not isinstance(edit, dict):
        return out
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            out.setdefault(uri, []).extend(edits or [])
    for change in edit.get("documentChanges") or []:
        uri = (change.get("textDocument") or {}).get("uri")
        if uri and change.get("edits"):
            out.setdefault(uri, []).extend(change["edits"])
    return out


def offset_of(text, pos):
    lines = text.splitlines(keepends=True)
    line = max(0, int(pos.get("line", 0)))
    if line >= len(lines):
        return len(text)
    base = sum(len(item) for item in lines[:line])
    body = lines[line].rstrip("\r\n")
    return base + min(max(0, int(pos.get("character", 0))), len(body))


def apply_text_edits(path, edits):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    ordered = sorted(edits, key=lambda e: (
        (e.get("range") or {}).get("start", {}).get("line", 0),
        (e.get("range") or {}).get("start", {}).get("character", 0),
    ), reverse=True)
    for edit in ordered:
        rng = edit.get("range") or {}
        start = offset_of(text, rng.get("start") or {})
        end = offset_of(text, rng.get("end") or {})
        if end < start:
            start, end = end, start
        text = text[:start] + (edit.get("newText") or "") + text[end:]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def tool_lsp(args):
    action = args.get("action")
    path = os.path.abspath(args.get("path") or "")
    if not os.path.isfile(path):
        return text_result(f"No existe o no es archivo: {path}", True)
    language = lsp_client.LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not language:
        ext = os.path.splitext(path)[1] or "(sin extension)"
        return text_result(f"Sin language server para {ext}. Soportado: .py .php .ts .tsx .js .jsx", True)

    timeout = float(args.get("timeout", 20))
    position = {
        "line": max(0, int(args.get("line", 1)) - 1),
        "character": max(0, int(args.get("column", 1)) - 1),
    }

    try:
        dbg(f"lsp start action={action} path={path}")
        client = lsp_client.get_client(path, language)
        dbg(f"client ready label={client.label} root={client.root}")
        uri = client.open_document(path)
        dbg("document opened")

        if action == "diagnostics":
            items = client.wait_diagnostics(uri, timeout)
            if not items:
                return text_result(f"(sin diagnosticos) {short_path(path)}")
            out = []
            for diag in items:
                start = (diag.get("range") or {}).get("start") or {}
                level = SEVERITY.get(diag.get("severity"), "?")
                out.append(f"{short_path(path)}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1} {level}: {diag.get('message', '')[:400]}")
            return text_result(f"{len(items)} diagnostico(s)\n" + "\n".join(out))

        if action == "hover":
            result = client.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": position}, timeout)
            if not result:
                return text_result("(sin hover)")
            contents = result.get("contents")
            if isinstance(contents, dict):
                body = contents.get("value", "")
            elif isinstance(contents, list):
                body = "\n".join(c if isinstance(c, str) else c.get("value", "") for c in contents)
            else:
                body = str(contents)
            return text_result(body[:4000])

        if action in ("definitions", "type_definition", "implementation", "references"):
            if action == "references":
                method, params = "textDocument/references", {
                    "textDocument": {"uri": uri},
                    "position": position,
                    "context": {"includeDeclaration": True},
                }
            else:
                method = {
                    "definitions": "textDocument/definition",
                    "type_definition": "textDocument/typeDefinition",
                    "implementation": "textDocument/implementation",
                }[action]
                params = {"textDocument": {"uri": uri}, "position": position}
            result = client.request(method, params, timeout)
            if not result:
                return text_result("(sin resultados)")
            if isinstance(result, dict):
                result = [result]
            lines = [fmt_location(loc) for loc in result]
            return text_result(f"{len(lines)} resultado(s)\n" + "\n".join(lines))

        if action == "symbols":
            result = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, timeout)
            lines = flatten_symbols(result)
            return text_result("\n".join(lines) if lines else "(sin simbolos)")

        if action == "workspace_symbols":
            query = args.get("query") or ""
            result = client.request("workspace/symbol", {"query": query}, timeout) or []
            if not result:
                return text_result("(sin simbolos)")
            lines = []
            for item in result[:60]:
                kind = SYMBOL_KIND.get(item.get("kind"), "?")
                lines.append(f"{kind} {item.get('name', '?')}  {fmt_location(item.get('location') or {})}")
            return text_result(f"{len(result)} resultado(s)\n" + "\n".join(lines))

        if action == "rename":
            new_name = args.get("new_name")
            if not new_name:
                return text_result("rename requiere new_name.", True)
            result = client.request("textDocument/rename",
                                    {"textDocument": {"uri": uri}, "position": position, "newName": new_name}, timeout)
            edits_by_file = collect_workspace_edits(result)
            if not edits_by_file:
                return text_result("(sin cambios)")
            lines, total = [], 0
            for file_uri, edits in edits_by_file.items():
                target = lsp_client.uri_to_path(file_uri)
                for edit in edits:
                    start = (edit.get("range") or {}).get("start") or {}
                    lines.append(f"{short_path(target)}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1} -> {str(edit.get('newText', ''))[:60]}")
                total += len(edits)
            if args.get("apply"):
                applied, failures = 0, []
                for file_uri, edits in edits_by_file.items():
                    try:
                        apply_text_edits(lsp_client.uri_to_path(file_uri), edits)
                        applied += 1
                    except OSError as exc:
                        failures.append(f"{short_path(lsp_client.uri_to_path(file_uri))}: {exc}")
                status = f"aplicado en {applied} archivo(s)"
                if failures:
                    status += " | fallos: " + "; ".join(failures)
            else:
                status = "preview (apply=true para escribir)"
            return text_result(f"{total} edit(s) en {len(edits_by_file)} archivo(s) - {status}\n" + "\n".join(lines[:80]))

        if action == "code_actions":
            result = client.request("textDocument/codeAction", {
                "textDocument": {"uri": uri},
                "range": {"start": position, "end": position},
                "context": {"diagnostics": []},
            }, timeout) or []
            if not result:
                return text_result("(sin acciones)")
            listing = [f"{index}. {item.get('title', '?')} [{item.get('kind', '')}]" for index, item in enumerate(result)]
            chosen = args.get("apply_index")
            if chosen is None:
                return text_result(f"{len(result)} accion(es); usa apply_index para aplicar una\n" + "\n".join(listing[:40]))
            index = int(chosen)
            if not 0 <= index < len(result):
                return text_result(f"apply_index fuera de rango (0..{len(result) - 1})", True)
            edits_by_file = collect_workspace_edits(result[index].get("edit"))
            if not edits_by_file:
                return text_result("Esa accion no trae edit aplicable (puede ser un comando del servidor).", True)
            applied = 0
            for file_uri, edits in edits_by_file.items():
                apply_text_edits(lsp_client.uri_to_path(file_uri), edits)
                applied += 1
            return text_result(f"aplicada: {result[index].get('title', '?')} en {applied} archivo(s)")

        if action == "format":
            edits = client.request("textDocument/formatting", {
                "textDocument": {"uri": uri},
                "options": {"tabSize": int(args.get("tab_size", 2)), "insertSpaces": True},
            }, timeout) or []
            if not edits:
                return text_result("(sin cambios de formato)")
            if args.get("apply"):
                apply_text_edits(path, edits)
                return text_result(f"{len(edits)} edit(s) de formato aplicados")
            lines = []
            for edit in edits:
                start = (edit.get("range") or {}).get("start") or {}
                end = (edit.get("range") or {}).get("end") or {}
                lines.append(f"{start.get('line', 0) + 1}:{start.get('character', 0) + 1}"
                             f"-{end.get('line', 0) + 1}:{end.get('character', 0) + 1} -> "
                             f"{str(edit.get('newText', ''))[:80]!r}")
            return text_result(f"{len(edits)} edit(s) - preview (apply=true para escribir)\n" + "\n".join(lines[:60]))

        return text_result(f"Accion desconocida: {action}", True)

    except TimeoutError as exc:
        return text_result(f"Timeout LSP: {exc}", True)
    except RuntimeError as exc:
        return text_result(f"LSP: {exc}", True)
    except (OSError, ValueError) as exc:
        return text_result(f"{type(exc).__name__}: {exc}", True)


def ast_grep_bin():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ast-grep.exe", "ast-grep"):
        candidate = os.path.join(here, "node_modules", "@ast-grep", "cli", name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ast-grep") or shutil.which("sg")


def run_ast(args, rewrite=None, apply=False):
    binary = ast_grep_bin()
    if not binary:
        return text_result("ast-grep no instalado: npm i --prefix <ompx> @ast-grep/cli", True)
    cmd = [binary, "run", "--color", "never", "-p", args["pattern"]]
    if rewrite is not None:
        cmd += ["-r", rewrite]
        if apply:
            cmd.append("-U")
    if args.get("lang"):
        cmd += ["--lang", args["lang"]]
    if args.get("glob"):
        cmd += ["--globs", args["glob"]]
    root = base_dir(args.get("path"))
    cmd.append(".")
    try:
        proc = subprocess.run(cmd, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return text_result(f"ast-grep fallo: {exc}", True)
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [line for line in out.splitlines() if line.strip()]
    limit = max(1, int(args.get("limit", 80)))
    if len(lines) > limit:
        lines = lines[:limit] + [f"[... {len(lines) - limit} lineas mas]"]
    return text_result("\n".join(lines) or "(sin coincidencias)", proc.returncode > 1)


def tool_ast_grep(args):
    return run_ast(args)


def tool_ast_replace(args):
    return run_ast(args, rewrite=args.get("rewrite"), apply=bool(args.get("apply")))


def tool_sessions(args):
    query = (args.get("query") or "").strip()
    if not query:
        return text_result("Falta query.", True)
    limit = max(1, int(args.get("limit", 20)))
    root = os.path.join(os.path.expanduser("~"), ".vibe", "logs", "session")
    if not os.path.isdir(root):
        return text_result("No hay sesiones guardadas.", True)
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    needle = query.lower()
    hits = []
    for path in files[:500]:
        session = os.path.basename(os.path.dirname(os.path.dirname(path)))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        found_here = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    pos = line.lower().find(needle)
                    if pos < 0:
                        continue
                    snippet = line[max(0, pos - 60): pos + len(query) + 60].replace("\n", " ").strip()
                    hits.append(f"{session} {when}  ...{snippet}...")
                    found_here += 1
                    if len(hits) >= limit or found_here >= 2:
                        break
        except OSError:
            continue
        if len(hits) >= limit:
            break
    if not hits:
        return text_result("(sin coincidencias)")
    return text_result("\n".join(hits))


def tool_apply_patch(args):
    diff = args.get("diff") or ""
    if not diff.strip():
        return text_result("Falta diff.", True)
    root = base_dir(args.get("path"))
    handle, tmp = tempfile.mkstemp(prefix="ompx-", suffix=".patch")
    os.close(handle)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(diff if diff.endswith("\n") else diff + "\n")
        check = subprocess.run(["git", "apply", "--check", tmp], cwd=root, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check.returncode != 0:
            return text_result("El parche no aplica:\n" + ((check.stderr or check.stdout) or "")[:1500], True)
        if not args.get("apply"):
            return text_result("El parche aplica limpio (preview). Usa apply=true para escribir.")
        run = subprocess.run(["git", "apply", tmp], cwd=root, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if run.returncode != 0:
            return text_result("Error aplicando:\n" + (run.stderr or run.stdout)[:1500], True)
        return text_result("Parche aplicado.")
    except (OSError, subprocess.SubprocessError) as exc:
        return text_result(f"git apply fallo: {exc}", True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _iter_texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_texts(item)


def tool_sessions_get(args):
    session = (args.get("session_id") or "").strip()
    if not session:
        return text_result("Falta session_id.", True)
    limit = max(1, int(args.get("limit", 20)))
    root = os.path.join(os.path.expanduser("~"), ".vibe", "logs", "session")
    files = glob.glob(os.path.join(root, "**", session, "**", "*.jsonl"), recursive=True)
    if not files:
        return text_result(f"Sesion no encontrada: {session}", True)
    fragments = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    for text in _iter_texts(obj):
                        clean = text.strip()
                        if len(clean) < 40 or " " not in clean:
                            continue
                        if not re.search(r"[A-Za-z]{3}", clean):
                            continue
                        if clean not in fragments[-3:]:
                            fragments.append(clean)
        except OSError:
            continue
    if not fragments:
        return text_result("(sin texto util en esa sesion)")
    tail = fragments[-limit:]
    return text_result(f"{len(fragments)} fragmento(s); ultimos {len(tail)}\n" + "\n---\n".join(item[:600] for item in tail))


def tool_info(args):
    py_label, _, _ = lsp_client.server_for("python")
    ts_label, _, _ = lsp_client.server_for("typescript")
    lines = [
        f"ompx {SERVER['version']}",
        f"python {sys.version.split()[0]} -> {sys.executable}",
        f"node -> {NODE}",
        f"ast-grep -> {ast_grep_bin() or 'no instalado'}",
        f"lsp python -> {py_label or 'no disponible'}",
        f"lsp ts/js -> {ts_label or 'no disponible'}",
        f"lsp php -> {lsp_client.server_for('php')[0] or 'no disponible'}",
        f"cwd -> {os.getcwd()}",
        f"tools -> {', '.join(sorted(HANDLERS))}",
    ]
    return text_result("\n".join(lines))


HANDLERS = {
    "glob": tool_glob,
    "grep": tool_grep,
    "read": tool_read,
    "eval_python": tool_eval,
    "eval_js": lambda args: tool_eval(args, "js"),
    "lsp_query": tool_lsp,
    "ast_grep": tool_ast_grep,
    "ast_replace": tool_ast_replace,
    "sessions": tool_sessions,
    "sessions_get": tool_sessions_get,
    "apply_patch": tool_apply_patch,
    "ompx_info": tool_info,
}


def dispatch(req):
    method = req.get("method", "")
    rid = req.get("id")
    dbg(f"recv {method} id={rid}")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": rid, "result": text_result(f"Tool desconocida: {name}", True)}
        try:
            result = handler(params.get("arguments") or {})
        except Exception as exc:
            result = text_result(f"{type(exc).__name__}: {exc}", True)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Metodo no soportado: {method}"}}


def handle(req):
    try:
        reply = dispatch(req)
    except Exception as exc:  # nunca dejar al cliente esperando
        reply = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
    if reply is not None:
        send(reply)


def main():
    for stream, kwargs in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                           (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"})):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError):
            pass
    workers = []
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except Exception:
                continue
            if req.get("id") is None:
                # notificacion: se atiende al momento y no responde
                try:
                    dispatch(req)
                except Exception:
                    pass
                continue
            thread = threading.Thread(target=handle, args=(req,), daemon=True)
            thread.start()
            workers.append(thread)
    finally:
        for thread in workers:
            thread.join(timeout=600)
        try:
            lsp_client.close_all()
        except Exception:
            pass
        for kind in KERNELS:
            kernel_stop(kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
