"""
=============================================================================
SISTEMA DE MONITOREO CLIMÁTICO FORESTAL — INAB / SIG-INAB
Pipeline Diario de Actualización Automatizada: GEE, ECMWF, CHIRPS y ANACAFE
=============================================================================
- Modelo Atmosférico, Suelo y Evapotranspiración: ECMWF ERA5-Land Daily
- Precipitación Diaria y Mensual (30d): UCSB CHIRPS Daily v2.0
- Downscaling Topoclimático y Orográfico de Alta Nitidez (DEM 20 m + SRTM 30 m)
- Contornos Meteorológicos en GEE: Isotermas (cada 2 °C) e Isoyetas (cada 10 mm)
- Malla Numérica Embebida (GEE_RASTER_GRID) para sincronización 100% exacta
  entre el color visual del mapa y la banderita de consulta puntual
- Recolección Diaria de Estaciones ANACAFE (.geojson, .csv, .gpkg) en ownCloud
=============================================================================
"""

import os
import json
import re
import sys
import datetime
import traceback
import sqlite3
import struct
import tempfile
import concurrent.futures
import requests
import ee

# Forzar salida de consola en tiempo real sin almacenamiento en búfer
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ── 1. AUTENTICACIÓN ROBUSTA CON GOOGLE EARTH ENGINE ──
def init_earth_engine():
    gee_key = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')
    if gee_key and gee_key.strip():
        print("Autenticando con Cuenta de Servicio de GEE desde Secret...", flush=True)
        gee_key_clean = gee_key.strip()
        try:
            key_dict = json.loads(gee_key_clean)
        except Exception as e:
            print(f"ERROR al decodificar JSON del secret GEE_SERVICE_ACCOUNT_KEY: {e}", flush=True)
            sys.exit(1)

        temp_key_file = 'temp_gee_key.json'
        with open(temp_key_file, 'w', encoding='utf-8') as f:
            f.write(gee_key_clean)

        try:
            credentials = ee.ServiceAccountCredentials(
                key_dict.get('client_email'),
                key_file=temp_key_file
            )
            ee.Initialize(credentials=credentials, project='monitoreoclimaticoforestal')
            print("✓ Autenticación GEE exitosa.", flush=True)
        finally:
            if os.path.exists(temp_key_file):
                os.remove(temp_key_file)
    else:
        print("Autenticando con credenciales por defecto...", flush=True)
        ee.Initialize(project='monitoreoclimaticoforestal')

try:
    init_earth_engine()

    guatemala_fc = ee.FeatureCollection("USDOS/LSIB_SIMPLE/2017").filter(ee.Filter.eq('country_na', 'Guatemala'))
    guatemala_mask = ee.Image.constant(1).clip(guatemala_fc).mask()

    def get_map_tile_url(image, vis_params):
        map_id = ee.Image(image).clip(guatemala_fc).getMapId(vis_params)
        return map_id['tile_fetcher'].url_format

    print("\n--- Iniciando preparación de colecciones (ECMWF + CHIRPS + MODIS + DEM 20m) ---", flush=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime('%Y-%m-%d')
    guatemala_geom = ee.Geometry.BBox(-92.25, 13.73, -88.22, 17.82)
    grid_bbox = [-92.30, 13.70, -88.15, 17.85]
    grid_geom = ee.Geometry.BBox(*grid_bbox)

    # Ventanas de tiempo
    date_era5_start = (now - datetime.timedelta(days=120)).strftime('%Y-%m-%d')
    date_chirps_start = (now - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    date_modis_start = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    date_gfs_start = (now - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
    date_gfs_end = (now + datetime.timedelta(days=2)).strftime('%Y-%m-%d')
    date_today = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # 1. Precipitación Observada Diaria y Mensual (30d): UCSB CHIRPS
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(date_chirps_start, date_today)

    # 2. Variables Atmosféricas, Suelo y Evapotranspiración: ECMWF ERA5-Land Daily
    era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(date_era5_start, date_today)

    # 3. Vigor Vegetal: MODIS Terra NDVI (250 m nativo)
    modis_ndvi = ee.ImageCollection("MODIS/061/MOD13Q1").filterDate(date_modis_start, date_today)

    # 4. Pronóstico Numérico para Respaldo de Rásteres ownCloud
    gfs = ee.ImageCollection("NOAA/GFS0P25").filterDate(date_gfs_start, date_gfs_end)

    # 5. Modelo Digital de Elevación Híbrido (DEM 20m Guatemala + SRTM 30m Regional)
    existing_assets = set()
    try:
        asset_list = ee.data.listAssets({'parent': 'projects/monitoreoclimaticoforestal/assets'}).get('assets', [])
        existing_assets = {a.get('name', '').split('/')[-1] for a in asset_list}
    except Exception:
        pass

    srtm_raw = ee.Image("USGS/SRTMGL1_003").select('elevation')
    srtm_30m = srtm_raw.unmask(0)
    terrain_slope = ee.Terrain.slope(srtm_raw).unmask(0)

    if 'DEM_Guatemala_20m' in existing_assets:
        print("✓ Usando DEM propio de 20 m (projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m) fusionado con SRTM 30 m...", flush=True)
        dem_20m = ee.Image("projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m").select(0)
        fine_dem = dem_20m.unmask(srtm_30m)
        land_mask = srtm_30m.gt(0).And(guatemala_mask)
    else:
        print("✓ Usando SRTM 30 m...", flush=True)
        fine_dem = srtm_30m
        land_mask = srtm_30m.gt(0).And(guatemala_mask)

    if 'delta_elevacion_20m' in existing_assets:
        delta_elevation = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_20m").select(0).unmask(0)
    elif 'delta_elevacion_era5land' in existing_assets:
        delta_30m = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_era5land").select(0).unmask(0)
        delta_elevation = delta_30m.add(fine_dem.subtract(srtm_30m)).unmask(0)
    else:
        coarse_dem = ee.Image("NOAA/NGDC/ETOPO1").select('bedrock').resample('bilinear')
        delta_elevation = fine_dem.subtract(coarse_dem).unmask(0)

    # Función limpia para rellenar bordes costeros ANTES de interpolar y recortar a tierra firme
    # Evita al 100% los bucles/líneas onduladas en los límites terrestres y el desenfoque de focalMean
    def clean_coarse(img, radius_px=2):
        filled = img.unmask(img.focalMean(radius_px, 'circle', 'pixels'))
        return filled.resample('bilinear').updateMask(land_mask)

    # ── A. PROCESAMIENTO FÍSICO Y DOWNSCALING TOPOCLIMÁTICO (20 m) ──
    print("Calculando capas climáticas con definición topográfica de 20 m...", flush=True)
    recent_chirps_raw = chirps.sort('system:time_start', False).first().select('precipitation')
    last_chirps_date = ee.Date(recent_chirps_raw.get('system:time_start'))

    # Acumulado de 30 días de CHIRPS (usado en Precipitación Mensual, De Martonne y Gaussen)
    monthly_precip_raw = chirps.filterDate(
        last_chirps_date.advance(-30, 'day'),
        last_chirps_date.advance(1, 'day')
    ).sum().select('precipitation')

    recent_era5_raw = era5.sort('system:time_start', False).first()
    last_era5_date = ee.Date(recent_era5_raw.get('system:time_start'))
    era5_8d = era5.filterDate(last_era5_date.advance(-8, 'day'), last_era5_date.advance(1, 'day'))

    # Factor orográfico de precipitación (20 m): realza la estructura topográfica de la lluvia en montaña
    oro_factor = ee.Image(1).add(delta_elevation.multiply(0.00025)).add(terrain_slope.multiply(0.004)).clamp(0.72, 1.48)

    # 1. Precipitación Diaria CHIRPS (mm)
    recent_chirps = clean_coarse(recent_chirps_raw, 1).multiply(oro_factor).clamp(0, 300).updateMask(land_mask)

    # 2. Precipitación Mensual CHIRPS 30 días (mm)
    monthly_precip = clean_coarse(monthly_precip_raw, 1).multiply(oro_factor).clamp(0, 1500).updateMask(land_mask)

    # 3. Temperatura Media y Mínima ECMWF ERA5-Land (°C, gradiente adiabático -0.0065 °C/m a 20 m)
    temp_base_c = clean_coarse(recent_era5_raw.select('temperature_2m').subtract(273.15))
    recent_temp = temp_base_c.add(delta_elevation.multiply(-0.0065)).updateMask(land_mask)

    temp_min_base_c = clean_coarse(recent_era5_raw.select('temperature_2m_min').subtract(273.15))
    recent_temp_min = temp_min_base_c.add(delta_elevation.multiply(-0.0065)).updateMask(land_mask)

    # 4. Punto de Rocío ECMWF ERA5-Land (°C, gradiente -0.0045 °C/m a 20 m)
    dew_base_c = clean_coarse(recent_era5_raw.select('dewpoint_temperature_2m').subtract(273.15))
    recent_dew = dew_base_c.add(delta_elevation.multiply(-0.0045)).updateMask(land_mask)

    # 5. Viento ECMWF ERA5-Land (nudos kt, con exposición en crestas topográficas de 20 m)
    u_wind_ecmwf = recent_era5_raw.select('u_component_of_wind_10m')
    v_wind_ecmwf = recent_era5_raw.select('v_component_of_wind_10m')
    wind_base_kt = clean_coarse(u_wind_ecmwf.hypot(v_wind_ecmwf).multiply(1.94384))
    recent_wind = wind_base_kt.add(delta_elevation.multiply(0.003)).add(terrain_slope.multiply(0.08)).clamp(0, 80).updateMask(land_mask)

    # 6. Humedad de Suelo Superficial (0-7cm) y Radicular (7-28cm) ECMWF con drenaje por pendiente y altitud (20 m)
    soil_topo_adj = delta_elevation.multiply(0.00006).subtract(terrain_slope.multiply(0.0022))
    soil1_base = clean_coarse(recent_era5_raw.select('volumetric_soil_water_layer_1'))
    soil2_base = clean_coarse(recent_era5_raw.select('volumetric_soil_water_layer_2'))
    recent_soil1 = soil1_base.add(soil_topo_adj).clamp(0.10, 0.52).updateMask(land_mask)
    recent_soil2 = soil2_base.add(soil_topo_adj.multiply(0.85)).clamp(0.10, 0.52).updateMask(land_mask)

    # 7. Vigorosidad Vegetal NDVI MODIS (250 m nativo reproyectado a WGS84 para evitar errores Sinusoidales)
    recent_ndvi_raw = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001)
    recent_ndvi = recent_ndvi_raw.reproject('EPSG:4326', None, 250).updateMask(land_mask).clip(guatemala_fc)

    # 8. Evapotranspiración Real (ET) y Potencial (PET) ECMWF ERA5-Land (mm / 8 días, sin huecos por nubes)
    et_real_base = clean_coarse(era5_8d.select('total_evaporation_sum').sum().multiply(-1000))
    et_pot_base = clean_coarse(era5_8d.select('potential_evaporation_sum').sum().multiply(-1000))
    recent_et_real = et_real_base.add(delta_elevation.multiply(-0.0035)).subtract(terrain_slope.multiply(0.08)).clamp(5, 45).updateMask(land_mask)
    recent_et_pot = et_pot_base.add(delta_elevation.multiply(-0.012)).subtract(terrain_slope.multiply(0.15)).clamp(15, 110).updateMask(land_mask)

    # 9. Déficit / Balance Hídrico Ombrotérmico de Gaussen (P_30d - 2*T, definición 20 m)
    recent_gaussen = monthly_precip.subtract(recent_temp.multiply(2)).updateMask(land_mask)

    # 10. Estrés Hídrico Foliar Forestal (basado en humedad de suelo 20 m)
    soil_pct_20m = recent_soil1.add(recent_soil2).multiply(50)
    recent_estres = ee.Image(42).subtract(soil_pct_20m).multiply(0.45).clamp(0, 8).updateMask(land_mask)

    # 11. Índice de Aridez de De Martonne Mensual (12 * P_30d / (T + 10), definición 20 m)
    recent_martonne = monthly_precip.multiply(12).divide(recent_temp.add(10).max(1)).updateMask(land_mask)

    # 12. Susceptibilidad Forestal a Sequía (definición 20 m)
    recent_susc_sequia = ee.Image(44).subtract(soil_pct_20m).multiply(0.038).clamp(0.08, 0.95).updateMask(land_mask)

    # 13. Contornos Meteorológicos: Isotermas (cada 2 °C) e Isoyetas (cada 5/10 mm)
    elev_smooth_iso = srtm_30m.focalMean(1200, 'circle', 'meters')
    coarse_elev_img = srtm_30m.subtract(delta_elevation)
    temp_iso_field = temp_base_c.add(elev_smooth_iso.subtract(coarse_elev_img).multiply(-0.0065))
    isotermas_edges = ee.Algorithms.CannyEdgeDetector(temp_iso_field.divide(2).floor(), 0.4, 0).focalMax(1.5, 'circle', 'pixels').updateMask(land_mask)

    rain_iso_field = clean_coarse(recent_chirps_raw, 1).focalMean(1000, 'circle', 'meters')
    isoyetas_edges = ee.Algorithms.CannyEdgeDetector(rain_iso_field.divide(5).floor(), 0.4, 0).focalMax(1.5, 'circle', 'pixels').updateMask(land_mask)

    # Pronósticos GFS para exportación secundaria a ownCloud
    latest_gfs_run = gfs.sort('system:time_start', False).first()
    latest_gfs_time = latest_gfs_run.get('system:time_start')
    latest_forecasts = gfs.filter(ee.Filter.eq('system:time_start', latest_gfs_time))
    precip_rate_daily = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 24)).select('precipitation_rate').mean().multiply(86400).resample('bilinear')
    gfs_7d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 168)).select('precipitation_rate').mean().multiply(86400 * 7).resample('bilinear')
    gfs_16d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 384)).select('precipitation_rate').mean().multiply(86400 * 16).resample('bilinear')

    urls = {}

    print("Procesando [1/20] Precipitación Diaria CHIRPS (20m)...", flush=True)
    urls['precip'] = get_map_tile_url(recent_chirps, {'min': 0, 'max': 25, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    print("Procesando [2/20] Temperatura ECMWF Topoclimática (20m)...", flush=True)
    urls['temp'] = get_map_tile_url(recent_temp, {'min': 10, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']})

    print("Procesando [3/20] Viento ECMWF (20m)...", flush=True)
    urls['wind'] = get_map_tile_url(recent_wind, {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']})

    print("Procesando [4/20] Humedad Suelo Superficial ECMWF (0-7cm, 20m)...", flush=True)
    urls['soil'] = get_map_tile_url(recent_soil1, {'min': 0.22, 'max': 0.48, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    print("Procesando [5/17] Punto de Rocío ECMWF (20m)...", flush=True)
    urls['dew'] = get_map_tile_url(recent_dew, {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']})

    print("Procesando [6/17] Precipitación Mensual CHIRPS (30 días, 20m)...", flush=True)
    urls['precipMensual'] = get_map_tile_url(monthly_precip, {'min': 50, 'max': 380, 'palette': ['#e0f3f8', '#74add1', '#4575b4', '#313695', '#045a8d', '#023858']})

    print("Procesando [7/17] Saturación Suelo Radicular ECMWF (7-28cm, 20m)...", flush=True)
    urls['soilRoot'] = get_map_tile_url(recent_soil2, {'min': 0.22, 'max': 0.48, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    print("Procesando [8/17] NDVI MODIS Vigorosidad (250m)...", flush=True)
    urls['ndvi'] = get_map_tile_url(recent_ndvi, {'min': 0.15, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']})

    print("Procesando [9/17 y 10/17] Evapotranspiración Real y Potencial ECMWF ERA5-Land (20m)...", flush=True)
    urls['etReal'] = get_map_tile_url(recent_et_real, {'min': 12, 'max': 36, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
    urls['etPot'] = get_map_tile_url(recent_et_pot, {'min': 25, 'max': 95, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})

    print("Procesando [11/17] Déficit / Balance Hídrico Gaussen (30d, 20m)...", flush=True)
    urls['gaussen'] = get_map_tile_url(recent_gaussen, {'min': -40, 'max': 280, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#a7f3d0', '#38bdf8', '#0284c7', '#1e3a8a']})

    print("Procesando [15/20] Estrés Hídrico Foliar (20m)...", flush=True)
    urls['estres'] = get_map_tile_url(recent_estres, {'min': 0, 'max': 8, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})

    print("Procesando [16/20] Índice de Aridez De Martonne (30d, 20m)...", flush=True)
    urls['martonne'] = get_map_tile_url(recent_martonne, {'min': 10, 'max': 130, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#a6d96a', '#38bdf8', '#1d4ed8', '#1e3a8a']})

    print("Procesando [17/20] Susceptibilidad a Sequía (20m)...", flush=True)
    urls['suscSequia'] = get_map_tile_url(recent_susc_sequia, {'min': 0.1, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})

    print("Procesando [18/20] Susceptibilidad a Heladas (ECMWF Tmin 20m)...", flush=True)
    urls['suscHelada'] = get_map_tile_url(recent_temp_min, {'min': -2, 'max': 18, 'palette': ['#08306b', '#2171b5', '#6baed6', '#bdd7e7', '#fee08b', '#fdae61', '#a0562e']})

    print("Procesando [19/20 y 20/20] Contornos: Isotermas (2°C) e Isoyetas (10mm)...", flush=True)
    urls['isotermas'] = get_map_tile_url(isotermas_edges.selfMask(), {'min': 0, 'max': 1, 'palette': ['#fef08a', '#f97316']})
    urls['isoyetas'] = get_map_tile_url(isoyetas_edges.selfMask(), {'min': 0, 'max': 1, 'palette': ['#e0f2fe', '#0284c7']})

    # ── B. EXTRACCIÓN DE MALLA NUMÉRICA GEE_RASTER_GRID PARA COINCIDENCIA 100% EN BANDERITA ──
    print("Extrayendo malla numérica GEE_RASTER_GRID para sincronización exacta de banderita...", flush=True)
    gee_raster_grid_json = None
    try:
        coarse_elev_ref = srtm_30m.subtract(delta_elevation)
        grid_stack = ee.Image.cat([
            recent_chirps_raw.unmask(0).rename('precip'),
            monthly_precip_raw.unmask(0).rename('precipMensual'),
            temp_base_c.unmask(22).rename('temp_base'),
            temp_min_base_c.unmask(15).rename('temp_min_base'),
            dew_base_c.unmask(16).rename('dew_base'),
            wind_base_kt.unmask(5).rename('wind_base'),
            soil1_base.unmask(0.35).rename('soil1_base'),
            soil2_base.unmask(0.36).rename('soil2_base'),
            recent_ndvi.unmask(0.5).rename('ndvi'),
            et_real_base.unmask(25).rename('et_real_base'),
            et_pot_base.unmask(55).rename('et_pot_base'),
            coarse_elev_ref.unmask(500).rename('coarse_elev')
        ]).reproject(crs='EPSG:4326', scale=9000)

        rect = grid_stack.sampleRectangle(region=grid_geom, defaultValue=0).getInfo().get('properties', {})
        rows = len(rect.get('precip', []))
        cols = len(rect['precip'][0]) if rows > 0 else 0

        def round_grid(mat, decimals=1):
            return [[round(float(v or 0), decimals) for v in row] for row in mat]

        if rows > 0 and cols > 0:
            grid_obj = {
                'bbox': grid_bbox,  # [lon_min, lat_min, lon_max, lat_max]
                'rows': rows,
                'cols': cols,
                'updated': date_str,
                'precip': round_grid(rect['precip'], 1),
                'precipMensual': round_grid(rect['precipMensual'], 0),
                'temp_base': round_grid(rect['temp_base'], 1),
                'temp_min_base': round_grid(rect['temp_min_base'], 1),
                'dew_base': round_grid(rect['dew_base'], 1),
                'wind_base': round_grid(rect['wind_base'], 1),
                'soil1_base': round_grid(rect['soil1_base'], 3),
                'soil2_base': round_grid(rect['soil2_base'], 3),
                'ndvi': round_grid(rect['ndvi'], 2),
                'et_real_base': round_grid(rect['et_real_base'], 1),
                'et_pot_base': round_grid(rect['et_pot_base'], 1),
                'coarse_elev': round_grid(rect['coarse_elev'], 0)
            }
            gee_raster_grid_json = json.dumps(grid_obj, separators=(',', ':'))
            print(f"✓ Malla numérica GEE_RASTER_GRID generada ({rows}x{cols} celdas).", flush=True)
    except Exception as e_grid:
        print(f"Aviso al generar GEE_RASTER_GRID: {e_grid}", flush=True)

    # ── 2. ACTUALIZACIÓN AUTOMÁTICA DE INDEX.HTML ──
    index_path = 'index.html'
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        for key, new_url in urls.items():
            pattern = rf"{key}:\s*'https://earthengine\.googleapis\.com/v1/projects/[^']+'"
            if re.search(pattern, html_content):
                html_content = re.sub(pattern, f"{key}: '{new_url}'", html_content)
            else:
                # Si la llave (como isotermas o isoyetas) aún no estaba en GEE_URLS, insertarla antes del cierre
                html_content = re.sub(
                    r"(suscHelada:\s*'https://earthengine\.googleapis\.com/v1/projects/[^']+')",
                    rf"\1,\n            {key}: '{new_url}'",
                    html_content,
                    count=1
                )

        if gee_raster_grid_json:
            grid_pattern = r"const GEE_RASTER_GRID\s*=\s*\{.*?\};\s*// END_GEE_RASTER_GRID"
            grid_replacement = f"const GEE_RASTER_GRID = {gee_raster_grid_json}; // END_GEE_RASTER_GRID"
            if re.search(grid_pattern, html_content, flags=re.DOTALL):
                html_content = re.sub(grid_pattern, lambda _: grid_replacement, html_content, flags=re.DOTALL)

        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("✓ index.html actualizado con las 20 URLs de GEE y la malla GEE_RASTER_GRID.", flush=True)

    # ── 3. RECOLECCIÓN DIARIA DE RED ANACAFE Y GENERACIÓN VECTORIAL / GPKG ──
    print("\n--- Iniciando Recolección de Datos de la Red ANACAFE ---", flush=True)
    anacafe_geojson = None
    anacafe_csv_bytes = None
    anacafe_gpkg_path = None
    calibracion_csv_bytes = None

    try:
        ANACAFE_CATALOG_URL = 'https://meteorologia.anacafe.org/servicios/api.php?op=estaciones'
        ANACAFE_OBS_URL = 'https://meteorologia.anacafe.org/servicios/api.php?op=obs'
        
        st_resp = requests.get(ANACAFE_CATALOG_URL, timeout=12)
        raw_catalog = st_resp.json()
        stations = raw_catalog if isinstance(raw_catalog, list) else raw_catalog.get('value', [])
        print(f"Catálogo ANACAFE obtenido: {len(stations)} estaciones registradas.", flush=True)

        d_today = now.strftime('%Y-%m-%d')
        d_yesterday = (now - datetime.timedelta(days=2)).strftime('%Y-%m-%d')

        def fetch_station_daily(s):
            sid = str(s.get('IdEstacion'))
            sname = str(s.get('Nombre', f'Estacion {sid}'))
            try:
                lat = float(s.get('Latitud'))
                lon = float(s.get('Longitud'))
            except (ValueError, TypeError):
                return None
            muni = str(s.get('Municipio', ''))

            obs_url = f"{ANACAFE_OBS_URL}&id={sid}&fecha1={d_yesterday}&fecha2={d_today}"
            try:
                r = requests.get(obs_url, timeout=10)
                if not r.ok:
                    return None
                data = r.json()
                obs = data.get('obs', {})
                dias = obs.get('dia') or obs.get('fecha') or []
                horas = obs.get('hora') or []
                
                if not dias or not horas:
                    return None

                def clean_float_list(arr):
                    res = []
                    if not arr: return res
                    for x in arr:
                        try:
                            val = float(str(x).replace(',', '.'))
                            if not (val != val):
                                res.append(val)
                        except (ValueError, TypeError):
                            pass
                    return res

                t_list = clean_float_list(obs.get('temperatura'))
                h_list = clean_float_list(obs.get('humedad'))
                p_list = clean_float_list(obs.get('lluvia'))
                w_list = clean_float_list(obs.get('vientoV'))
                rad_list = clean_float_list(obs.get('radiacion'))
                dew_list = clean_float_list(obs.get('rocio'))
                soil_list = clean_float_list(obs.get('suelo'))

                n_rec = len(dias)
                t_max = max(t_list) if t_list else None
                t_min = min(t_list) if t_list else None
                t_avg = sum(t_list)/len(t_list) if t_list else None
                h_avg = sum(h_list)/len(h_list) if h_list else None
                p_sum = sum(p_list) if p_list else 0.0
                w_max = max(w_list) if w_list else None
                rad_avg = sum(rad_list)/len(rad_list) if rad_list else None
                dew_avg = sum(dew_list)/len(dew_list) if dew_list else None
                soil_avg = sum(soil_list)/len(soil_list) if soil_list else None

                last_date = dias[-1] if dias else d_today
                last_time = horas[-1] if horas else ''

                readings = []
                for k in range(n_rec):
                    readings.append({
                        'id': sid, 'nombre': sname, 'muni': muni,
                        'lat': lat, 'lon': lon,
                        'fecha': dias[k], 'hora': horas[k] if k < len(horas) else '',
                        'temp': obs.get('temperatura', [])[k] if k < len(obs.get('temperatura', [])) else '',
                        'hr': obs.get('humedad', [])[k] if k < len(obs.get('humedad', [])) else '',
                        'lluvia': obs.get('lluvia', [])[k] if k < len(obs.get('lluvia', [])) else '',
                        'viento': obs.get('vientoV', [])[k] if k < len(obs.get('vientoV', [])) else '',
                        'rad': obs.get('radiacion', [])[k] if k < len(obs.get('radiacion', [])) else '',
                        'rocio': obs.get('rocio', [])[k] if k < len(obs.get('rocio', [])) else '',
                        'suelo': obs.get('suelo', [])[k] if k < len(obs.get('suelo', [])) else ''
                    })

                return {
                    'id': sid, 'nombre': sname, 'muni': muni,
                    'lat': lat, 'lon': lon, 'registros': n_rec,
                    't_max': t_max, 't_min': t_min, 't_avg': t_avg,
                    'h_avg': h_avg, 'p_sum': p_sum, 'w_max': w_max,
                    'rad_avg': rad_avg, 'dew_avg': dew_avg, 'soil_avg': soil_avg,
                    'ultima_fecha': last_date, 'ultima_hora': last_time,
                    'readings': readings
                }
            except Exception:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
            st_results = list(pool.map(fetch_station_daily, stations))

        active_stations = [s for s in st_results if s is not None]
        print(f"✓ Descarga completada: {len(active_stations)} estaciones ANACAFE activas con mediciones recientes.", flush=True)

        # A. Crear Capa Vectorial GeoJSON
        geojson_features = []
        for s in active_stations:
            geojson_features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(s['lon'], 5), round(s['lat'], 5)]
                },
                "properties": {
                    "id_estacion": s['id'],
                    "nombre": s['nombre'],
                    "municipio": s['muni'],
                    "fecha_corte": date_str,
                    "ultima_lectura": f"{s['ultima_fecha']} {s['ultima_hora']}",
                    "registros_48h": s['registros'],
                    "temp_media_c": round(s['t_avg'], 1) if s['t_avg'] is not None else None,
                    "temp_max_c": round(s['t_max'], 1) if s['t_max'] is not None else None,
                    "temp_min_c": round(s['t_min'], 1) if s['t_min'] is not None else None,
                    "humedad_rel_pct": round(s['h_avg'], 0) if s['h_avg'] is not None else None,
                    "precipitacion_48h_mm": round(s['p_sum'], 1) if s['p_sum'] is not None else 0.0,
                    "viento_max_kmh": round(s['w_max'], 1) if s['w_max'] is not None else None,
                    "radiacion_media_wm2": round(s['rad_avg'], 0) if s['rad_avg'] is not None else None,
                    "punto_rocio_c": round(s['dew_avg'], 1) if s['dew_avg'] is not None else None,
                    "humedad_suelo_pct": round(s['soil_avg'], 1) if s['soil_avg'] is not None else None
                }
            })

        anacafe_geojson = json.dumps({
            "type": "FeatureCollection",
            "crs": { "type": "name", "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" } },
            "features": geojson_features
        }, indent=2, ensure_ascii=False)

        # B. Crear CSV con todas las lecturas
        all_readings = []
        for s in active_stations:
            all_readings.extend(s.get('readings', []))

        csv_header = "ID_Estacion,Nombre,Municipio,Latitud,Longitud,Fecha,Hora,Temperatura_C,Humedad_Relativa_pct,Precipitacion_mm,Viento_kmh,Radiacion_wm2,Punto_Rocio_C,Humedad_Suelo_pct"
        csv_rows = [csv_header]
        for r in all_readings:
            row_str = f'"{r["id"]}","{r["nombre"]}","{r["muni"]}",{r["lat"]},{r["lon"]},"{r["fecha"]}","{r["hora"]}",{r["temp"]},{r["hr"]},{r["lluvia"]},{r["viento"]},{r["rad"]},{r["rocio"]},{r["suelo"]}'
            csv_rows.append(row_str)
        anacafe_csv_bytes = "\r\n".join(csv_rows).encode('utf-8-sig')

        # C. Crear Geodatabase Estándar OGC GeoPackage (.gpkg)
        temp_gpkg = os.path.join(tempfile.gettempdir(), f'ANACAFE_Estaciones_Meteorologicas_{date_str}.gpkg')
        if os.path.exists(temp_gpkg):
            os.remove(temp_gpkg)

        conn = sqlite3.connect(temp_gpkg)
        cur = conn.cursor()

        cur.executescript("""
            PRAGMA application_id = 1196444487;
            PRAGMA user_version = 10200;

            CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );

            INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES 
            ('WGS 84 geodetic', 4326, 'EPSG', 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', 'WGS 84');

            CREATE TABLE IF NOT EXISTS gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER,
                CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            );

            CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name)
            );

            CREATE TABLE IF NOT EXISTS estaciones_anacafe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geom BLOB,
                id_estacion TEXT,
                nombre TEXT,
                municipio TEXT,
                latitud DOUBLE,
                longitud DOUBLE,
                fecha_corte TEXT,
                registros_48h INTEGER,
                temp_media_c DOUBLE,
                temp_max_c DOUBLE,
                temp_min_c DOUBLE,
                humedad_rel_pct DOUBLE,
                precipitacion_48h_mm DOUBLE,
                viento_max_kmh DOUBLE,
                radiacion_media_wm2 DOUBLE,
                punto_rocio_c DOUBLE,
                humedad_suelo_pct DOUBLE
            );
        """)

        def create_gpkg_point(lon, lat):
            header = struct.pack('<2sBBii', b'GP', 0, 1, 4326, 0)
            wkb_point = struct.pack('<BIdd', 1, 1, lon, lat)
            return header + wkb_point

        for s in active_stations:
            geom_blob = create_gpkg_point(s['lon'], s['lat'])
            cur.execute("""
                INSERT INTO estaciones_anacafe 
                (geom, id_estacion, nombre, municipio, latitud, longitud, fecha_corte, registros_48h,
                 temp_media_c, temp_max_c, temp_min_c, humedad_rel_pct, precipitacion_48h_mm,
                 viento_max_kmh, radiacion_media_wm2, punto_rocio_c, humedad_suelo_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                geom_blob, s['id'], s['nombre'], s['muni'], s['lat'], s['lon'], date_str, s['registros'],
                round(s['t_avg'], 1) if s['t_avg'] is not None else None,
                round(s['t_max'], 1) if s['t_max'] is not None else None,
                round(s['t_min'], 1) if s['t_min'] is not None else None,
                round(s['h_avg'], 1) if s['h_avg'] is not None else None,
                round(s['p_sum'], 1) if s['p_sum'] is not None else 0.0,
                round(s['w_max'], 1) if s['w_max'] is not None else None,
                round(s['rad_avg'], 1) if s['rad_avg'] is not None else None,
                round(s['dew_avg'], 1) if s['dew_avg'] is not None else None,
                round(s['soil_avg'], 1) if s['soil_avg'] is not None else None
            ))

        cur.execute("""
            INSERT OR REPLACE INTO gpkg_contents 
            (table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id)
            VALUES ('estaciones_anacafe', 'features', 'estaciones_anacafe', 'Red Meteorológica ANACAFE Diaria',
                    -92.25, 13.73, -88.22, 17.82, 4326)
        """)

        cur.execute("""
            INSERT OR REPLACE INTO gpkg_geometry_columns 
            (table_name, column_name, geometry_type_name, srs_id, z, m)
            VALUES ('estaciones_anacafe', 'geom', 'POINT', 4326, 0, 0)
        """)

        conn.commit()
        conn.close()
        anacafe_gpkg_path = temp_gpkg
        print(f"✓ Geodatabase GeoPackage generada: {temp_gpkg}", flush=True)

        # D. Calibración Técnica: Estaciones ANACAFE vs Modelos Globales (ECMWF + CHIRPS)
        print("Calculando calibración puntual: ANACAFE vs ECMWF / CHIRPS...", flush=True)
        try:
            calib_features = []
            for s in active_stations:
                pt = ee.Geometry.Point([s['lon'], s['lat']])
                calib_features.append(ee.Feature(pt, {
                    'st_id': s['id'], 'st_nombre': s['nombre'],
                    'obs_temp': s['t_avg'] or 0, 'obs_rain': s['p_sum'] or 0,
                    'obs_soil': s['soil_avg'] or 0, 'obs_dew': s['dew_avg'] or 0
                }))

            calib_fc = ee.FeatureCollection(calib_features)
            sampled = ee.Image.cat([
                recent_temp.rename('ecmwf_temp_20m'),
                recent_chirps.rename('chirps_rain'),
                recent_soil1.multiply(100).rename('ecmwf_soil_pct'),
                recent_dew.rename('ecmwf_dew')
            ]).reduceRegions(
                collection=calib_fc,
                reducer=ee.Reducer.first(),
                scale=100
            )

            res = sampled.getInfo().get('features', [])
            calib_lines = ["ID_Estacion,Nombre,Fecha,Temp_ANACAFE,Temp_ECMWF_20m,Sesgo_Temp_C,Lluvia_ANACAFE,Lluvia_CHIRPS,Sesgo_Lluvia_mm,Suelo_ANACAFE,Suelo_ECMWF,Sesgo_Suelo_pct"]
            for f in res:
                p = f.get('properties', {})
                sid = p.get('st_id', '')
                snom = p.get('st_nombre', '')
                t_obs = round(p.get('obs_temp', 0) or 0, 1)
                t_mod = round(p.get('ecmwf_temp_20m', 0) or 0, 1)
                dt = round(t_obs - t_mod, 1)

                r_obs = round(p.get('obs_rain', 0) or 0, 1)
                r_mod = round(p.get('chirps_rain', 0) or 0, 1)
                dr = round(r_obs - r_mod, 1)

                s_obs = round(p.get('obs_soil', 0) or 0, 1)
                s_mod = round(p.get('ecmwf_soil_pct', 0) or 0, 1)
                ds = round(s_obs - s_mod, 1)

                calib_lines.append(f'"{sid}","{snom}",{date_str},{t_obs},{t_mod},{dt},{r_obs},{r_mod},{dr},{s_obs},{s_mod},{ds}')

            calibracion_csv_bytes = "\r\n".join(calib_lines).encode('utf-8-sig')
            print("✓ Tabla de calibración y análisis de sesgo generada.", flush=True)
        except Exception as e_cal:
            print(f"Nota en calibración: {e_cal}", flush=True)

    except Exception as e_anacafe:
        print(f"Aviso en recolección ANACAFE: {e_anacafe}", flush=True)

    # ── 4. EXPORTACIÓN Y DISTRIBUCIÓN EN OWNCLOUD (01 A 05) ──
    owncloud_config_raw = os.environ.get('OWNCLOUD_CONFIG')
    if owncloud_config_raw and owncloud_config_raw.strip():
        print("\n--- Iniciando Distribución en ownCloud (01 a 05) ---", flush=True)
        try:
            oc_cfg = json.loads(owncloud_config_raw.strip())
            token = oc_cfg.get('token', 'LYKifAMgtrqhJiq')
            password = oc_cfg.get('password', '')
            base_dav = f"https://inab.ocis.nube4.cloud/remote.php/dav/public-files/{token}"

            def upload_to_owncloud(target_folder, filename, content_bytes, content_type='application/octet-stream'):
                dest_url = f"{base_dav}/{target_folder}/{filename}"
                headers = {'Content-Type': content_type}
                try:
                    res = requests.put(dest_url, data=content_bytes, auth=(token, password), headers=headers, timeout=50)
                    if res.status_code == 401 and password:
                        res = requests.put(dest_url, data=content_bytes, auth=('', password), headers=headers, timeout=50)

                    if res.status_code in [200, 201, 204]:
                        print(f"  ✓ Subido a [{target_folder}]: {filename} ({res.status_code})", flush=True)
                        return True
                    elif res.status_code == 404:
                        fallback_url = f"{base_dav}/{filename}"
                        res2 = requests.put(fallback_url, data=content_bytes, auth=(token, password), headers=headers, timeout=50)
                        if res2.status_code == 401 and password:
                            res2 = requests.put(fallback_url, data=content_bytes, auth=('', password), headers=headers, timeout=50)
                        if res2.status_code in [200, 201, 204]:
                            print(f"  ✓ Subido a [Raíz]: {filename} ({res2.status_code})", flush=True)
                            return True
                    print(f"  ⚠️ Aviso en {filename}: Servidor respondió HTTP {res.status_code}", flush=True)
                except Exception as ex:
                    print(f"  ⚠️ Error de red al subir {filename}: {ex}", flush=True)
                return False

            data_payload = json.dumps({
                "fecha": date_str,
                "actualizado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "modelo_atmosferico": "ECMWF ERA5-Land (Downscaling Topoclimático DEM 20m)",
                "modelo_precipitacion": "UCSB CHIRPS Daily v2.0 (Modulación Orográfica 20m)",
                "urls": urls
            }, indent=2)
            upload_to_owncloud("01_DIARIOS", "datos_climaticos_inab.json", data_payload.encode('utf-8'), 'application/json')

            if anacafe_geojson:
                upload_to_owncloud("05_ESTACIONES", "ANACAFE_Red_Meteorologica_Actual.geojson", anacafe_geojson.encode('utf-8'), 'application/geo+json')
                upload_to_owncloud("05_ESTACIONES", f"ANACAFE_Red_Meteorologica_{date_str}.geojson", anacafe_geojson.encode('utf-8'), 'application/geo+json')

            if anacafe_csv_bytes:
                upload_to_owncloud("05_ESTACIONES", f"ANACAFE_Mediciones_Completas_{date_str}.csv", anacafe_csv_bytes, 'text/csv')

            if anacafe_gpkg_path and os.path.exists(anacafe_gpkg_path):
                with open(anacafe_gpkg_path, 'rb') as gf:
                    gpkg_bytes = gf.read()
                upload_to_owncloud("05_ESTACIONES", "ANACAFE_Estaciones_Meteorologicas.gpkg", gpkg_bytes, 'application/geopackage+sqlite3')
                upload_to_owncloud("05_ESTACIONES", f"ANACAFE_Estaciones_Meteorologicas_{date_str}.gpkg", gpkg_bytes, 'application/geopackage+sqlite3')

            if calibracion_csv_bytes:
                upload_to_owncloud("05_ESTACIONES", f"calibracion_anacafe_vs_globales_{date_str}.csv", calibracion_csv_bytes, 'text/csv')

            all_exports = [
                ("01_DIARIOS", recent_chirps, f"CHIRPS_precipitacion_diaria_{date_str}", 5500),
                ("01_DIARIOS", recent_temp, f"ECMWF_temperatura_20m_{date_str}", 2000),
                ("01_DIARIOS", recent_soil1, f"ECMWF_humedad_suelo_superficial_{date_str}", 2000),
                ("01_DIARIOS", recent_soil2, f"ECMWF_humedad_suelo_radicular_{date_str}", 2000),
                ("01_DIARIOS", recent_dew, f"ECMWF_punto_rocio_{date_str}", 2000),
                ("01_DIARIOS", recent_wind, f"ECMWF_viento_velocidad_{date_str}", 2000),
                ("01_DIARIOS", recent_gaussen, f"Gaussen_deficit_hidrico_{date_str}", 2000),
                ("01_DIARIOS", recent_estres, f"Estres_hidrico_foliar_{date_str}", 2000),
                ("01_DIARIOS", recent_susc_sequia, f"Susceptibilidad_sequia_{date_str}", 2000),
                ("01_DIARIOS", recent_temp_min, f"Susceptibilidad_heladas_{date_str}", 2000),
                ("02_SEMANALES", recent_ndvi.toFloat(), f"MODIS_ndvi_vigor_16D_{date_str}", 2000),
                ("02_SEMANALES", recent_et_real.toFloat(), f"ECMWF_evapotranspiracion_real_8D_{date_str}", 2000),
                ("02_SEMANALES", recent_et_pot.toFloat(), f"ECMWF_evapotranspiracion_potencial_8D_{date_str}", 2000),
                ("03_MENSUALES", monthly_precip, f"CHIRPS_precipitacion_mensual_acumulada_{date_str}", 5500),
                ("03_MENSUALES", recent_martonne, f"DeMartonne_indice_aridez_{date_str}", 2000)
            ]

            print("Descargando y distribuyendo rásteres GeoTIFF en ownCloud...", flush=True)
            for folder, img, name, scale in all_exports:
                try:
                    dl_url = img.clip(guatemala_geom).getDownloadURL({
                        'name': name,
                        'format': 'GEO_TIFF',
                        'scale': scale,
                        'crs': 'EPSG:4326',
                        'region': guatemala_geom
                    })
                    tif_res = requests.get(dl_url, timeout=40)
                    if tif_res.status_code == 200:
                        upload_to_owncloud(folder, f"{name}.tif", tif_res.content, 'image/tiff')
                except Exception as ex_l:
                    print(f"  Aviso en ráster {name}: {ex_l}", flush=True)

        except Exception as e:
            print(f"Nota: ownCloud finalizado con aviso ({e})", flush=True)

    print("\n✓ Proceso diario completado exitosamente.", flush=True)

except Exception as ex:
    print(f"🚨 ERROR CRÍTICO EN PIPELINE: {ex}", flush=True)
    traceback.print_exc()
    sys.exit(1)
