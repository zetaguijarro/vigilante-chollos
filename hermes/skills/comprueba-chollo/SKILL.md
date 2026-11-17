---
name: comprueba-chollo
description: Cuando llega "comprueba N", abre la ficha de la tienda de un chollo que el vigilante no ha podido leer (PcComponentes da 403 al script), apunta precio y stock y deja que el código decida. Tarea corta.
---

# Comprueba chollo

El vigilante no ha podido abrir la ficha del candidato #N: la tienda bloquea al script. Tú sí puedes con tu navegador. Tu único trabajo es **leer tres datos y pasárselos al código**. No compares precios, no busques en otras webs, no apliques filtros ni redactes nada salvo que el paso 3 te lo pida: el código decide.

PY = C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe
V = C:\Users\Zeus\proyectos\vigilante-chollos\src\vigilante.py

## 1. Abre la ficha
El enlace está en el aviso de Telegram al que responde este mensaje. Si no lo ves, ejecuta `PY V dossier N` y usa el campo `u` (solo ese).

Ábrela con tu navegador y comprueba que el producto es el del aviso. Si es otro producto, o la página no carga: responde "#N sin validar: motivo" y para.

## 2. Pasa los datos al código
Apunta, tal como los ves en la ficha:
- **precio**: el precio actual que pagaría cualquiera, sin cupones personales ni suscripciones.
- **stock**: `si` o `no`.
- **vendedor**: quién lo vende (por ejemplo, `PcComponentes` o el nombre del marketplace).

Ejecuta: `PY V decidir N --precio 30,00 --stock si --vendedor PcComponentes`

Devuelve una línea JSON con `veredicto`.

## 3. Según el veredicto
- **descartado**: el script ya ha mandado el motivo a Telegram. Responde solo "#N comprobado".
- **pasa**: sigue la habilidad `redacta-chollo` desde su paso 1 con el mismo N (redaccion, entrega_N.json con título y descripción, entregar). Responde "#N enviado".
- **sin_validar**: responde "#N sin validar" y el motivo.

Si el vendedor no es la propia tienda y es un marketplace con pocas valoraciones, añade al final de tu respuesta "Vendedor de marketplace: revísalo antes de publicar".
