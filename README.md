# Vigilante de chollos

Busca ofertas para publicar en Chollometro (perfil PorLa_Jeta). Todo el filtrado se hace con código y sin IA, así que no gasta tokens. Hermes solo interviene cuando pulsas «Verificar #N» en Telegram.

## Cómo funciona

1. **Ronda (código, 0 €).** Lee las tiendas, detecta lo nuevo o lo que baja de precio y aplica los filtros: marca, descuento, precio de mercado (PrecioReal), histórico (PCComTracker o histórico propio) y duplicados en Chollometro. Lo que pasa llega a Telegram con un botón «Verificar #N».
2. **Verificación (Hermes).** El botón manda «verifica N» a Hermes. La habilidad `verifica-chollo` ejecuta `dossier N` (ficha, stock, histórico…), la IA da el veredicto y redacta, y `entregar N` envía la ficha en 5 mensajes.
3. **Programación.** El Programador de tareas de Windows lanza `tareas/vig_rapidas.py` cada 5 min y `tareas/vig_navegador.py` cada 15 min, con `pythonw.exe` (sin ventana).

## Estructura

```
vigilante-chollos/
├── src/vigilante.py          motor: lectura, filtros, Chollometro, Telegram, dossier
├── config/
│   ├── tiendas.json          tiendas por categoría, filtros, apartadas, horario, entrega
│   └── marcas.txt            marcas admitidas (tecnología, generalistas, móviles)
├── data/                     base de datos, cachés y perfil de Chrome (fuera de git)
├── tests/test_vigilante.py   160 pruebas, sin red real
├── tareas/                   lo que lanza el Programador de tareas (lo genera `wrappers`)
├── hermes/
│   ├── skills/verifica-chollo/SKILL.md   habilidad «verifica N»
│   └── plugins/vigilante-botones/        botón «Verificar» de Telegram
└── docs/
    ├── ESTADO.md             historial de decisiones y estado (antiguo CLAUDE.md)
    ├── SPEC.md               análisis de Instinct, conflictos y cambios propuestos
    ├── LECCIONES_INSTINCT.md reglas aprendidas del chat con Instinct
    └── prompt_vigilante.md   formato compacto de candidatos para la IA
```

## Puesta en marcha

Solo usa la librería estándar de Python más Playwright con Chromium (para las tiendas que se leen con navegador). Sirve el Python de Hermes:

```powershell
$py = "C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe"
cd C:\Users\Zeus\proyectos\vigilante-chollos
& $py -m unittest discover -s tests      # deben pasar todas
```

El token de Telegram se lee de la variable `VIGILANTE_TELEGRAM_TOKEN` o `TELEGRAM_BOT_TOKEN`, o del `.env` de Hermes. Nunca va en este repositorio.

## Comandos

```powershell
& $py src\vigilante.py <categoría>            # una ronda de esa categoría
& $py src\vigilante.py rapidas | navegador    # ronda de todas las categorías (lo que programan las tareas)
& $py src\vigilante.py prueba <categoría>     # diagnóstico por tienda, no guarda nada
& $py src\vigilante.py informe [días]         # qué aporta cada tienda
& $py src\vigilante.py descubrir <categoría>  # busca la página de ofertas de cada tienda
& $py src\vigilante.py catalogo [cats|tiendas]# categorías normales de cada tienda
& $py src\vigilante.py volcar <tienda> ...    # por qué una tienda da 0 productos
& $py src\vigilante.py login <tienda> ...     # abre la tienda para que inicies sesión
& $py src\vigilante.py chollometro [días] [páginas]  # primera carga de la copia de Chollometro
& $py src\vigilante.py dossier N              # datos para «verifica N» (lo usa Hermes)
& $py src\vigilante.py recordar N buena|dudosa|caducada <motivo>
& $py src\vigilante.py entregar N             # envía la ficha verificada
& $py src\vigilante.py wrappers               # regenera tareas/
```

## Rutas configurables

| Variable | Por defecto | Para qué |
|---|---|---|
| `VIGILANTE_HOME` | la raíz del proyecto | Dónde están `config/` y `tareas/` |
| `VIGILANTE_DATOS` | `<raíz>/data` | Base de datos y cachés |
| `HERMES_HOME` | `%LOCALAPPDATA%\hermes` | Dónde buscar el `.env` de Hermes |

## Estado frente a la instalación de Hermes

La versión **en marcha** sigue en `C:\Users\Zeus\AppData\Local\hermes\scripts`: es la que lanzan las tareas programadas y la que usa la habilidad de Hermes. Este proyecto es una copia ordenada del mismo código (16/11/2026). Para que pase a ser el que funciona, ver «Pasar a producción» en `CLAUDE.md`.

## Reglas que no se rompen

- No se salta ningún antibot ni captcha. robots.txt no se mira desde el 16/11/2026 por decisión de Zeta (`filtros_codigo.respetar_robots`), salvo en los hosts de `robots_siempre`. Las tiendas bloqueadas van a `apartadas` con su motivo.
- No se compra, no se reserva y no se mete nada en carritos.
- Cambios de filtros: siempre con su prueba en `tests/`.
