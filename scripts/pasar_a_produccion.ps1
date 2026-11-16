# Pasa el vigilante de la carpeta de Hermes a este proyecto (una sola vez).
# Uso, en PowerShell:  & C:\Users\Zeus\proyectos\vigilante-chollos\scripts\pasar_a_produccion.ps1
# Pasos: 1) para las tareas y espera a que acabe la ronda en curso, 2) copia los datos,
# 3) apunta las tareas al proyecto, 4) instala la habilidad de Hermes con las rutas nuevas,
# y vuelve a activar las tareas. Si algo falla, las tareas se quedan DESACTIVADAS y no se pierde nada:
# la carpeta de Hermes no se borra.

$ErrorActionPreference = 'Stop'
$proy   = 'C:\Users\Zeus\proyectos\vigilante-chollos'
$viejo  = 'C:\Users\Zeus\AppData\Local\hermes\scripts'
$skill  = 'C:\Users\Zeus\AppData\Local\hermes\skills\verifica-chollo\SKILL.md'
$pyw    = 'C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\pythonw.exe'

$tareas = @(Get-ScheduledTask | Where-Object TaskName -like 'Vigilante*')
if ($tareas.Count -eq 0) { throw 'No hay tareas "Vigilante*".' }
Write-Host "Tareas: $($tareas.TaskName -join ', ')"

# 1) Parar
$tareas | Disable-ScheduledTask | Out-Null
Write-Host '1/4 Tareas desactivadas. Esperando a que termine la ronda en curso (max. 20 min)...'
$limite = (Get-Date).AddMinutes(20)
while ($true) {
    $vivos = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'hermes\\scripts\\(vig_|vigilante\.py)' })
    if ($vivos.Count -eq 0) { break }
    if ((Get-Date) -gt $limite) { throw "Sigue habiendo rondas en marcha (PID $($vivos.ProcessId -join ', ')). Tareas desactivadas; vuelve a lanzar el script mas tarde." }
    Start-Sleep -Seconds 15
}

# 2) Datos (base, cachés, perfil de Chrome con las sesiones)
Write-Host '2/4 Copiando datos...'
robocopy "$viejo\vigilante_data" "$proy\data" /E /XF navegador.lock /R:1 /W:1 /NFL /NDL /NJH /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy fallo (codigo $LASTEXITCODE). Tareas desactivadas." }
$o = (Get-Item "$viejo\vigilante_data\vistos.db").Length; $d = (Get-Item "$proy\data\vistos.db").Length
if ($o -ne $d) { throw "vistos.db no coincide ($o vs $d bytes). Tareas desactivadas." }

# 3) Tareas -> proyecto
Write-Host '3/4 Apuntando las tareas al proyecto...'
foreach ($t in $tareas) {
    $grupo = if ($t.TaskName -like '*navegador*') { 'navegador' } else { 'rapidas' }
    $accion = New-ScheduledTaskAction -Execute $pyw -Argument "`"$proy\tareas\vig_$grupo.py`"" -WorkingDirectory $proy
    Set-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Action $accion | Out-Null
}

# 4) Habilidad de Hermes (copia de seguridad de la anterior)
Write-Host '4/4 Instalando la habilidad verifica-chollo...'
Copy-Item $skill "$skill.antes-del-proyecto" -Force
Copy-Item "$proy\hermes\skills\verifica-chollo\SKILL.md" $skill -Force

# Reactivar y marcar la copia vieja
$tareas | Enable-ScheduledTask | Out-Null
Set-Content -Path "$viejo\NO_EDITAR_YA_NO_SE_USA.txt" -Encoding UTF8 -Value "Desde $(Get-Date -Format 'dd/MM/yyyy HH:mm') el vigilante funciona desde $proy. Esta carpeta queda como copia de seguridad."

Get-ScheduledTask | Where-Object TaskName -like 'Vigilante*' | ForEach-Object {
    [pscustomobject]@{ Tarea = $_.TaskName; Estado = $_.State; Lanza = $_.Actions[0].Arguments }
} | Format-Table -AutoSize
Write-Host 'Hecho. Comprueba en un rato con:  & $py src\vigilante.py informe 1'
