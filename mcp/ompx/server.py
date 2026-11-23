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
import threading
import time
import urllib.parse
import urllib.request

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
        "description": "Consulta el language server del archivo (.py via pyright, .ts/.js via typescript-language-server): definiciones, referencias, hover, diagnosticos y simbolos del documento.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definitions", "references", "hover", "diagnostics", "symbols"],
                },
                "path": {"type": "string"},
                "line": {"type": "integer", "default": 1, "description": "Linea 1-based (definitions, references, hover)."},
                "column": {"type": "integer", "default": 1, "description": "Columna 1-based (definitions, references, hover)."},
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
    hits, seen = [], 0
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
                for number, line in enumerate(fh, start=1):
                    if not rx.search(line):
                        continue
                    if seen < skip:
                        seen += 1
                        continue
                    hits.append(f"{rel}:{number}: {line.rstrip()[:400]}")
                    seen += 1
                    if len(hits) >= limit + 1:
                        break
        except (OSError, ValueError):
            continue
        if len(hits) > limit:
            break
    if not hits:
        return text_result("(sin coincidencias)")
    page = hits[:limit]
    tail = "" if len(hits) <= limit else f"\n[... mas; usa skip={skip + limit}]"
    return text_result("\n".join(page) + tail)


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


def tool_read(args):
    raw = str(args.get("path") or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        return read_url(raw)
    path = os.path.abspath(raw)
    offset = max(1, int(args.get("offset", 1)))
    limit = max(1, int(args.get("limit", 150)))
    if path.lower().endswith(".pdf"):
        return read_pdf(path, offset, limit)
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


def tool_lsp(args):
    action = args.get("action")
    path = os.path.abspath(args.get("path") or "")
    if not os.path.isfile(path):
        return text_result(f"No existe o no es archivo: {path}", True)
    language = lsp_client.LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not language:
        ext = os.path.splitext(path)[1] or "(sin extension)"
        return text_result(f"Sin language server para {ext}. Soportado: .py .ts .tsx .js .jsx .rs .go", True)

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

        if action in ("definitions", "references"):
            if action == "definitions":
                method, params = "textDocument/definition", {"textDocument": {"uri": uri}, "position": position}
            else:
                method, params = "textDocument/references", {
                    "textDocument": {"uri": uri},
                    "position": position,
                    "context": {"includeDeclaration": True},
                }
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
