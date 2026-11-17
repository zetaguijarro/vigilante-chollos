---
name: redacta-chollo
description: Redacta el título y la descripción de un chollo que el vigilante YA ha verificado por código, cuando llega "redacta N". Tarea corta, sin navegar ni buscar.
---

# Redacta chollo

El vigilante ya ha leído la ficha y ha aplicado los filtros por código: el candidato #N **pasa**. Precios, información, enlace, foto y etiqueta ya están hechos. Tú solo escribes dos textos. No verifiques nada, no abras ninguna página, no busques: gastarías tokens para repetir lo que ya está comprobado.

PY = C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe
V = C:\Users\Zeus\proyectos\vigilante-chollos\src\vigilante.py

## 1. Datos
Ejecuta `PY V redaccion N`. Devuelve una línea JSON con: `nombre`, `etiqueta`, `tallas`, `specs`, `resumen`, `titulo_ficha` y `cupon` (texto del cupón que hace falta para el precio, o null).

Si devuelve `error`, responde "#N: no hay decisión guardada" y para.

## 2. Escribe el archivo
Escribe C:\Users\Zeus\proyectos\vigilante-chollos\data\entrega_N.json, en UTF-8, con solo estas dos claves:

- "titulo": 140 caracteres como mucho: marca, modelo comercial y características clave. Sin precio, sin tienda, sin código alfanumérico interno, sin emojis y sin etiqueta (la etiqueta va en la INFO, la pone el script).
  - Ropa y calzado: todas las tallas de `tallas` y su variante (color). Rango ("36 a 40") solo si están todas las intermedias; si hay huecos, enuméralas ("36, 37⅓, 38 y 40"). Si no caben en 140 caracteres, no las acortes ni las conviertas en rango: responde "#N espera: no caben las tallas en el título" y para.
  - Videojuegos: nombre completo, edición y plataforma. No pongas "digital"; el formato físico sí se indica.
  - Viajes: fechas y duración.
  - Si `cupon` no es null, el código hace falta para el precio: termina el título con "con cupón CÓDIGO" (el código que aparece en `cupon`). Es la única excepción a "sin código alfanumérico".
- "descripcion": solo producto, en bloques. Cada bloque empieza con un encabezado con un emoji y el texto entre dobles asteriscos (el script lo convierte en negrita real), pegado a sus viñetas "• ". Una línea en blanco entre bloques. Pocos emojis, solo en los encabezados. Ejemplo:

  🧥 **Diseño y materiales**
  • Corte clásico, capucha y bolsillos laterales.
  • 100% poliéster reciclado.

  📏 **Tallas disponibles**
  • M, L y XL en color gris.

  Bloques útiles: características (o diseño y materiales), especificaciones, tallas disponibles (ropa y calzado). Composición de ropa y calzado solo si viene en `specs` de esta variante; si no, no la pongas. En electrónica, solo especificaciones de este modelo.

Usa solo `specs`, `resumen` y lo que dice el propio `nombre`. Nada inventado ni tomado de un modelo parecido; si hay pocas características, pon las que haya. Sin precios, ahorro, históricos, comparadores, tienda, notas de comprobación, bromas ni consejos para combinar ropa. Sin afirmaciones médicas ni de durabilidad. No escribas "Recogida en tienda GRATIS" (no está verificada).

## 3. Entrega
Ejecuta `PY V entregar N`. El script añade la foto, el enlace, el precio y la INFO ADICIONAL, y manda los 6 mensajes en orden: foto, título, enlace, precio, INFO y descripción.

Tu respuesta final es solo: "#N enviado".
