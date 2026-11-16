# data/

Carpeta de trabajo del vigilante. No se sube a git (ver `.gitignore`).

Qué se crea aquí:

- `vistos.db`: base SQLite (productos vistos, avisos enviados, veredictos, copia local de Chollometro, estadísticas).
- `log.txt`: registro de rondas y errores.
- `robots.json`: caché de 24 h de los robots.txt.
- `urls_ofertas.json`: página de ofertas de cada tienda (la genera `descubrir`).
- `urls_catalogo.json`: categorías de cada tienda (la genera `catalogo`).
- `perfil/`: perfil de Chrome del navegador automático, con las sesiones de las tiendas con login.

Para usar los datos que ya tiene la instalación de Hermes en vez de esta carpeta:

    $env:VIGILANTE_DATOS = "C:\Users\Zeus\AppData\Local\hermes\scripts\vigilante_data"
