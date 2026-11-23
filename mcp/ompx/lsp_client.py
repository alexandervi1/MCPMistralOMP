"""Cliente LSP minimo para el MCP ompx.

Servidores persistentes por (comando, root). Framing Content-Length sobre
stdin/stdout binario. Responde a las requests que el servidor hace al cliente
para que no se quede colgado. Detecta la muerte del servidor y adjunta stderr.
"""

import collections
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

_npm_root = [None]

TRACE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace")


def trace(message):
    """Activa trazas creando el archivo 'trace' junto a este modulo."""
    if not os.path.exists(TRACE_FILE):
        return
    try:
        with open(os.path.join(os.path.dirname(TRACE_FILE), "ompx-trace.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} {message}\n")
    except OSError:
        pass


LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".rs": "rust", ".go": "go", ".json": "json",
    ".css": "css", ".scss": "scss", ".html": "html",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
}

ROOT_MARKERS = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git"),
    "typescript": ("tsconfig.json", "package.json", ".git"),
    "javascript": ("package.json", "tsconfig.json", ".git"),
    "rust": ("Cargo.toml", ".git"),
    "go": ("go.mod", ".git"),
}


def npm_global_root():
    if _npm_root[0] is not None:
        return _npm_root[0]
    cmd = ["cmd", "/c", "npm", "root", "-g"] if os.name == "nt" else ["npm", "root", "-g"]
    trace(f"npm root start {cmd}")
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        _npm_root[0] = proc.stdout.strip() if proc.returncode == 0 else ""
        trace(f"npm root rc={proc.returncode} out={_npm_root[0]!r} err={proc.stderr.strip()[:120]!r}")
    except (OSError, subprocess.SubprocessError) as exc:
        trace(f"npm root failed {exc}")
        _npm_root[0] = ""
    return _npm_root[0]


def node_bin():
    return shutil.which("node") or "node"


def server_for(language):
    """Devuelve (etiqueta, comando, initializationOptions) o (None, None, None).

    Prefiere los paquetes vendorizados junto a este modulo; despues los globales.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "node_modules")
    root = npm_global_root()
    if language == "python":
        for base in (local, root):
            if not base:
                continue
            js = os.path.join(base, "pyright", "langserver.index.js")
            if os.path.isfile(js):
                return "pyright", [node_bin(), js, "--stdio"], None
        exe = shutil.which("pylsp")
        if exe:
            return "pylsp", [exe], None
        return None, None, None
    if language in ("typescript", "javascript"):
        for base in (local, root):
            if not base:
                continue
            js = os.path.join(base, "typescript-language-server", "lib", "cli.mjs")
            tsserver = os.path.join(base, "typescript", "lib", "tsserver.js")
            if os.path.isfile(js) and os.path.isfile(tsserver):
                return "tls", [node_bin(), js, "--stdio"], {"tsserver": {"path": tsserver}}
        return None, None, None
    if language == "rust":
        exe = shutil.which("rust-analyzer")
        return ("rust-analyzer", [exe], None) if exe else (None, None, None)
    if language == "go":
        exe = shutil.which("gopls")
        return ("gopls", [exe], None) if exe else (None, None, None)
    return None, None, None


def path_to_uri(path):
    from pathlib import Path

    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri):
    parts = urllib.parse.urlsplit(uri)
    path = urllib.parse.unquote(parts.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return os.path.normpath(path)


def find_root(path, language):
    markers = ROOT_MARKERS.get(language, (".git",))
    current = os.path.dirname(os.path.abspath(path))
    while True:
        for marker in markers:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.dirname(os.path.abspath(path))
        current = parent


class Client:
    def __init__(self, label, command, root, init_options=None):
        self.label = label
        self.root = root
        self.init_options = init_options
        self.seq = 0
        self.messages = queue.Queue()
        self.diagnostics = {}
        self.capabilities = {}
        self.opened = {}
        self.version = 0
        self.dead = False
        self.lock = threading.RLock()
        self.stderr_tail = collections.deque(maxlen=20)
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
        )
        trace(f"spawn {self.label} cwd={root} pid={self.proc.pid}")
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        try:
            self._initialize()
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
            raise

    # --- framing -------------------------------------------------------
    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def _read_loop(self):
        stream = self.proc.stdout
        while True:
            headers = {}
            while True:
                line = stream.readline()
                if not line:
                    self.dead = True
                    return
                line = line.strip()
                if not line:
                    break
                key, _, value = line.partition(b":")
                headers[key.strip().lower()] = value.strip()
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                continue
            if length <= 0:
                continue
            body = stream.read(length)
            try:
                self.messages.put(json.loads(body))
            except Exception:
                continue

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def _died(self):
        if not self.dead and self.proc.poll() is None:
            return None
        code = self.proc.poll()
        if code == 0:
            return None
        detail = " | ".join(list(self.stderr_tail)[-5:]) or "sin stderr"
        trace(f"{self.label} died rc={code} stderr={detail[:300]}")
        return f"{self.label} termino (rc={code}): {detail[:500]}"

    def _next(self, timeout):
        try:
            return self.messages.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None

    def _answer_server_request(self, msg):
        """El servidor nos pide cosas: contestar null evita bloqueos.

        Solo mensajes con 'method' Y 'id' son requests del servidor; una respuesta
        a nuestra propia request tambien trae 'id' pero no 'method'.
        """
        method = msg.get("method")
        if not method or "id" not in msg:
            return False
        params = msg.get("params") or {}
        if method == "workspace/configuration":
            items = params.get("items") or []
            result = [None] * len(items)
        elif method in ("workspace/workspaceFolders",):
            result = [{"uri": path_to_uri(self.root), "name": os.path.basename(self.root)}]
        else:
            result = None
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        return True

    def _initialize(self):
        self.seq += 1
        params = {
            "processId": os.getpid(),
            "rootUri": path_to_uri(self.root),
            "workspaceFolders": [{"uri": path_to_uri(self.root), "name": os.path.basename(self.root)}],
            "capabilities": {
                "workspace": {"workspaceFolders": True, "configuration": True},
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False},
                    "definition": {}, "references": {}, "hover": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "publishDiagnostics": {"relatedInformation": False},
                    "diagnostic": {"dynamicRegistration": False},
                },
            },
            "clientInfo": {"name": "ompx", "version": "1.0.0"},
        }
        if self.init_options:
            params["initializationOptions"] = self.init_options
        self._send({"jsonrpc": "2.0", "id": self.seq, "method": "initialize", "params": params})
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = self._next(min(0.5, max(0.05, deadline - time.time())))
            if msg is None:
                died = self._died()
                if died:
                    raise RuntimeError(died)
                continue
            if self._answer_server_request(msg):
                continue
            if msg.get("id") == self.seq:
                if "error" in msg:
                    err = msg["error"] or {}
                    trace(f"init error {err}")
                    raise RuntimeError(f"{self.label}: {err.get('message', err)}")
                if "result" in msg:
                    self.capabilities = msg["result"].get("capabilities", {})
                    self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    trace(f"init ok {self.label} caps={len(self.capabilities)}")
                    return
        raise TimeoutError(f"{self.label} no completo el handshake")

    # --- documentos ----------------------------------------------------
    def open_document(self, path):
        with self.lock:
            return self._open_document(path)

    def _open_document(self, path):
        uri = path_to_uri(path)
        text = open(path, "r", encoding="utf-8", errors="replace").read()
        language = LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower(), "plaintext")
        if uri in self.opened and self.opened[uri][0] == text:
            return uri
        self.version += 1
        if uri in self.opened:
            self._send({
                "jsonrpc": "2.0", "method": "textDocument/didChange",
                "params": {"textDocument": {"uri": uri, "version": self.version},
                           "contentChanges": [{"text": text}]},
            })
        else:
            self._send({
                "jsonrpc": "2.0", "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": uri, "languageId": language,
                                            "version": self.version, "text": text}},
            })
        self.opened[uri] = (text, language)
        return uri

    @staticmethod
    def _key(uri):
        try:
            return os.path.normcase(uri_to_path(uri))
        except Exception:
            return uri

    # --- requests ------------------------------------------------------
    def request(self, method, params, timeout):
        with self.lock:
            return self._request(method, params, timeout)

    def _request(self, method, params, timeout):
        self.seq += 1
        rid = self.seq
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._next(min(0.5, max(0.05, deadline - time.time())))
            if msg is None:
                died = self._died()
                if died:
                    raise RuntimeError(died)
                continue
            if self._answer_server_request(msg):
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                doc = (msg.get("params") or {}).get("uri")
                if doc:
                    self.diagnostics[self._key(doc)] = (msg.get("params") or {}).get("diagnostics", [])
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    trace(f"req {method} error {msg['error']}")
                    raise RuntimeError(msg["error"].get("message", "error LSP"))
                trace(f"req {method} ok")
                return msg.get("result")
        trace(f"req {method} TIMEOUT {timeout}")
        raise TimeoutError(f"{method} sin respuesta en {timeout:g}s")

    def wait_diagnostics(self, uri, timeout):
        with self.lock:
            return self._wait_diagnostics(uri, timeout)

    def _wait_diagnostics(self, uri, timeout):
        key = self._key(uri)
        if "diagnosticProvider" in self.capabilities:
            result = self.request("textDocument/diagnostic",
                                  {"textDocument": {"uri": uri}}, timeout) or {}
            if result.get("kind") == "full":
                items = result.get("items", [])
                trace(f"diag(pull) {os.path.basename(key)} items={len(items)}")
                return items
            return self.diagnostics.get(key, [])
        deadline = time.time() + min(timeout, 5)
        while time.time() < deadline:
            msg = self._next(min(0.5, max(0.05, deadline - time.time())))
            if msg is None:
                died = self._died()
                if died:
                    raise RuntimeError(died)
                continue
            if self._answer_server_request(msg):
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                doc = (msg.get("params") or {}).get("uri")
                if doc:
                    self.diagnostics[self._key(doc)] = (msg.get("params") or {}).get("diagnostics", [])
                if doc and self._key(doc) == key:
                    break
        items = self.diagnostics.get(key, [])
        trace(f"diag {os.path.basename(key)} items={len(items)}")
        return items

    def close(self):
        try:
            self.request("shutdown", None, 3)
        except Exception:
            pass
        try:
            self._send({"jsonrpc": "2.0", "method": "exit"})
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


_clients = {}
_clients_lock = threading.Lock()


def get_client(path, language):
    root = find_root(path, language)
    label, command, init_options = server_for(language)
    trace(f"server_for {language} -> {label} {command} root={root}")
    if not command:
        raise RuntimeError(
            f"sin language server para '{language}'. Instala pyright/tsserver, "
            "o pasa language_server: {command, args}."
        )
    key = (label, root, tuple(command))
    with _clients_lock:
        client = _clients.get(key)
        if client is None or client.proc.poll() is not None:
            client = Client(label, command, root, init_options)
            _clients[key] = client
        return client


def close_all():
    with _clients_lock:
        for client in list(_clients.values()):
            client.close()
        _clients.clear()
