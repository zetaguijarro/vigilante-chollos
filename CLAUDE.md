# Vigilante de chollos (contexto para Claude Code)

Proyecto personal de Zeta: vigilante de ofertas para Chollometro (perfil PorLa_Jeta). Lee primero `README.md` (estructura y comandos) y `docs/ESTADO.md` (historial de decisiones, estado por tienda y lo aprendido; está escrito con las rutas antiguas de la carpeta de Hermes).

Habla con Zeta en español, directo y con sinceridad. Prioridad: **gastar los mínimos tokens posibles** sin empeorar la calidad. Todo lo que se pueda hacer con código, con código.

## Entorno

- Python de Hermes (sin PyYAML; con Playwright y Chromium). `python` no está en el PATH:
  `C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe`
- Pruebas: `& $py -m unittest discover -s tests`. Son 193 y deben pasar siempre. Cada cambio lleva su prueba.
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

1. **Fusionar la rama `comparar-mejor`** si no está en `main` (`git pull origin comparar-mejor`; 187 pruebas). Trae: precio habitual propio (`habitual_propio`, con la tabla `precios`; da resultados hacia el 24/11), mercado propio por EAN (`mercado_propio`) y enlaces fijos en Amazon y AliExpress (`url_canonica`: `/dp/ASIN` e `/item/<id>.html`). Mirar con los datos reales que no salgan avisos raros (un EAN mal leído que case dos productos distintos).
2. **HECHO (17/11, noche; ver `docs/ESTADO.md`). Falta medirlo en `data/recorrido.txt` unos días.** **Miravia y AliExpress con Chrome visible. Autorizado por Zeta**, con las condiciones de Idealo (`docs/ESTADO.md`, "Decisiones de Zeta"): Chrome visible normal, sin perfil disfrazado ni ocultar la automatización, pocas páginas por ronda. **Si sale reto o captcha no se resuelve:** pausa fija de 30 min, siempre la misma aunque se repita (decisión de Zeta: sin pausas que se doblen). Primero probar a mano con una página de ofertas de cada una: cuántos productos da y si sale reto. Si funciona, añadirlo a las rondas y medirlo en `data/recorrido.txt`. Hoy: Miravia da 17 productos por descarga (el resto carga con JavaScript) y 0 con el Chrome oculto; AliExpress está en pausa por antibot.
3. **Cupones de Miravia y AliExpress.** Zeta: "dan muy buenos resultados con los cupones". Cuando el punto 2 funcione, mirar cómo pinta cada web los cupones en las tarjetas de producto ("cupón de 3 €", "-10 % con cupón") y si se pueden leer del listado. Diseño propuesto, pendiente de que Zeta diga de dónde saca él los códigos (generales de la tienda, los de cada tarjeta o los dos):
    - Lista de cupones activos por tienda en `tiendas.json` (código, descuento en € o %, compra mínima, fecha de fin), rellenada a mano o desde los hilos calientes de cupones de esa tienda en la copia de Chollometro.
    - En la ronda, a cada producto se le aplica el mejor cupón que le sirva y se compara con el precio final. El historial (`precios`) guarda el precio **sin** cupón, para no ensuciar el habitual.
    - Un cupón nuevo hace revisar los productos de esa tienda aunque su precio no haya cambiado (es cuando salen los chollos).
    - Aviso y ficha: "con el código X" y el precio sin código. Chollometro no admite códigos de un solo uso ni personales.
4. **Amazon no da resultados.** Ver la última entrada de `docs/ESTADO.md` sobre Amazon. Mirar con `grep amazon data/recorrido.txt | tail -n 20` qué lee. Probar `/gp/movers-and-shakers/<categoría>` y `/deals` en vez de (o además de) los más vendidos. Comprobar si `https://es.camelcamelcamel.com/product/<ASIN>` se puede leer (`asin_de` saca el ASIN) y qué datos de precio da: sería el histórico de Amazon, como PCComTracker para PcComponentes.

5. **Falso 2x1 (ya arreglado en `comparar-mejor`).** Un "Aire acondicionado Split 2x1" saltó como oferta 2x1. Ahora en el **título** un NxM solo cuenta si va con palabra de promo (oferta, llévate, paga, gratis...) y se ignora junto a palabras técnicas (split, HDMI, cable, regleta, puertos...). La **etiqueta de la tarjeta** ("2x1", "3x2") sigue contando: Zeta avisa de que muchas veces la promo viene ahí y no en el título. Hay prueba de las dos cosas. Si se escapa una promo real, pedir a Zeta la tarjeta y ajustar `RE_NXM_TECNICO` / `RE_NXM_PROMO`.
6. **Modos del navegador (comando `modos`, en `comparar-mejor`).** `& $py src\vigilante.py modos miravia.es aliexpress.com mediamarkt.es` lee la misma página con 4 modos: oculto antiguo (Chromium de Playwright), oculto nuevo (Chrome sin ventana), visible (lo que usan las rondas: `"visible": true`, `"canal": "chrome"`) y visible con imágenes (`bloquear_recursos` false). Saca productos, segundos, KB y reto, y lo apunta en `data/modos.txt`. Con un reto no prueba más modos de esa tienda (pausa fija de 30 min). Lanzarlo fuera de una ronda de navegador (mismo perfil y cerrojo). Con el resultado, decidir por tienda: si "visible+imágenes" da muchos más productos, añadir un `bloquear_recursos` por tienda; si "oculto nuevo" iguala al visible, no cambiar nada (el portátil es solo para esto y a Zeta le da igual que se abran ventanas). Si "oculto antiguo" da ERROR, falta ese Chromium: no importa.
7. **Tarea programada del navegador**: debe estar en "Ejecutar solo cuando el usuario haya iniciado sesión" (si no, Chrome visible no tiene escritorio y falla) y el portátil sin suspenderse. Comprobarlo con Zeta; las tareas solo las cambia él.

## Dónde lo dejamos (17/11/2026, 09:15)

Contexto: Zeta pasa a trabajar con Claude Code en VS Code para que Claude ejecute él mismo los comandos. Lo de ayer y hoy está en `docs/ESTADO.md` (cada cambio con su fecha) y en `docs/SPEC.md`. Lo que sigue, por orden:

1. **Comprobar que la última rama está en `main`.** Lo último subido es la rama `comprueba` (166 pruebas). Si `git log -1` en `main` no la incluye, falta `git pull origin comprueba`.
2. **Probar el botón "Verificar" con Hermes de verdad.** Nada del lado de Hermes está probado en vivo: el plugin `vigilante-botones` (lanza `decidir N` y, según el veredicto, no despierta a Hermes, le manda "redacta N" o "comprueba N") y las habilidades `redacta-chollo` y `comprueba-chollo`. Caso de prueba: el **#189** de PcComponentes (30,00 € con habitual de 39,99 € en PCComTracker) tiene que salir DESTACADOS por la vía "comprueba". Si el plugin falla, cae a "verifica N", el camino antiguo.
3. **Tres ofertas "buena" del 16/11 sin publicar** (PcComponentes, en la tabla `modelos`): Zeta reinició y no llegó a verlas. Comprobarlas con "comprueba N" y, si siguen, que las publique. Serán el primer dato real de resultados.
4. **Ventas privadas (Privé, Showroomprivé, Privalia, Private Sport Shop).** Llegó un aviso de "sesión caducada" a las 08:26. Sospecha: es falso; la sesión está iniciada pero la portada no enseña productos, solo carteles de campañas. Comprobar con `volcar privalia.com zalando-prive.es showroomprive.es privatesportshop.es`. Si la página es grande y sin formulario de acceso, hay que leer las campañas de dentro en vez de la portada.
5. **MediaMarkt lee poco** (16 % de lo que triunfa). Campañas por descarga: unos 13 productos (el resto carga con JavaScript al hacer scroll). Ya se leen 2 categorías de catálogo cada 10 min; las de direcciones con tildes fallaban y se arregló (`url_ascii`). Falta: `catalogo mediamarkt.es` con la versión que baja un nivel (subcategorías, hasta 40) y mirar en `data/recorrido.txt` cuántos productos salen. Si sigue siendo poco, probar MediaMarkt con navegador una tarde, con vuelta atrás si sale muro.
6. **Radar**: `precioreal.com` ya se lee pero da 0 productos. Mirar con `volcar precioreal.com`.
7. **Pendiente de revisar a mano**: la página de ofertas guardada de IKEA (una categoría de armarios), Dyson (3 productos) y DeLonghi (una categoría, no ofertas). Se corrigen en `data/urls_ofertas.json`.
8. **Navegador a 4 pestañas y 3 páginas** desde hoy (portátil dedicado: 16 GB, i5-8265U). Vigilar en el diario que la ronda de navegador no pase de 14 min ni suban las lecturas vacías; si pasa, bajar primero las páginas.

Herramientas para mirar sin tocar: `data/recorrido.txt` (diario por tienda y ronda), `data/log.txt`, `prueba <categoría>`, `volcar <tienda>`, `informe 1`, `decidir N --callado` (decide sin mandar nada).

Cerrojo del navegador: si `data/navegador.lock` existe y su proceso no (`tasklist //FI "PID eq <pid>"`), está huérfano y bloquea las rondas de navegador y el `login`: se borra. Pasó el 16/11.

Recomendación que se le dio a Zeta y conviene respetar: tras tantos cambios en dos días, dejar correr el sistema y medir (publicadas, grados, rechazos) antes de tocar filtros.

## Siguiente trabajo propuesto (16/11/2026)

Orden del apartado 5 de `docs/SPEC.md`: resultados de lo publicado (`publicadas`), demanda por tasa, cobertura de Amazon, MediaMarkt, AliExpress y Miravia, cambios de precio con fecha, recopilaciones y reglas de tallas en el dossier. Después:

- Partir `src/vigilante.py` (~3.400 líneas) en módulos. Las pruebas sustituyen funciones del módulo (`v.descargar`, `v.extraer`, `v.Navegador`, `v.enviar_telegram`...), así que hay que hacerlo por fases y ajustar esas sustituciones en cada paso.
- Cobertura: ~45 % de lo que se calienta en Chollometro es Amazon y AliExpress, y el vigilante apenas los lee. Opciones: sesión en las ventas privadas (`login`), Keepa (de pago) o CamelCamelCamel (mirar robots.txt), aprender de las publicaciones de Zeta.
