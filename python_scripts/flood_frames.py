from __future__ import annotations

import argparse
from pathlib import Path
import csv

import numpy as np
import rasterio
import matplotlib.pyplot as plt


def read_raster_as_nan(path: Path):
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        transform = src.transform
        crs = src.crs
    return arr, transform, crs


def robust_norm(arr: np.ndarray, pmin=2, pmax=98):
    out = np.full_like(arr, np.nan, dtype=np.float32)
    valid = np.isfinite(arr)
    if not np.any(valid):
        return out
    lo, hi = np.nanpercentile(arr[valid], [pmin, pmax])
    if hi <= lo:
        out[valid] = 0.0
        return out
    out[valid] = (arr[valid] - lo) / (hi - lo)
    out[valid] = np.clip(out[valid], 0.0, 1.0)
    return out


def fixed_norm(arr: np.ndarray, lo: float, hi: float):
    out = np.zeros_like(arr, dtype=np.float32)
    valid = np.isfinite(arr)
    if hi <= lo:
        return out
    out[valid] = (arr[valid] - lo) / (hi - lo)
    return np.clip(out, 0.0, 1.0)


def build_background_rgb(dem: np.ndarray):
    dem_n = robust_norm(dem, 2, 98)
    bg = np.zeros((dem.shape[0], dem.shape[1], 3), dtype=np.float32)
    bg[..., 0] = dem_n
    bg[..., 1] = dem_n
    bg[..., 2] = dem_n
    bg = 0.20 + 0.80 * bg
    return np.clip(bg, 0.0, 1.0)


def compute_flood_potential(dem, flowacc, slope, w_flowacc, w_terrain, w_slope, gamma):
    flowacc_ln = np.log1p(flowacc)
    fa_n = robust_norm(flowacc_ln, 2, 99)

    dem_n = robust_norm(dem, 2, 98)
    terrain_low = 1.0 - dem_n

    slope_n = robust_norm(slope, 2, 98)
    slope_retention = 1.0 - slope_n

    pot = w_flowacc * fa_n + w_terrain * terrain_low + w_slope * slope_retention
    pot = np.clip(pot, 0.0, 1.0)
    pot = np.power(pot, gamma)
    pot[~np.isfinite(dem)] = np.nan
    return pot


def render_frame_rgb(bg_rgb, depth_map, velocity_map, wet_mask, depth_max, min_visible_depth, vel_lo, vel_hi):
    alpha = np.zeros_like(depth_map, dtype=np.float32)
    denom = max(depth_max - min_visible_depth, 1e-6)

    visible = wet_mask & np.isfinite(depth_map)
    alpha[visible] = (depth_map[visible] - min_visible_depth) / denom
    alpha = np.clip(alpha, 0.0, 1.0)
    alpha = np.power(alpha, 0.85)

    vel_n = fixed_norm(velocity_map, vel_lo, vel_hi)
    cmap = plt.get_cmap("turbo")
    flood_rgb = cmap(vel_n)[..., :3].astype(np.float32)

    out = bg_rgb.copy()
    out = out * (1.0 - alpha[..., None]) + flood_rgb * alpha[..., None]

    nodata = ~np.isfinite(depth_map)
    out[nodata] = 0.03
    return np.clip(out, 0.0, 1.0)


def main():
    parser = argparse.ArgumentParser(description="Generador de frames de inundación (depth/velocity/discharge)")
    parser.add_argument("--input-dir", type=str, required=True)
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--n-frames", type=int, default=240)
    parser.add_argument("--depth-min", type=float, default=0.00)
    parser.add_argument("--depth-max", type=float, default=0.40)
    parser.add_argument("--min-visible-depth", type=float, default=0.005)

    parser.add_argument("--w-flowacc", type=float, default=0.55)
    parser.add_argument("--w-terrain", type=float, default=0.30)
    parser.add_argument("--w-slope", type=float, default=0.15)
    parser.add_argument("--gamma", type=float, default=1.4)

    parser.add_argument("--growth-exp", type=float, default=1.8)
    parser.add_argument("--front-exp", type=float, default=1.3)

    parser.add_argument("--save-raster-every", type=int, default=0)
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    frames_dir = output_dir / "frames"
    rasters_dir = output_dir / "rasters"
    frames_dir.mkdir(parents=True, exist_ok=True)
    if args.save_raster_every > 0:
        rasters_dir.mkdir(parents=True, exist_ok=True)

    dem, transform, crs = read_raster_as_nan(input_dir / "dem_clipped.tif")
    slope, _, _ = read_raster_as_nan(input_dir / "slope.tif")
    manning, _, _ = read_raster_as_nan(input_dir / "manning_aligned.tif")
    flowacc, _, _ = read_raster_as_nan(input_dir / "flowacc.tif")

    cell_size_x = abs(transform.a)
    cell_size_y = abs(transform.e)
    cell_area = cell_size_x * cell_size_y

    bg_rgb = build_background_rgb(dem)
    flood_potential = compute_flood_potential(
        dem=dem,
        flowacc=flowacc,
        slope=slope,
        w_flowacc=args.w_flowacc,
        w_terrain=args.w_terrain,
        w_slope=args.w_slope,
        gamma=args.gamma,
    )

    valid_p = np.isfinite(flood_potential)
    pot_idx = robust_norm(flood_potential, 5, 99)
    pot_idx = np.where(valid_p, pot_idx, np.nan)

    depth_ref = np.full_like(pot_idx, np.nan, dtype=np.float32)
    depth_ref[valid_p] = args.depth_max * pot_idx[valid_p]

    valid_ref = valid_p & np.isfinite(manning) & np.isfinite(slope) & (manning > 0)
    slope_safe_ref = np.clip(slope, 1e-8, None)

    vel_ref = np.full_like(depth_ref, np.nan, dtype=np.float32)
    vel_ref[valid_ref] = (
        (1.0 / manning[valid_ref])
        * np.power(depth_ref[valid_ref], 2.0 / 3.0)
        * np.sqrt(slope_safe_ref[valid_ref])
    )

    if np.any(np.isfinite(vel_ref)):
        vel_lo, vel_hi = np.nanpercentile(vel_ref[np.isfinite(vel_ref)], [5, 98])
    else:
        vel_lo, vel_hi = 0.0, 1.0

    depths = np.linspace(args.depth_min, args.depth_max, args.n_frames, dtype=np.float32)

    metrics_path = output_dir / "metrics.csv"
    with open(metrics_path, "w", newline="", encoding="utf-8") as fcsv:
        writer = csv.writer(fcsv)
        writer.writerow([
            "frame",
            "depth_global_m",
            "flooded_area_m2",
            "mean_velocity_m_s",
            "max_velocity_m_s",
            "mean_discharge_m3_s",
            "max_discharge_m3_s",
        ])

        for i, d in enumerate(depths):
            t = i / max(args.n_frames - 1, 1)
            growth = np.power(t, args.growth_exp)
            tau = 1.0 - growth

            wet_mask = valid_p & (pot_idx >= tau)

            front = np.zeros_like(pot_idx, dtype=np.float32)
            den = max(1.0 - tau, 1e-6)
            front[wet_mask] = np.power((pot_idx[wet_mask] - tau) / den, args.front_exp)

            depth_map = np.full_like(pot_idx, np.nan, dtype=np.float32)
            depth_map[valid_p] = 0.0
            depth_map[wet_mask] = d * front[wet_mask]

            valid_h = wet_mask & np.isfinite(manning) & np.isfinite(slope) & (manning > 0)
            slope_safe = np.clip(slope, 1e-8, None)

            velocity = np.full_like(depth_map, np.nan, dtype=np.float32)
            discharge = np.full_like(depth_map, np.nan, dtype=np.float32)

            velocity[valid_h] = (
                (1.0 / manning[valid_h])
                * np.power(depth_map[valid_h], 2.0 / 3.0)
                * np.sqrt(slope_safe[valid_h])
            )
            discharge[valid_h] = velocity[valid_h] * depth_map[valid_h] * cell_size_x

            flooded = wet_mask & (depth_map >= args.min_visible_depth)
            flooded_area = float(np.sum(flooded) * cell_area)

            mean_v = float(np.nanmean(velocity[flooded])) if np.any(flooded) else 0.0
            max_v = float(np.nanmax(velocity[flooded])) if np.any(flooded) else 0.0
            mean_q = float(np.nanmean(discharge[flooded])) if np.any(flooded) else 0.0
            max_q = float(np.nanmax(discharge[flooded])) if np.any(flooded) else 0.0

            writer.writerow([i, float(d), flooded_area, mean_v, max_v, mean_q, max_q])

            rgb = render_frame_rgb(
                bg_rgb=bg_rgb,
                depth_map=depth_map,
                velocity_map=velocity,
                wet_mask=wet_mask,
                depth_max=args.depth_max,
                min_visible_depth=args.min_visible_depth,
                vel_lo=vel_lo,
                vel_hi=vel_hi,
            )

            frame_path = frames_dir / f"frame_{i:04d}.png"
            plt.imsave(frame_path, rgb)

            if args.save_raster_every > 0 and (i % args.save_raster_every == 0):
                meta = {
                    "driver": "GTiff",
                    "height": depth_map.shape[0],
                    "width": depth_map.shape[1],
                    "count": 1,
                    "dtype": "float32",
                    "crs": crs,
                    "transform": transform,
                    "nodata": -9999.0,
                    "compress": "lzw",
                }

                for name, arr in [("depth", depth_map), ("velocity", velocity), ("discharge", discharge)]:
                    out_arr = arr.astype(np.float32).copy()
                    out_arr[~np.isfinite(out_arr)] = -9999.0
                    out_path = rasters_dir / f"{name}_{i:04d}.tif"
                    with rasterio.open(out_path, "w", **meta) as dst:
                        dst.write(out_arr, 1)

            if i % 25 == 0 or i == args.n_frames - 1:
                print(f"[INFO] Frame {i+1}/{args.n_frames} depth={d:.3f}m area={flooded_area:.1f}m2")

    print(f"[OK] Frames generados en: {frames_dir}")
    print(f"[OK] Métricas: {metrics_path}")


if __name__ == "__main__":
    main()
