# Vigilante de chollos (contexto para Claude Code)

Proyecto personal de Zeta: vigilante de ofertas para Chollometro (perfil PorLa_Jeta). Lee primero `README.md` (estructura y comandos) y `docs/ESTADO.md` (historial de decisiones, estado por tienda y lo aprendido; está escrito con las rutas antiguas de la carpeta de Hermes).

Habla con Zeta en español, directo y con sinceridad. Prioridad: **gastar los mínimos tokens posibles** sin empeorar la calidad. Todo lo que se pueda hacer con código, con código.

## Entorno

- Python de Hermes (sin PyYAML; con Playwright y Chromium). `python` no está en el PATH:
  `C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe`
- Pruebas: `& $py -m unittest discover -s tests`. Son 160 y deben pasar siempre. Cada cambio lleva su prueba.
- En PowerShell 5.1 los `python -c` con comillas fallan: usa scripts en el scratchpad.
- git: `C:\Program Files\Git\cmd\git.exe` (instalado el 16/11/2026). Remoto privado en GitHub: https://github.com/zetaguijarro/vigilante-chollos (`main` sigue a `origin/main`, push con la credencial guardada de Windows).

## Dos copias del código (importante)

- **En marcha:** `C:\Users\Zeus\AppData\Local\hermes\scripts` (vigilante.py, tiendas.json, vigilante_data/). La lanzan las tareas "Vigilante rapidas" y "Vigilante navegador" y la usa la habilidad `verifica-chollo` de Hermes.
- **Este proyecto:** la misma versión, ordenada y con git. Datos propios en `data/` (vacía salvo las URLs).
- Mientras no se pase a producción, un cambio aquí NO afecta a lo que funciona. Si Zeta pide un cambio en el vigilante, pregunta en cuál de las dos copias, o haz el cambio aquí y llévalo a la otra.

## Pasar a producción (solo con autorización de Zeta)

Lo hace `scripts\pasar_a_produccion.ps1`, que tiene que lanzar Zeta: el sistema de permisos bloquea a Claude desactivar o cambiar las tareas programadas (16/11/2026). La habilidad del proyecto ya tiene las rutas nuevas. Pasos que hace el script:

1. Copiar `vigilante_data/` de Hermes a `data/` (o fijar `VIGILANTE_DATOS` a esa carpeta).
2. Cambiar las dos tareas programadas para que lancen `tareas\vig_rapidas.py` y `tareas\vig_navegador.py` de este proyecto.
3. Cambiar en `hermes/skills/verifica-chollo/SKILL.md` la ruta de `vigilante.py` y copiarla a `C:\Users\Zeus\AppData\Local\hermes\skills\verifica-chollo\`.
4. Dejar de editar la copia de Hermes.

## Decisiones de Zeta (no cambiar sin preguntar)

Están en `docs/ESTADO.md` ("Decisiones de Zeta"). Las más importantes: no saltarse antibot ni captcha (robots.txt no se mira desde el 16/11/2026, salvo en Chollometro: `respetar_robots` y `robots_siempre`); formato móvil de 5 mensajes; nada sin contraste real (mercado, histórico de PCComTracker o histórico propio de 14+ días); viajes apagados desde el 16/11/2026; crear o cambiar tareas programadas solo con autorización explícita.

## Análisis de Instinct (16/11/2026)

`docs/SPEC.md` une el análisis del bot de Instinct (chat, base de datos y 5.627 hilos de Chollometro) con este proyecto. Léelo antes de proponer cambios de filtros, tiendas o base de datos. Resumen:

- El reglamento escrito para Instinct no se copia aquí: el vigilante ya hace casi todo y es más estricto.
- Hay 8 conflictos con "Decisiones de Zeta" (apartado 4 del SPEC). Mientras Zeta no decida, manda `docs/ESTADO.md`. No los resuelvas por tu cuenta.
- Los cambios del apartado 5 están propuestos, no autorizados.

## Siguiente trabajo propuesto (16/11/2026)

Orden del apartado 5 de `docs/SPEC.md`: resultados de lo publicado (`publicadas`), demanda por tasa, cobertura de Amazon, MediaMarkt, AliExpress y Miravia, cambios de precio con fecha, recopilaciones y reglas de tallas en el dossier. Después:

- Partir `src/vigilante.py` (~3.400 líneas) en módulos. Las pruebas sustituyen funciones del módulo (`v.descargar`, `v.extraer`, `v.Navegador`, `v.enviar_telegram`...), así que hay que hacerlo por fases y ajustar esas sustituciones en cada paso.
- Cobertura: ~45 % de lo que se calienta en Chollometro es Amazon y AliExpress, y el vigilante apenas los lee. Opciones: sesión en las ventas privadas (`login`), Keepa (de pago) o CamelCamelCamel (mirar robots.txt), aprender de las publicaciones de Zeta.
