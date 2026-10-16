# Portal COMEX — MVP funcional

## Ejecutar (Windows)
1. Instala Python 3.11 o posterior (incluido Python 3.14).
2. Ejecuta `iniciar.bat` (no requiere `pip install`).
3. Abre `http://127.0.0.1:8765`.
4. Carga CSV de DIN o un padrón CSV de `RUT;Razon Social`.
5. Busca y exporta la lista de clientes.

Los datos persisten en `portal_comex.sqlite3` (se crea automáticamente). Respáldalo para conservarlos.

## Importante
- Solo reconoce columnas cuyos encabezados coinciden con alias comunes (RUT, importador, CIF, país, etc.). Debe validarse y ajustarse con el encabezado del CSV oficial real.
- Un archivo oficial sin RUT ni nombre NO permite identificar importadores; no se puede deducir esa información a partir del producto.
- El sistema nunca inventa razones sociales: se toma el nombre del CSV o del padrón importado.
- Las importaciones se registran por **fila/ítem**, no por embarque único.
- El valor monetario es una suma simple del campo detectado: revisar moneda y unidad antes de usarlo comercialmente.
- Los datos y la API son locales, vinculados a `127.0.0.1`, sin autenticación. NO publicar directamente en internet.
- Límite de archivo 350 MB; la carga utiliza memoria proporcional al CSV. Para archivos muy grandes se recomienda adaptar importación por streaming.
