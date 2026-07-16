from __future__ import annotations

import argparse
import os
from pathlib import Path
from collections import deque

import numpy as np
from pyproj import CRS
from pyproj import datadir as pyproj_datadir
from pyproj.exceptions import ProjError

_proj_data_dir = pyproj_datadir.get_data_dir()
os.environ["PROJ_LIB"] = _proj_data_dir
os.environ["PROJ_DATA"] = _proj_data_dir
os.environ.pop("GDAL_DATA", None)

import geopandas as gpd
import rasterio
from rasterio.crs import CRS as RioCRS
from rasterio.features import geometry_mask
from rasterio.mask import mask
from rasterio.warp import Resampling, reproject

try:
    import richdem as rd
    HAS_RICHDEM = True
except ImportError:
    rd = None
    HAS_RICHDEM = False

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator


def find_dem_file(input_dir: Path) -> Path:
    candidates = [input_dir / "dem.tif", input_dir / "dem.asc"]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("No se encontró DEM. Se esperaba dem.tif o dem.asc")


def validate_inputs(input_dir: Path) -> dict:
    perim = input_dir / "perim.shp"
    manning = input_dir / "manning3.asc"
    dem = find_dem_file(input_dir)

    missing = []
    if not perim.exists():
        missing.append(str(perim))
    if not manning.exists():
        missing.append(str(manning))

    if missing:
        raise FileNotFoundError(f"Faltan archivos requeridos: {missing}")

    return {"perim": perim, "dem": dem, "manning": manning}


def save_geotiff(
    out_path: Path,
    array: np.ndarray,
    crs,
    transform,
    dtype="float32",
    nodata=-9999.0,
):
    out_arr = array.astype(dtype, copy=True)
    if np.issubdtype(out_arr.dtype, np.floating):
        out_arr[np.isnan(out_arr)] = nodata

    meta = {
        "driver": "GTiff",
        "height": out_arr.shape[0],
        "width": out_arr.shape[1],
        "count": 1,
        "dtype": out_arr.dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "lzw",
    }

    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out_arr, 1)


def _parse_crs_or_fail(crs_value, label: str) -> CRS:
    try:
        return CRS.from_user_input(crs_value)
    except Exception as e:
        raise ValueError(f"CRS inválido en {label}: {crs_value}. Detalle: {e}")


def _to_rasterio_crs_compatible(pyproj_crs: CRS) -> RioCRS:
    epsg_code = pyproj_crs.to_epsg()
    if epsg_code is not None:
        try:
            return RioCRS.from_epsg(epsg_code)
        except Exception:
            pass
    return RioCRS.from_user_input(pyproj_crs.to_wkt())


def clip_dem_to_perimeter(
    dem_path: Path,
    perim_gdf: gpd.GeoDataFrame,
    perim_crs_override: str | None = None,
    dem_crs_override: str | None = None,
    assume_same_crs: bool = False,
):
    with rasterio.open(dem_path) as src:
        dem_crs_input = dem_crs_override if dem_crs_override else src.crs
        if dem_crs_input is None:
            raise ValueError("El DEM no tiene CRS definido. Usá --dem-crs EPSG:XXXX")

        dem_crs_pyproj = _parse_crs_or_fail(dem_crs_input, "DEM")
        dem_crs_rio = _to_rasterio_crs_compatible(dem_crs_pyproj)

        if perim_gdf.crs is None:
            if not perim_crs_override:
                raise ValueError("perim.shp no tiene CRS definido. Usá --perim-crs EPSG:XXXX")
            perim_gdf = perim_gdf.set_crs(perim_crs_override, allow_override=True)

        perim_crs_pyproj = _parse_crs_or_fail(perim_gdf.crs, "perim.shp")

        if assume_same_crs:
            perim_in_dem_crs = perim_gdf.set_crs(dem_crs_pyproj, allow_override=True)
        else:
            if perim_crs_pyproj != dem_crs_pyproj:
                try:
                    perim_in_dem_crs = perim_gdf.to_crs(dem_crs_pyproj)
                except ProjError as e:
                    raise ValueError(
                        "No se pudo transformar perim -> DEM.\n"
                        f"- CRS perim: {perim_crs_pyproj}\n"
                        f"- CRS DEM:   {dem_crs_pyproj}\n"
                        f"- Error: {e}\n"
                        "Probá con --dem-crs correcto o --assume-same-crs si sabés que ya están iguales."
                    )
            else:
                perim_in_dem_crs = perim_gdf

        geoms = [g for g in perim_in_dem_crs.geometry if g is not None and not g.is_empty]
        if not geoms:
            raise ValueError("El perímetro no contiene geometrías válidas.")

        out_image, out_transform = mask(src, geoms, crop=True, nodata=src.nodata)
        dem = out_image[0].astype(np.float32)

        src_nodata = src.nodata if src.nodata is not None else -9999.0
        dem = np.where(dem == src_nodata, np.nan, dem)

        return dem, out_transform, dem_crs_rio, geoms


def reproject_manning_to_dem_grid(
    manning_path: Path,
    dem_shape: tuple[int, int],
    dem_transform,
    dem_crs,
):
    warp_nodata = -9999.0

    with rasterio.open(manning_path) as src:
        if src.crs is None:
            raise ValueError("El raster de Manning no tiene CRS definido.")

        src_arr = src.read(1).astype(np.float32)
        src_nodata = src.nodata if src.nodata is not None else warp_nodata
        src_arr = np.where(src_arr == src_nodata, np.nan, src_arr)
        src_for_warp = np.where(np.isnan(src_arr), warp_nodata, src_arr).astype(np.float32)

        destination = np.full(dem_shape, warp_nodata, dtype=np.float32)

        reproject(
            source=src_for_warp,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dem_transform,
            dst_crs=dem_crs,
            src_nodata=warp_nodata,
            dst_nodata=warp_nodata,
            resampling=Resampling.nearest,
        )

    return np.where(destination == warp_nodata, np.nan, destination)


def build_inside_mask(shape, transform, geoms):
    return geometry_mask(
        geoms,
        transform=transform,
        invert=True,
        out_shape=shape,
        all_touched=False,
    )


def compute_slope_aspect(dem: np.ndarray, transform):
    nodata_mask = np.isnan(dem)

    if HAS_RICHDEM:
        nodata_val = -9999.0
        dem_rd_arr = np.where(nodata_mask, nodata_val, dem).astype(np.float64)
        dem_rd = rd.rdarray(dem_rd_arr, no_data=nodata_val)

        slope = np.array(rd.TerrainAttribute(dem_rd, attrib="slope_riserun"), dtype=np.float32)
        aspect = np.array(rd.TerrainAttribute(dem_rd, attrib="aspect"), dtype=np.float32)

        slope[nodata_mask] = np.nan
        aspect[nodata_mask] = np.nan
        return slope, aspect

    xres = abs(transform.a)
    yres = abs(transform.e)

    filled = dem.astype(np.float64).copy()
    if np.all(np.isnan(filled)):
        raise ValueError("DEM completamente vacío (NaN). No se puede calcular slope/aspect.")

    fill_value = np.nanmedian(filled)
    filled[nodata_mask] = fill_value

    dz_dy, dz_dx = np.gradient(filled, yres, xres)
    slope = np.sqrt(dz_dx**2 + dz_dy**2).astype(np.float32)
    aspect = ((np.degrees(np.arctan2(-dz_dx, dz_dy)) + 360.0) % 360.0).astype(np.float32)

    slope[nodata_mask] = np.nan
    aspect[nodata_mask] = np.nan
    return slope, aspect


def fill_sinks_simple_python(dem: np.ndarray, max_iters: int = 0) -> np.ndarray:
    if max_iters <= 0:
        return dem

    out = dem.copy()
    rows, cols = out.shape
    valid = np.isfinite(out)
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for _ in range(max_iters):
        changed = 0
        new_out = out.copy()
        for r in range(1, rows - 1):
            for c in range(1, cols - 1):
                if not valid[r, c]:
                    continue
                mn = np.inf
                found = False
                for dr, dc in nbrs:
                    rr, cc = r + dr, c + dc
                    if valid[rr, cc]:
                        found = True
                        if out[rr, cc] < mn:
                            mn = out[rr, cc]
                if found and out[r, c] < mn:
                    new_out[r, c] = mn
                    changed += 1
        out = new_out
        if changed == 0:
            break
    return out


@njit(cache=True)
def fill_sinks_simple_numba(dem: np.ndarray, valid: np.ndarray, max_iters: int) -> np.ndarray:
    out = dem.copy()
    rows, cols = out.shape

    for _ in range(max_iters):
        changed = 0
        new_out = out.copy()

        for r in range(1, rows - 1):
            for c in range(1, cols - 1):
                if not valid[r, c]:
                    continue

                mn = 1.0e30
                found = False

                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        rr = r + dr
                        cc = c + dc
                        if valid[rr, cc]:
                            found = True
                            val = out[rr, cc]
                            if val < mn:
                                mn = val

                if found and out[r, c] < mn:
                    new_out[r, c] = mn
                    changed += 1

        out = new_out
        if changed == 0:
            break

    return out


def fill_sinks_simple(dem: np.ndarray, max_iters: int = 0, use_numba: bool = False) -> np.ndarray:
    if max_iters <= 0:
        return dem

    if use_numba and HAS_NUMBA:
        valid = np.isfinite(dem)
        return fill_sinks_simple_numba(dem.astype(np.float64), valid, int(max_iters)).astype(np.float32)

    return fill_sinks_simple_python(dem, max_iters=max_iters).astype(np.float32)


def _flow_d8_core_python(dem: np.ndarray, valid: np.ndarray, xres: float, yres: float):
    rows, cols = dem.shape
    diag = float(np.hypot(xres, yres))

    flowdir = np.full((rows, cols), np.nan, dtype=np.float32)
    receiver = np.full(rows * cols, -1, dtype=np.int64)
    indeg = np.zeros(rows * cols, dtype=np.int32)

    def idx(r, c):
        return r * cols + c

    neighbors = [
        (0, 1, 1, xres),
        (1, 1, 2, diag),
        (1, 0, 4, yres),
        (1, -1, 8, diag),
        (0, -1, 16, xres),
        (-1, -1, 32, diag),
        (-1, 0, 64, yres),
        (-1, 1, 128, diag),
    ]

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue

            z = dem[r, c]
            best_drop = 0.0
            best_code = 0
            best_rc = None

            for dr, dc, code, dist in neighbors:
                rr, cc = r + dr, c + dc
                if rr < 0 or rr >= rows or cc < 0 or cc >= cols:
                    continue
                if not valid[rr, cc]:
                    continue

                drop = (z - dem[rr, cc]) / dist
                if drop > best_drop:
                    best_drop = drop
                    best_code = code
                    best_rc = (rr, cc)

            flowdir[r, c] = float(best_code)
            if best_rc is not None:
                s = idx(r, c)
                t = idx(best_rc[0], best_rc[1])
                receiver[s] = t
                indeg[t] += 1

    return flowdir, receiver, indeg


@njit(cache=True)
def _flow_d8_core_numba(dem: np.ndarray, valid: np.ndarray, xres: float, yres: float):
    rows, cols = dem.shape
    diag = np.sqrt(xres * xres + yres * yres)

    flowdir = np.empty((rows, cols), dtype=np.float32)
    flowdir[:, :] = np.nan

    receiver = np.empty(rows * cols, dtype=np.int64)
    receiver[:] = -1

    indeg = np.zeros(rows * cols, dtype=np.int32)

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue

            z = dem[r, c]
            best_drop = 0.0
            best_code = 0
            best_rr = -1
            best_cc = -1

            rr = r
            cc = c + 1
            if cc < cols and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / xres
                if drop > best_drop:
                    best_drop = drop
                    best_code = 1
                    best_rr = rr
                    best_cc = cc

            rr = r + 1
            cc = c + 1
            if rr < rows and cc < cols and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / diag
                if drop > best_drop:
                    best_drop = drop
                    best_code = 2
                    best_rr = rr
                    best_cc = cc

            rr = r + 1
            cc = c
            if rr < rows and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / yres
                if drop > best_drop:
                    best_drop = drop
                    best_code = 4
                    best_rr = rr
                    best_cc = cc

            rr = r + 1
            cc = c - 1
            if rr < rows and cc >= 0 and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / diag
                if drop > best_drop:
                    best_drop = drop
                    best_code = 8
                    best_rr = rr
                    best_cc = cc

            rr = r
            cc = c - 1
            if cc >= 0 and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / xres
                if drop > best_drop:
                    best_drop = drop
                    best_code = 16
                    best_rr = rr
                    best_cc = cc

            rr = r - 1
            cc = c - 1
            if rr >= 0 and cc >= 0 and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / diag
                if drop > best_drop:
                    best_drop = drop
                    best_code = 32
                    best_rr = rr
                    best_cc = cc

            rr = r - 1
            cc = c
            if rr >= 0 and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / yres
                if drop > best_drop:
                    best_drop = drop
                    best_code = 64
                    best_rr = rr
                    best_cc = cc

            rr = r - 1
            cc = c + 1
            if rr >= 0 and cc < cols and valid[rr, cc]:
                drop = (z - dem[rr, cc]) / diag
                if drop > best_drop:
                    best_drop = drop
                    best_code = 128
                    best_rr = rr
                    best_cc = cc

            flowdir[r, c] = np.float32(best_code)

            if best_rr != -1:
                s = r * cols + c
                t = best_rr * cols + best_cc
                receiver[s] = t
                indeg[t] += 1

    return flowdir, receiver, indeg


def compute_flow_d8_numpy(dem: np.ndarray, transform, use_numba: bool = False):
    rows, cols = dem.shape
    valid = np.isfinite(dem)

    xres = float(abs(transform.a))
    yres = float(abs(transform.e))

    if use_numba and HAS_NUMBA:
        flowdir, receiver, indeg = _flow_d8_core_numba(
            dem.astype(np.float64), valid, xres, yres
        )
    else:
        flowdir, receiver, indeg = _flow_d8_core_python(dem, valid, xres, yres)

    n = rows * cols
    valid_flat = valid.ravel()

    acc = np.zeros(n, dtype=np.float64)
    acc[valid_flat] = 1.0

    q = deque(np.where(valid_flat & (indeg == 0))[0].tolist())

    while q:
        u = q.popleft()
        v = receiver[u]
        if v != -1:
            acc[v] += acc[u]
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)

    flowacc = acc.reshape(rows, cols).astype(np.float32)
    flowacc[~valid] = np.nan

    return flowdir.astype(np.float32), flowacc


def compute_manning_velocity_discharge(
    slope: np.ndarray,
    manning_n: np.ndarray,
    depth_m: float,
    cell_size_x: float,
):
    velocity = np.full_like(slope, np.nan, dtype=np.float32)
    discharge = np.full_like(slope, np.nan, dtype=np.float32)

    valid = (~np.isnan(slope)) & (~np.isnan(manning_n)) & (manning_n > 0)
    slope_safe = np.clip(slope, 1e-8, None)

    velocity[valid] = (1.0 / manning_n[valid]) * (depth_m ** (2.0 / 3.0)) * np.sqrt(slope_safe[valid])
    discharge[valid] = velocity[valid] * depth_m * cell_size_x

    return velocity, discharge


def main():
    parser = argparse.ArgumentParser(description="Preprocesamiento hidráulico básico")
    parser.add_argument("--input-dir", type=str, default=".", help="Carpeta con perim.shp, dem, manning3.asc")
    parser.add_argument("--output-dir", type=str, default="./output", help="Carpeta de salida")
    parser.add_argument("--depth", type=float, default=0.10, help="Profundidad hidráulica asumida (m)")
    parser.add_argument("--perim-crs", type=str, default=None, help="CRS perim si no tiene .prj. Ej: EPSG:5367")
    parser.add_argument("--dem-crs", type=str, default=None, help="Sobrescribe CRS del DEM si viene mal")
    parser.add_argument("--assume-same-crs", action="store_true", help="Fuerza mismo CRS sin reproyectar")
    parser.add_argument("--fill-sinks-iters", type=int, default=0, help="Iteraciones de relleno simple de sinks")
    parser.add_argument("--use-numba", action="store_true", help="Usar numba para acelerar D8 y fill sinks")
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = validate_inputs(input_dir)

    print(f"[INFO] Input dir: {input_dir}")
    print(f"[INFO] Output dir: {output_dir}")
    print(f"[INFO] PROJ data dir activo: {_proj_data_dir}")
    print("[INFO] richdem detectado." if HAS_RICHDEM else "[WARN] richdem no instalado: fallback numpy.")

    if args.use_numba:
        if HAS_NUMBA:
            print("[INFO] numba activado: se usará aceleración JIT.")
        else:
            print("[WARN] --use-numba solicitado pero numba no está instalado. Se usará Python normal.")

    perim_gdf = gpd.read_file(files["perim"])
    if perim_gdf.empty:
        raise ValueError("perim.shp está vacío.")

    dem, dem_transform, dem_crs, geoms_dem_crs = clip_dem_to_perimeter(
        files["dem"],
        perim_gdf,
        perim_crs_override=args.perim_crs,
        dem_crs_override=args.dem_crs,
        assume_same_crs=args.assume_same_crs,
    )

    inside_mask = build_inside_mask(dem.shape, dem_transform, geoms_dem_crs)
    dem[~inside_mask] = np.nan

    manning = reproject_manning_to_dem_grid(
        files["manning"],
        dem.shape,
        dem_transform,
        dem_crs,
    )
    manning[~inside_mask] = np.nan

    if args.fill_sinks_iters > 0:
        print(f"[INFO] Relleno simple de sinks activo: {args.fill_sinks_iters} iteraciones")
        dem_flow = fill_sinks_simple(
            dem,
            max_iters=args.fill_sinks_iters,
            use_numba=(args.use_numba and HAS_NUMBA),
        )
    else:
        dem_flow = dem

    slope, aspect = compute_slope_aspect(dem_flow, dem_transform)

    print("[INFO] Calculando flowdir/flowacc con D8...")
    fdir, facc = compute_flow_d8_numpy(
        dem_flow,
        dem_transform,
        use_numba=(args.use_numba and HAS_NUMBA),
    )

    save_geotiff(output_dir / "dem_clipped.tif", dem, dem_crs, dem_transform)
    save_geotiff(output_dir / "manning_aligned.tif", manning, dem_crs, dem_transform)
    save_geotiff(output_dir / "slope.tif", slope, dem_crs, dem_transform)
    save_geotiff(output_dir / "aspect.tif", aspect, dem_crs, dem_transform)
    save_geotiff(output_dir / "flowdir_d8.tif", fdir, dem_crs, dem_transform)
    save_geotiff(output_dir / "flowacc.tif", facc, dem_crs, dem_transform)

    cell_size_x = abs(dem_transform.a)
    velocity, discharge = compute_manning_velocity_discharge(
        slope=slope,
        manning_n=manning,
        depth_m=args.depth,
        cell_size_x=cell_size_x,
    )
    save_geotiff(output_dir / "velocity_manning.tif", velocity, dem_crs, dem_transform)
    save_geotiff(output_dir / "discharge_manning.tif", discharge, dem_crs, dem_transform)

    print(f"[OK] Proceso finalizado. Salida: {output_dir}")


if __name__ == "__main__":
    main()