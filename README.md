# MCPMistralOMP

Configuración reproducible de **Mistral Vibe** (CLI y extensión de VS Code) con el comportamiento y las capacidades que uso en omp: autonomía con frenos, MCP propio (búsqueda, LSP, ejecución, ast-grep, historial), navegador real, agentes y skills de proceso compartidas.

Probado en Windows 11 con Vibe `3.1.4`, Python 3.14, Node 24, VS Code 1.147 y la extensión `mistralai.mistral-vibe-code` 1.30.0.

## Estructura

```
.
├── install.ps1              instalador idempotente (5 pasos)
├── vibe/                    lo que se copia a ~/.vibe
│   ├── config.toml          permisos, denylist, sensibles, MCP, modelos
│   ├── AGENTS.md            reglas de ingeniería y mapa de tools
│   ├── hooks.toml           declaración del hook pre_tool
│   ├── hooks/intercept.py   bloquea cat/head/tail, sed -i, echo >
│   └── agents/              plan · scout · reviewer · ui-designer
├── mcp/ompx/                servidor MCP propio
│   ├── server.py            12 tools, JSON-RPC a mano, sin dependencias Python
│   ├── lsp_client.py        cliente LSP (pyright, intelephense, typescript-language-server)
│   └── package.json         deps fijadas: pyright, intelephense, TS 5, tsserver, ast-grep
└── scripts/
    └── merge_trust.py       añade rutas a trusted_folders.toml sin duplicar
```

## Requisitos

| Pieza | Versión | Nota |
|---|---|---|
| Mistral Vibe | 3.1.4+ | CLI; la extensión de VS Code trae su propio agente |
| Python | 3.11+ | `ompx` es stdlib; `pypdf` opcional para PDFs |
| Node | 18+ | kernels JS y language servers |
| Git | cualquiera | `git ls-files` da la semántica de `.gitignore` |
| PowerShell | 5.1+ | instalador |

## Instalación

```powershell
git clone https://github.com/alexandervi1/MCPMistralOMP.git $env:USERPROFILE\.vibe-omp
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.vibe-omp\install.ps1
```

Otros workspaces (el trust es por prefijo de ruta):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -TrustRoots "C:\dev","D:\work"
```

En VS Code, `/reload` o **Developer: Reload Window** para que el panel recoja la config.

## Qué hace el instalador

1. Crea `~/.vibe/{agents,hooks,mcp/ompx}`.
2. Copia `vibe/` y `mcp/`; respalda el `config.toml` previo en `config.toml.prev` y **reescribe las rutas absolutas** incrustadas para la máquina actual.
3. Añade los roots a `~/.vibe/trusted_folders.toml` sin duplicar entradas.
4. `npm i` con las versiones fijadas de `mcp/ompx/package.json`.
5. Enlaza (junction) 13 skills de `superpowers` desde `~/.omp` a `~/.agents/skills`, que es la ruta que comparten omp y Vibe. Si no existe `~/.omp`, se omite.

## Tools de `ompx`

| Tool | Qué hace |
|---|---|
| `glob` | rutas por patrón, `.gitignore` exacto vía `git ls-files` |
| `grep` | `ruta:linea: texto`, paginación, filtro por glob y líneas de contexto |
| `read` | rangos de línea; bloquea `.env`/`secrets`/`*.pem`/`*.key`/`.ssh`; URL, PDF, docx/pptx/xlsx y zip (lista o extrae una entrada) |
| `eval_python` | kernel persistente (variables e imports sobreviven) |
| `eval_js` | kernel Node persistente (`var`/`globalThis` sobreviven, `await` soportado) |
| `lsp_query` | `definitions`, `type_definition`, `implementation`, `references`, `hover`, `diagnostics`, `symbols`, `workspace_symbols`, `rename` (preview/apply), `code_actions`, `format` — pyright (Python), intelephense (PHP), typescript-language-server (TS/JS) |
| `ast_grep` | búsqueda estructural (`$A`, `$$$`) |
| `ast_replace` | reescritura estructural; `apply=true` escribe |
| `apply_patch` | aplica un diff unificado con `git apply`; por defecto `--check` |
| `sessions` | busca en `~/.vibe/logs/session` |
| `sessions_get` | lee una sesión por id y devuelve sus últimos fragmentos |
| `ompx_info` | versión, rutas de python/node/ast-grep, language servers y tools |

Además `browse` (Playwright MCP): 25 tools de navegador con captura y snapshot.

## Verificación

```powershell
vibe --version
vibe -p "Responde exactamente: OK" --max-turns 1
vibe -p "Lista las tools del servidor ompx, una por linea"
```

En el panel de VS Code el selector debe mostrar **DeepSeek V4.1 Flash**, los agentes `plan` y `ui-designer`, y thinking `none/low/high/xhigh/max`.

## Mapa con omp

| omp | Aquí |
|---|---|
| `AGENTS.md` global | `vibe/AGENTS.md` |
| skills de `~/.agents/skills` | mismas, compartidas |
| `agents/*.md` | `vibe/agents/*.toml` |
| `bashInterceptor` | `hooks/intercept.py` |
| tools `grep`/`glob`/`read`/`eval`/`lsp`/`ast_edit` | MCP `ompx` |
| navegador | MCP `browse` |
| hooks `.ts` | shell + JSON por stdin |
| subagentes con MCP | **no posible**: regla de Vibe |

## Mantenimiento

```powershell
# tras cambiar las tools de un MCP (Vibe cachea el catálogo por servidor)
Remove-Item "$env:USERPROFILE\.vibe\logs\mcp-descriptors\unified\*.json"

# trazas del MCP (LSP, timings)
New-Item "$env:USERPROFILE\.vibe\mcp\ompx\trace" -ItemType File
```

`/config` y `/skills` escriben en `~/.vibe/`, no en este repo. Si un cambio debe sobrevivir a una reinstalación, cópialo aquí y haz commit.

## Límites conocidos

- Los subagentes de Vibe no pueden usar MCP: `scout` y `reviewer` solo ven `read_file`/`bash`/`skill`.
- El agente embebido del panel (3.1.3) no expone `smart-approve`; el CLI sí.
- El modelo no acepta `thinking=medium`: cada preset declara `thinking_levels` válidos.
- Sin `certifi`, las lecturas por URL caen a TLS sin verificar (el output lo avisa).
- Sin `pypdf`, `read` no extrae PDFs (devuelve el comando para instalarlo).

## Seguridad

`default_agent = "accept-edits"` con `bash` en `permission = "always"`: el agente ejecuta shell sin preguntar. La denylist cubre `rm -rf`, `git reset --hard`, `git push --force` y publishes; `sensitive_patterns` frena `rm`, `mv`, `chmod`, `curl`, `ssh`, `reg`… En repos ajenos: `vibe --agent ask` o `vibe --agent plan`.

## Licencia

MIT. Ver [LICENSE](LICENSE).
