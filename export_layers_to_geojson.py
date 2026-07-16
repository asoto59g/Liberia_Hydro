from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape


def export_perimeter(perim_path: Path, out_path: Path, to_crs: str = "EPSG:4326") -> None:
    gdf = gpd.read_file(perim_path)
    if gdf.empty:
        raise ValueError("El perímetro está vacío.")
    if gdf.crs is None:
        raise ValueError("perim.shp no tiene CRS. Definí CRS antes de exportar.")
    gdf = gdf.to_crs(to_crs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="GeoJSON", encoding="utf-8")


def polygonize_flood_raster(
    flood_raster: Path,
    out_path: Path,
    min_depth: float = 0.005,
    to_crs: str = "EPSG:4326",
    simplify_tolerance: float = 0.0,
) -> None:
    with rasterio.open(flood_raster) as src:
        arr = src.read(1).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)

        mask = np.isfinite(arr) & (arr >= min_depth)
        if not np.any(mask):
            gdf_empty = gpd.GeoDataFrame({"depth": []}, geometry=[], crs=src.crs)
            gdf_empty = gdf_empty.to_crs(to_crs)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            gdf_empty.to_file(out_path, driver="GeoJSON", encoding="utf-8")
            return

        geoms = []
        vals = []
        for geom, value in shapes(arr, mask=mask, transform=src.transform):
            if not np.isfinite(value):
                continue
            if value < min_depth:
                continue
            geoms.append(shape(geom))
            vals.append(float(value))

    gdf = gpd.GeoDataFrame({"depth": vals}, geometry=geoms, crs=src.crs)
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()

    if simplify_tolerance > 0:
        gdf["geometry"] = gdf.geometry.simplify(simplify_tolerance, preserve_topology=True)

    gdf = gdf.to_crs(to_crs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="GeoJSON", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Exporta perímetro y zonas inundadas a GeoJSON para visor web")
    parser.add_argument("--perim", type=str, required=True, help="Ruta a perim.shp")
    parser.add_argument("--flood-raster", type=str, required=True, help="Raster de profundidad (ej. depth_0290.tif)")
    parser.add_argument("--out-dir", type=str, default="./web/data", help="Directorio de salida GeoJSON")
    parser.add_argument("--min-depth", type=float, default=0.005, help="Umbral de profundidad para inundación")
    parser.add_argument("--to-crs", type=str, default="EPSG:4326", help="CRS final para web")
    parser.add_argument("--simplify", type=float, default=0.0, help="Tolerancia de simplificación geométrica")
    args = parser.parse_args()

    perim = Path(args.perim).resolve()
    flood_raster = Path(args.flood_raster).resolve()
    out_dir = Path(args.out_dir).resolve()

    if not perim.exists():
        raise FileNotFoundError(f"No existe perim: {perim}")
    if not flood_raster.exists():
        raise FileNotFoundError(f"No existe flood-raster: {flood_raster}")

    perim_out = out_dir / "perim.geojson"
    flood_out = out_dir / "flood_zones.geojson"

    export_perimeter(perim, perim_out, to_crs=args.to_crs)
    polygonize_flood_raster(
        flood_raster=flood_raster,
        out_path=flood_out,
        min_depth=args.min_depth,
        to_crs=args.to_crs,
        simplify_tolerance=args.simplify,
    )

    print(f"[OK] Perímetro exportado: {perim_out}")
    print(f"[OK] Zonas inundadas exportadas: {flood_out}")


if __name__ == "__main__":
    main()