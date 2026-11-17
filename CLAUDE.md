# Vigilante de chollos (contexto para Claude Code)

Proyecto personal de Zeta: vigilante de ofertas para Chollometro (perfil PorLa_Jeta). Lee primero `README.md` (estructura y comandos) y `docs/ESTADO.md` (historial de decisiones, estado por tienda y lo aprendido; está escrito con las rutas antiguas de la carpeta de Hermes).

Habla con Zeta en español, directo y con sinceridad. Prioridad: **gastar los mínimos tokens posibles** sin empeorar la calidad. Todo lo que se pueda hacer con código, con código.

## Entorno

- Python de Hermes (sin PyYAML; con Playwright y Chromium). `python` no está en el PATH:
  `C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe`
- Pruebas: `& $py -m unittest discover -s tests`. Son 198 y deben pasar siempre. Cada cambio lleva su prueba.
- En PowerShell 5.1 los `python -c` con comillas fallan: usa scripts en el scratchpad.
- git: `C:\Program Files\Git\cmd\git.exe` (instalado el 16/11/2026). Remoto privado en GitHub: https://github.com/zetaguijarro/vigilante-chollos (`main` sigue a `origin/main`, push con la credencial guardada de Windows).

## En producción desde el 16/11/2026 (noche)

- **Lo que funciona es este proyecto.** Las seis tareas "Vigilante rapidas/navegador" (y sus variantes 0001 y 0901) lanzan `tareas\vig_rapidas.py` y `tareas\vig_navegador.py` de esta carpeta, con los datos en `data\`. Un cambio aquí afecta a lo que está en marcha desde la siguiente ronda: pasa las pruebas antes.
- La carpeta antigua `C:\Users\Zeus\AppData\Local\hermes\scripts` queda solo como copia de seguridad. No se edita ni se usa.
- Hermes: las habilidades y el plugin del botón viven en `hermes\` de este proyecto y se instalan con `bash scripts/instalar_en_hermes.sh`. Después hay que **reiniciar Hermes**, que los carga al arrancar. Eso lo hace Zeta.
- Crear, cambiar o desactivar tareas programadas: solo Zeta (el sistema de permisos lo bloquea a Claude). `scripts\pasar_a_produccion.ps1` ya se lanzó; sirve de referencia si hubiera que repetirlo.
- Rutina de Zeta tras un cambio: `git pull origin <rama>`, pruebas, `git push origin main` y, si cambió algo de `hermes\`, el instalador y reiniciar Hermes.

## Decisiones de Zeta (no cambiar sin preguntar)

Están en `docs/ESTADO.md` ("Decisiones de Zeta"). Las más importantes: no saltarse antibot ni captcha (robots.txt no se mira en ninguna web: desde el 16/11/2026 y, desde el 17/11/2026, tampoco en Chollometro; `respetar_robots` false y `robots_siempre` vacío); formato de 6 mensajes de `docs/FORMATO.md` (desde el 17/11/2026); nada sin contraste real (mercado, histórico de PCComTracker o histórico propio de 14+ días); viajes apagados desde el 16/11/2026; crear o cambiar tareas programadas solo con autorización explícita.

## Análisis de Instinct (16/11/2026)

`docs/SPEC.md` une el análisis del bot de Instinct (chat, base de datos y 5.627 hilos de Chollometro) con este proyecto. Léelo antes de proponer cambios de filtros, tiendas o base de datos. Resumen:

- El reglamento escrito para Instinct no se copia aquí: el vigilante ya hace casi todo y es más estricto.
- Hay 8 conflictos con "Decisiones de Zeta" (apartado 4 del SPEC). Mientras Zeta no decida, manda `docs/ESTADO.md`. No los resuelvas por tu cuenta.
- Los cambios del apartado 5 están propuestos, no autorizados.

## Encargos de Zeta (17/11/2026, noche)

Pedidos desde Claude en el móvil. Por orden:

1. **HECHO (17/11, noche): `comparar-mejor` ya está en `main`.** Trae: precio habitual propio (`habitual_propio`, con la tabla `precios`; da resultados hacia el 24/11), mercado propio por EAN (`mercado_propio`) y enlaces fijos en Amazon y AliExpress (`url_canonica`: `/dp/ASIN` e `/item/<id>.html`). Mirar con los datos reales que no salgan avisos raros (un EAN mal leído que case dos productos distintos).
2. **HECHO (17/11, noche; ver `docs/ESTADO.md`). Falta medirlo en `data/recorrido.txt` unos días.** **Miravia y AliExpress con Chrome visible. Autorizado por Zeta**, con las condiciones de Idealo (`docs/ESTADO.md`, "Decisiones de Zeta"): Chrome visible normal, sin perfil disfrazado ni ocultar la automatización, pocas páginas por ronda. **Si sale reto o captcha no se resuelve:** pausa fija de 30 min, siempre la misma aunque se repita (decisión de Zeta: sin pausas que se doblen). Primero probar a mano con una página de ofertas de cada una: cuántos productos da y si sale reto. Si funciona, añadirlo a las rondas y medirlo en `data/recorrido.txt`. Hoy: Miravia da 17 productos por descarga (el resto carga con JavaScript) y 0 con el Chrome oculto; AliExpress está en pausa por antibot.
3. **Cupones de Miravia y AliExpress.** Zeta: "dan muy buenos resultados con los cupones". Cuando el punto 2 funcione, mirar cómo pinta cada web los cupones en las tarjetas de producto ("cupón de 3 €", "-10 % con cupón") y si se pueden leer del listado. Diseño propuesto, pendiente de que Zeta diga de dónde saca él los códigos (generales de la tienda, los de cada tarjeta o los dos):
    - Lista de cupones activos por tienda en `tiendas.json` (código, descuento en € o %, compra mínima, fecha de fin), rellenada a mano o desde los hilos calientes de cupones de esa tienda en la copia de Chollometro.
    - En la ronda, a cada producto se le aplica el mejor cupón que le sirva y se compara con el precio final. El historial (`precios`) guarda el precio **sin** cupón, para no ensuciar el habitual.
    - Un cupón nuevo hace revisar los productos de esa tienda aunque su precio no haya cambiado (es cuando salen los chollos).
    - Aviso y ficha: "con el código X" y el precio sin código. Chollometro no admite códigos de un solo uso ni personales.
4. **Amazon: HECHO (17/11, noche; ver `docs/ESTADO.md`).** El tachado de `/deals` con etiqueta "Mediano" o "Más bajo" cuenta como histórico ("Recomendado" no); CamelCamelCamel en las rondas (Chrome normal, 8 por ronda, caché 24 h). `movers-and-shakers` no existe en Amazon España. Falta medir en `data/recorrido.txt` cuántas candidatas de Amazon salen. `/deals` solo da 30 productos por lectura.

5. **Falso 2x1 (HECHO, en `main`).** Un "Aire acondicionado Split 2x1" saltó como oferta 2x1. Ahora en el **título** un NxM solo cuenta si va con palabra de promo (oferta, llévate, paga, gratis...) y se ignora junto a palabras técnicas (split, HDMI, cable, regleta, puertos...). La **etiqueta de la tarjeta** ("2x1", "3x2") sigue contando: Zeta avisa de que muchas veces la promo viene ahí y no en el título. Hay prueba de las dos cosas. Si se escapa una promo real, pedir a Zeta la tarjeta y ajustar `RE_NXM_TECNICO` / `RE_NXM_PROMO`.
6. **Modos del navegador (comando `modos`, rama `modos-navegador`).** Miravia y AliExpress ya funcionan con su propio `NavegadorNormal`, así que `modos` sirve sobre todo para MediaMarkt y para cualquier tienda que lea poco: `& $py src\vigilante.py modos mediamarkt.es` lee la misma página con 4 modos: oculto antiguo (Chromium de Playwright), oculto nuevo (Chrome sin ventana), visible (lo que usan las rondas: `"visible": true`, `"canal": "chrome"`) y visible con imágenes (`bloquear_recursos` false). Saca productos, segundos, KB y reto, y lo apunta en `data/modos.txt`. Con un reto no prueba más modos de esa tienda (pausa fija de 30 min). Lanzarlo fuera de una ronda de navegador (mismo perfil y cerrojo). Con el resultado, decidir por tienda: si "visible+imágenes" da muchos más productos, añadir un `bloquear_recursos` por tienda; si "oculto nuevo" iguala al visible, no cambiar nada (el portátil es solo para esto y a Zeta le da igual que se abran ventanas). Si "oculto antiguo" da ERROR, falta ese Chromium: no importa.
7. **Tarea programada del navegador**: debe estar en "Ejecutar solo cuando el usuario haya iniciado sesión" (si no, Chrome visible no tiene escritorio y falla) y el portátil sin suspenderse. Comprobarlo con Zeta; las tareas solo las cambia él.
8. **MediaMarkt: HECHO (17/11, noche; ver `docs/ESTADO.md`).** Lista fija de 36 categorías por demanda, 4 por ronda cada 10 min, por descarga (60 productos cada una). Con navegador no gana: `modos` dio 13 productos en los 4 modos. Falta medir en `data/recorrido.txt`. 8. **MediaMarkt (plan acordado con Zeta, 17/11 noche).** Problema: por descarga solo da unos 13 productos (el resto carga al hacer scroll); "Ofertas Flash" y "Ofertas" dan 1-2. Ya hecho: 2 categorías de catálogo cada 10 min, `url_ascii` para las direcciones con tildes, el "Captcha" de `volcar` es falso positivo, y PCComTracker como histórico si el modelo coincide. Por orden: (1) que Zeta pase `catalogo mediamarkt.es` otra vez (ahora `niveles: 2`, hasta 40 subcategorías; faltaban auriculares, consolas, tablets y monitores); (2) `modos mediamarkt.es` para ver cuántos productos da con Chrome y scroll; (3) decidir con datos: si Chrome da muchos más sin reto, pasarla a las rondas de navegador (cada 15 min); si no, seguir con descarga + catálogo. Mirar `data/recorrido.txt` por la tarde. Opinión dada a Zeta: con 40 categorías rotando ya gana bastante sin riesgo.

## Pendiente de la mañana del 17/11 (repasado por la noche)

Hecho: la rama `comprueba` está en `main`; el botón "Verificar" funciona con Hermes de verdad (el #197 se redactó y entregó en 6 mensajes, tras `hermes sessions discard`); navegador a 4 pestañas y 3 páginas. Sin comprobar todavía:

1. **Tres ofertas "buena" del 16/11 sin publicar** (PcComponentes, tabla `modelos`): ya serán viejas; mirar si sigue alguna.
2. **Ventas privadas** (Privalia, Private Sport Shop; Privé y Showroomprivé están apartadas): aviso de "sesión caducada" probablemente falso. Comprobar con `volcar privalia.com privatesportshop.es`.
3. **Radar**: `precioreal.com` da 0 productos. Mirar con `volcar precioreal.com`.
4. **Revisar a mano** las páginas guardadas de IKEA, Dyson y DeLonghi en `data/urls_ofertas.json`.
5. **Vigilar** que la ronda de navegador no pase de 14 min (ahora también abre el Chrome normal de Miravia, AliExpress y CamelCamelCamel).

Herramientas para mirar sin tocar: `data/recorrido.txt` (diario por tienda y ronda), `data/log.txt`, `prueba <categoría>`, `volcar <tienda>`, `informe 1`, `decidir N --callado` (decide sin mandar nada).

Cerrojo del navegador: si `data/navegador.lock` existe y su proceso no (`tasklist //FI "PID eq <pid>"`), está huérfano y bloquea las rondas de navegador y el `login`: se borra. Pasó el 16/11.

Recomendación que se le dio a Zeta y conviene respetar: tras tantos cambios en dos días, dejar correr el sistema y medir (publicadas, grados, rechazos) antes de tocar filtros.

## Siguiente trabajo propuesto (16/11/2026)

Orden del apartado 5 de `docs/SPEC.md`: resultados de lo publicado (`publicadas`), demanda por tasa, cobertura de Amazon, MediaMarkt, AliExpress y Miravia, cambios de precio con fecha, recopilaciones y reglas de tallas en el dossier. Después:

- Partir `src/vigilante.py` (~3.400 líneas) en módulos. Las pruebas sustituyen funciones del módulo (`v.descargar`, `v.extraer`, `v.Navegador`, `v.enviar_telegram`...), así que hay que hacerlo por fases y ajustar esas sustituciones en cada paso.
- Cobertura: ~45 % de lo que se calienta en Chollometro es Amazon y AliExpress, y el vigilante apenas los lee. Hecho el 17/11: Miravia y AliExpress con Chrome normal y búsquedas por familia; Amazon con CamelCamelCamel y las etiquetas del tachado. Keepa (de pago) queda como opción si Amazon sigue corto.
