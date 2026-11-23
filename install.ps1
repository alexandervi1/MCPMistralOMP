# Instala esta configuración de Vibe en la máquina actual.
# Uso:  powershell -ExecutionPolicy Bypass -File install.ps1
#       powershell -ExecutionPolicy Bypass -File install.ps1 -TrustRoots "C:\dev","D:\work"
# Idempotente: se puede ejecutar tantas veces como quieras.

param(
    [string[]]$TrustRoots = @((Join-Path $env:USERPROFILE "Documents\GitHub"))
)

$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$vibe = Join-Path $env:USERPROFILE ".vibe"

Write-Host "[1/5] Directorios"
foreach ($dir in @($vibe, "$vibe\agents", "$vibe\hooks", "$vibe\mcp\ompx")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "[2/5] Copiando configuracion"
if (Test-Path "$vibe\config.toml") {
    Copy-Item "$vibe\config.toml" "$vibe\config.toml.prev" -Force
}
Copy-Item "$src\vibe\config.toml" "$vibe\config.toml" -Force
Copy-Item "$src\vibe\AGENTS.md" "$vibe\AGENTS.md" -Force
Copy-Item "$src\vibe\hooks.toml" "$vibe\hooks.toml" -Force
Copy-Item "$src\vibe\hooks\intercept.py" "$vibe\hooks\intercept.py" -Force
Copy-Item "$src\vibe\agents\*.toml" "$vibe\agents\" -Force
Copy-Item "$src\mcp\ompx\*.py" "$vibe\mcp\ompx\" -Force
Copy-Item "$src\mcp\ompx\package.json" "$vibe\mcp\ompx\" -Force

# Las rutas del usuario van incrustadas en config.toml (server MCP) y hooks.toml.
# Reescribirlas por la ruta real de esta maquina.
$fwd = ($vibe -replace '\\', '/')
foreach ($file in @("$vibe\config.toml", "$vibe\hooks.toml")) {
    $text = [System.IO.File]::ReadAllText($file)
    $updated = $text -replace 'C:/Users/Workstation 2040/\.vibe/', "$fwd/"
    if ($updated -ne $text) {
        [System.IO.File]::WriteAllText($file, $updated, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "  ruta reescrita en $(Split-Path $file -Leaf)"
    }
}

Write-Host "[3/5] Trust de workspaces"
foreach ($root in $TrustRoots) {
    if (Test-Path $root) {
        & python "$src\scripts\merge_trust.py" $root
    } else {
        Write-Host "  (omitido, no existe): $root"
    }
}

Write-Host "[4/5] Dependencias del MCP (pyright, typescript, tsserver, ast-grep)"
& npm i --prefix "$vibe\mcp\ompx" --silent
Write-Host "  npm exit: $LASTEXITCODE"
Write-Host "  (opcional, PDFs: python -m pip install pypdf)"

Write-Host "[5/5] Skills de superpowers (junctions desde omp)"
$skillSrc = Join-Path $env:USERPROFILE ".omp\plugins\node_modules\superpowers\skills"
$skillDst = Join-Path $env:USERPROFILE ".agents\skills"
$skillNames = @("brainstorming","systematic-debugging","test-driven-development","writing-plans","executing-plans","verification-before-completion","requesting-code-review","receiving-code-review","subagent-driven-development","dispatching-parallel-agents","using-git-worktrees","finishing-a-development-branch","writing-skills")
if (Test-Path $skillSrc) {
    New-Item -ItemType Directory -Force -Path $skillDst | Out-Null
    foreach ($n in $skillNames) {
        $link = Join-Path $skillDst $n
        $target = Join-Path $skillSrc $n
        if ((Test-Path $target) -and -not (Test-Path $link)) {
            cmd /c mklink /J "$link" "$target" | Out-Null
            Write-Host "  + $n"
        }
    }
} else {
    Write-Host "  (omitido, no existe $skillSrc)"
}

Write-Host ""
Write-Host "Listo. Verifica con:"
Write-Host "  vibe --version"
Write-Host "  vibe -p 'Responde OK' --max-turns 1"
Write-Host "  vibe -p 'Lista las tools del servidor ompx, una por linea'"
Write-Host "Si cambiaste las tools de un MCP:"
Write-Host "  Remove-Item (Join-Path $vibe 'logs\mcp-descriptors\unified\*.json')"
