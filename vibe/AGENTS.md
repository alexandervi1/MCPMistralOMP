# Vibe — reglas globales

Equivalen a omp. Un AGENTS.md de proyecto más cercano gana sobre esto.

## Herramientas
- Nombres reales: `read_file`, `write_file`, `edit`, `bash`/`git_bash`/`powershell`, `todo`, `task`, `skill`, `web_search`, `web_fetch`.
- MCP `ompx` añade `glob`, `grep`, `read` (rangos de línea, bloquea `.env`/`*.pem`/`*.key`/`.ssh`), `eval_python` (kernel persistente: variables e imports sobreviven entre llamadas) y `lsp_query` (pyright para Python, intelephense para PHP, typescript-language-server para TS/JS; actions: definitions, references, hover, diagnostics, symbols; `line`/`column` 1-based). Para buscar, navegar código o calcular con estado, usa `ompx` antes que el shell.
- MCP `browse` da el navegador (Playwright): navegar, snapshot, captura, click, formularios.
- No hay `ast_edit`, `canvas`, `out://`, `xd://` ni kernel JS. Si una instrucción los pide, uso lo más cercano y digo qué falta.
- Skills del usuario en `~/.agents/skills`. Si una skill dice "read skill://X", la cargo con la herramienta `skill`; no intento leer `skill://`.
- `.omp/`, `~/.omp/` y sus hooks son contexto de omp, no de Vibe: no los cargo salvo petición explícita.

## Skills de proceso (superpowers)

Las 13 skills de `superpowers` de omp están enlazadas en `~/.agents/skills` (junction, misma fuente). Su texto nombra tools de Claude/Pi; traduzco al vuelo:

- `Task` → `task` · `TodoWrite` → `todo` · `Skill` → `skill`
- `Read` → `read_file` · `Write` → `write_file` · `Edit` → `edit` · `Bash` → `bash`
- `Grep` → `ompx.grep` · `Glob` → `ompx.glob`
- Si una skill pide `git worktree`, puedo usar además `vibe --worktree <nombre>`.

## Ediciones
- Leo el archivo antes de editarlo. Edito con `edit`/`write_file`, nunca con sed/perl.
- Cambio mínimo; reutilizo patrones existentes; no añado una segunda convención.
- Borro el código que mi cambio deja muerto. Lo que no escribí no se toca sin permiso.
- Antes de un comando destructivo o de borrar algo ajeno, pregunto.

## Verificación
- La prueba más estrecha que ejercite el cambio, una vez. No suites completas por rutina.
- No declaro hecho lo que no observé. Sin salida observada = digo que no está verificado.
- No repito un check que el usuario ya reportó, ni re-audito lo aplicado.
- UI web: abro la superficie real; si no hay runtime, script desechable y reporto el límite visual.

## Comunicación
- Español, técnico, frases cortas. Sin relleno, sin narrar pasos obvios, sin resúmenes.
- Rutas, símbolos y comandos exactos. Lo incierto va marcado como inferencia.
- Bloqueado solo si las tools no alcanzan la información: hago todo lo alcanzable y digo qué falta y qué intenté.

## UI
Antes de tocar UI: cargo `impeccable` con la herramienta `skill` (o `frontend-design` si es superficie nueva y sin comando). Launcher válido en este Windows: `~/.agents/skills/impeccable/scripts/impeccable.cmd`; el script POSIX falla por CRLF.

Verificación visual (sustituye al navegador de omp):
- Principal: MCP `browse` (Playwright). `browser_navigate` → `browser_resize` a 1440x900 → `browser_take_screenshot`. Mira la captura antes de declarar terminado.
- `browser_evaluate` y `browser_file_upload` piden aprobación; úsalos solo cuando el CSS computado o un input de archivo sean necesarios.
- Los subagentes no ven MCP. Para ellos: `npx -y playwright@latest screenshot --viewport-size="1440,900" <url> shot.png` por shell y `read_file` del PNG.
- Playwright deja `.playwright-mcp/` en el cwd: artefacto, no se commitea.
