---
name: redacta-chollo
description: Redacta el título y la descripción de un chollo que el vigilante YA ha verificado por código, cuando llega "redacta N". Tarea corta, sin navegar ni buscar.
---

# Redacta chollo

El vigilante ya ha leído la ficha y ha aplicado los filtros por código: el candidato #N **pasa**. Precios, información, enlace, foto y etiqueta ya están hechos. Tú solo escribes dos textos. No verifiques nada, no abras ninguna página, no busques: gastarías tokens para repetir lo que ya está comprobado.

PY = C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe
V = C:\Users\Zeus\proyectos\vigilante-chollos\src\vigilante.py

## 1. Datos
Ejecuta `PY V redaccion N`. Devuelve una línea JSON con: `nombre`, `etiqueta`, `tallas`, `specs`, `resumen` y `titulo_ficha`.

Si devuelve `error`, responde "#N: no hay decisión guardada" y para.

## 2. Escribe el archivo
Escribe C:\Users\Zeus\proyectos\vigilante-chollos\data\entrega_N.json, en UTF-8, con solo estas dos claves:

- "titulo": título sobrio, de 140 caracteres como mucho: marca, modelo y especificación clave. En ropa y calzado, las tallas de `tallas`. Sin precio, sin tienda, sin emojis y sin etiqueta: la etiqueta la pone el script.
- "descripcion": en dos partes y sin encabezados.
  1. Un párrafo de 4 a 6 frases: qué es, para quién es, qué lo distingue y para qué sirve.
  2. Una línea en blanco y entre 6 y 10 características, una por línea, empezando por "• ".

Usa solo `specs`, `resumen` y lo que dice el propio `nombre`. Nada inventado ni tomado de un modelo parecido; si hay menos de 6 características, pon las que haya. Sin precio, sin tienda, sin afirmaciones médicas ni de durabilidad.

## 3. Entrega
Ejecuta `PY V entregar N`. El script añade la etiqueta, los precios, la información y el enlace, y manda los 5 mensajes.

Tu respuesta final es solo: "#N enviado".
