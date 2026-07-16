# Simulación y visualización de zonas inundadas - Liberia, CR

Visor geografico link:  https://asoto59g.github.io/Liberia_Hydro/

Proyecto para:
- Preprocesar insumos geoespaciales (DEM, perímetro y rugosidad de Manning)
- Generar capas hidráulicas derivadas (slope, flow direction, flow accumulation, velocidad y caudal)
- Crear frames PNG de una inundación sintética basada en potencial de anegamiento
- Construir un video MP4
- Publicar un visor geográfico con:
  - Fondo Google Satellite Hybrid
  - Perímetro del área de estudio en rojo
  - Zonas inundadas en azul

## 1. Archivos utilizados

### Insumos de entrada (carpeta `input/`)
- `perim.shp` (+ `.dbf`, `.shx`, `.prj`): polígono de zona de estudio
- `dem.tif` o `dem.asc`: modelo digital de elevación
- `manning3.asc`: raster coeficiente de rugosidad de Manning

### Scripts Python
- `preprocess_hydraulic.py`
  - Recorta DEM al perímetro
  - Reproyecta/alinea Manning al grid del DEM
  - Calcula slope, aspect, flowdir D8 y flowacc
  - Calcula velocidad/caudal por Manning para profundidad fija
- `generate_flood_frames.py`
  - Construye un índice de potencial de inundación
  - Simula evolución temporal de lámina de agua
  - Renderiza frames PNG con color por velocidad
  - Exporta `metrics.csv` y opcionalmente rasters por frame

### Construcción de video
- `build_video.ps1` (usa `ffmpeg`)

Comando usado:
```powershell
.\build_video.ps1 -FramesDir "C:\git\out\anim_flood_300\frames" -OutVideo "C:\git\out\anim_flood_300\flood_anim.mp4" -Fps 30 -Crf 20 -Preset slow
