"""
=============================================================================
SISTEMA DE MONITOREO CLIMÁTICO FORESTAL — INAB / SIG-INAB
Pipeline Diario de Actualización Automatizada: GEE, ECMWF, CHIRPS y ANACAFE
=============================================================================
Migración a Modelo Europeo (ECMWF ERA5-Land), Downscaling Topoclimático SRTM (~300m),
Conservación de CHIRPS para Precipitación, Recolección Diaria de la Red ANACAFE,
Generación de GeoPackage (.gpkg) / GeoJSON y Distribución en ownCloud (01 a 05).
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

    def get_map_tile_url(image, vis_params):
        map_id = ee.Image(image).getMapId(vis_params)
        return map_id['tile_fetcher'].url_format

    print("\n--- Iniciando preparación de colecciones (ECMWF + CHIRPS + MODIS) ---", flush=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime('%Y-%m-%d')
    guatemala_geom = ee.Geometry.BBox(-92.25, 13.73, -88.22, 17.82)

    # Ventanas de tiempo optimizadas
    date_era5_start = (now - datetime.timedelta(days=120)).strftime('%Y-%m-%d')
    date_chirps_start = (now - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    date_modis_start = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    date_gfs_start = (now - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
    date_gfs_end = (now + datetime.timedelta(days=2)).strftime('%Y-%m-%d')
    date_today = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # Colecciones
    # 1. Precipitación Observada y Mensual: UCSB CHIRPS (Conservado según requerimiento técnico)
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(date_chirps_start, date_today)

    # 2. Variables Atmosféricas y de Suelo: ECMWF ERA5-Land Daily
    era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(date_era5_start, date_today)

    # 3. Vigor Vegetal y Evapotranspiración: MODIS Terra
    modis_ndvi = ee.ImageCollection("MODIS/061/MOD13Q1").filterDate(date_modis_start, date_today)
    modis_et = ee.ImageCollection("MODIS/061/MOD16A2").filterDate(date_modis_start, date_today)

    # 4. Pronóstico Numérico de Precipitación
    gfs = ee.ImageCollection("NOAA/GFS0P25").filterDate(date_gfs_start, date_gfs_end)

    # 5. Modelo Digital de Elevación para Downscaling Topoclimático
    #    Prioridad 1: DEM propio de 20 m subido como Asset (projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m)
    #    Prioridad 2: Capa precalculada de diferencia de elevación (projects/monitoreoclimaticoforestal/assets/delta_elevacion_era5land)
    #    Prioridad 3 (Fallback instantáneo): SRTM 30 m (USGS/SRTMGL1_003) - ETOPO1 suavizado a malla ERA5 (~9 km)
    existing_assets = set()
    try:
        asset_list = ee.data.listAssets({'parent': 'projects/monitoreoclimaticoforestal/assets'}).get('assets', [])
        existing_assets = {a.get('name', '').split('/')[-1] for a in asset_list}
    except Exception:
        pass

    coarse_dem = ee.Image("NOAA/NGDC/ETOPO1").select('bedrock').focalMean(5000, 'circle', 'meters')

    if 'delta_elevacion_20m' in existing_assets:
        print("✓ Usando capa precalculada delta_elevacion_20m (DEM propio 20 m)...", flush=True)
        delta_elevation = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_20m").select(0).unmask(0)
    elif 'DEM_Guatemala_20m' in existing_assets:
        print("✓ Usando DEM propio de 20 m (projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m)...", flush=True)
        fine_dem = ee.Image("projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m").select(0)
        delta_elevation = fine_dem.subtract(coarse_dem).unmask(0)
        # Lanzar precalculado automático de delta_elevacion_20m una sola vez si aún no existe
        try:
            ee.batch.Export.image.toAsset(
                image=delta_elevation.toFloat().rename('delta_elev'),
                description='precalc_delta_elevacion_20m',
                assetId='projects/monitoreoclimaticoforestal/assets/delta_elevacion_20m',
                region=guatemala_geom,
                scale=30,
                maxPixels=1e9
            ).start()
            print("  → Exportando delta_elevacion_20m precalculado en segundo plano para corridas futuras.", flush=True)
        except Exception:
            pass
    elif 'delta_elevacion_era5land' in existing_assets:
        print("✓ Usando capa precalculada delta_elevacion_era5land...", flush=True)
        delta_elevation = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_era5land").select(0).unmask(0)
    else:
        print("✓ Usando SRTM 30 m + ETOPO1 (downscaling instantáneo sin reduceResolution)...", flush=True)
        fine_dem = ee.Image("USGS/SRTMGL1_003").select('elevation')
        delta_elevation = fine_dem.subtract(coarse_dem).unmask(0)

    # Función para suavizado continuo y relleno de bordes
    def prep_layer(img):
        return img.unmask(img.focalMean(1, 'square', 'pixels')).resample('bicubic')

    # ── A. EXTRACCIÓN Y DOWNSCALING TOPOCLIMÁTICO DE DATOS EUROPEOS (ECMWF) ──
    print("Aplicando Downscaling Topoclimático sobre ERA5-Land...", flush=True)
    recent_chirps_raw = chirps.sort('system:time_start', False).first().select('precipitation')
    recent_era5_raw = era5.sort('system:time_start', False).first()

    # 1. Temperatura con gradiente adiabático ambiental (-0.0065 °C/m)
    temp_era5_c = recent_era5_raw.select('temperature_2m').resample('bicubic').subtract(273.15)
    temp_downscaled_300m = temp_era5_c.add(delta_elevation.multiply(-0.0065))

    # 2. Punto de Rocío con gradiente psicrométrico (-0.0045 °C/m)
    dew_era5_c = recent_era5_raw.select('dewpoint_temperature_2m').resample('bicubic').subtract(273.15)
    dew_downscaled_300m = dew_era5_c.add(delta_elevation.multiply(-0.0045))

    # 3. Humedad de Suelo ERA5-Land (0-7cm superficial y 7-28cm radicular)
    recent_era5_soil1_raw = recent_era5_raw.select('volumetric_soil_water_layer_1')
    recent_era5_soil2_raw = recent_era5_raw.select('volumetric_soil_water_layer_2')
    recent_soil1_300m = recent_era5_soil1_raw.resample('bicubic')
    recent_soil2_300m = recent_era5_soil2_raw.resample('bicubic')

    # 4. Viento ECMWF ERA5-Land (u, v a 10m convertido a nudos)
    u_wind_ecmwf = recent_era5_raw.select('u_component_of_wind_10m')
    v_wind_ecmwf = recent_era5_raw.select('v_component_of_wind_10m')
    wind_spd_ecmwf = u_wind_ecmwf.pow(2).add(v_wind_ecmwf.pow(2)).sqrt().multiply(1.94384).resample('bicubic')

    # 5. Pronósticos GFS / ECMWF
    latest_gfs_run = gfs.sort('system:time_start', False).first()
    latest_gfs_time = latest_gfs_run.get('system:time_start')
    latest_forecasts = gfs.filter(ee.Filter.eq('system:time_start', latest_gfs_time))

    recent_chirps = prep_layer(recent_chirps_raw)
    # Capas topoclimáticas compuestas (ERA5 + SRTM): ya interpoladas en origen, solo se rellenan bordes costeros
    recent_temp = temp_downscaled_300m.unmask(temp_downscaled_300m.focalMean(2, 'square', 'pixels'))
    recent_dew = dew_downscaled_300m.unmask(dew_downscaled_300m.focalMean(2, 'square', 'pixels'))
    recent_soil1 = prep_layer(recent_soil1_300m)
    recent_soil2 = prep_layer(recent_soil2_300m)

    urls = {}

    # 1. Precipitación Diaria CHIRPS (Conservado)
    print("Procesando [1/18] Precipitación Diaria CHIRPS...", flush=True)
    urls['precip'] = get_map_tile_url(recent_chirps, {'min': 0, 'max': 30, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 2. Temperatura ECMWF ERA5-Land Topoclimática 300m
    print("Procesando [2/18] Temperatura ECMWF Topoclimática (300m)...", flush=True)
    urls['temp'] = get_map_tile_url(recent_temp, {'min': 10, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']})

    # 3. Viento ECMWF ERA5-Land
    print("Procesando [3/18] Viento ECMWF...", flush=True)
    urls['wind'] = get_map_tile_url(prep_layer(wind_spd_ecmwf), {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']})

    # 4. Humedad Suelo Superficial ECMWF (0-7cm)
    print("Procesando [4/18] Humedad Suelo Superficial ECMWF...", flush=True)
    urls['soil'] = get_map_tile_url(recent_soil1, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 5. Punto de Rocío ECMWF Topoclimático (300m)
    print("Procesando [5/18] Punto de Rocío ECMWF...", flush=True)
    urls['dew'] = get_map_tile_url(recent_dew, {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']})

    # 6. Acumulada Diaria GFS/ECMWF
    print("Procesando [6/18] Acumulada Diaria...", flush=True)
    precip_rate_daily = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 24)).select('precipitation_rate').mean().multiply(86400).resample('bicubic')
    urls['precipAccum'] = get_map_tile_url(precip_rate_daily, {'min': 0, 'max': 20, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 7. Precipitación Mensual Acumulada CHIRPS (Conservado)
    print("Procesando [7/18] Precipitación Mensual CHIRPS (30 días)...", flush=True)
    chirps_base = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
    monthly_precip_raw = chirps_base.filterDate(
        ee.Date(recent_chirps_raw.get('system:time_start')).advance(-30, 'day'),
        ee.Date(recent_chirps_raw.get('system:time_start'))
    ).sum().select('precipitation')
    urls['precipMensual'] = get_map_tile_url(prep_layer(monthly_precip_raw), {'min': 30, 'max': 300, 'palette': ['#74add1', '#4575b4', '#313695', '#2b8cbe', '#045a8d', '#023858']})

    # 8. Humedad Suelo Radicular ECMWF (7-28cm)
    print("Procesando [8/18] Humedad Suelo Radicular ECMWF...", flush=True)
    urls['soilRoot'] = get_map_tile_url(recent_soil2, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 9. NDVI MODIS
    print("Procesando [9/18] NDVI MODIS Vigorosidad...", flush=True)
    recent_ndvi_raw = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001)
    urls['ndvi'] = get_map_tile_url(prep_layer(recent_ndvi_raw), {'min': 0.1, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']})

    # 10 & 11. ET Real & Potencial MODIS
    print("Procesando [10/18 y 11/18] Evapotranspiración MODIS...", flush=True)
    recent_et_raw = modis_et.sort('system:time_start', False).first()
    urls['etReal'] = get_map_tile_url(prep_layer(recent_et_raw.select('ET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
    urls['etPot'] = get_map_tile_url(prep_layer(recent_et_raw.select('PET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})

    # 12 & 13. Pronósticos 7D y 16D Suavizados
    print("Procesando [12/18 y 13/18] Pronósticos 7D y 16D...", flush=True)
    gfs_7d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 168)).select('precipitation_rate').mean().multiply(86400 * 7).resample('bicubic')
    gfs_16d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 384)).select('precipitation_rate').mean().multiply(86400 * 16).resample('bicubic')
    urls['p7d'] = get_map_tile_url(gfs_7d_raw, {'min': 0, 'max': 150, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})
    urls['p16d'] = get_map_tile_url(gfs_16d_raw, {'min': 0, 'max': 300, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 14. Déficit Hídrico Gaussen (CHIRPS vs ECMWF 300m)
    print("Procesando [14/18] Déficit Hídrico Gaussen...", flush=True)
    gaussen_raw = recent_chirps_raw.subtract(temp_downscaled_300m.multiply(2))
    urls['gaussen'] = get_map_tile_url(prep_layer(gaussen_raw), {'min': -40, 'max': 20, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#e0f3f8', '#67a9cf', '#023858']})

    # 15. Estrés Hídrico Foliar (ECMWF Suelo)
    print("Procesando [15/18] Estrés Hídrico Foliar...", flush=True)
    soil_pct = recent_era5_soil1_raw.add(recent_era5_soil2_raw).multiply(50)
    estres_raw = ee.Image(36).subtract(soil_pct).multiply(0.45).clamp(0, 8)
    urls['estres'] = get_map_tile_url(prep_layer(estres_raw), {'min': 0, 'max': 8, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})

    # 16. Aridez De Martonne (CHIRPS vs ECMWF)
    print("Procesando [16/18] Aridez De Martonne...", flush=True)
    martonne_raw = recent_chirps_raw.multiply(12).divide(temp_downscaled_300m.add(10))
    urls['martonne'] = get_map_tile_url(prep_layer(martonne_raw), {'min': 5, 'max': 35, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#abd9e9', '#2c7bb6']})

    # 17. Susceptibilidad a Sequía
    print("Procesando [17/18] Susceptibilidad Sequía...", flush=True)
    susc_sequia_raw = ee.Image(38).subtract(soil_pct).multiply(0.035).clamp(0.08, 0.95)
    urls['suscSequia'] = get_map_tile_url(prep_layer(susc_sequia_raw), {'min': 0.1, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})

    # 18. Susceptibilidad a Heladas (ECMWF Temperatura Topoclimática 300m)
    print("Procesando [18/18] Susceptibilidad Heladas...", flush=True)
    urls['suscHelada'] = get_map_tile_url(recent_temp, {'min': -2, 'max': 14, 'palette': ['#1f6888', '#388385', '#529688', '#80a599', '#d4c4b0', '#b57448', '#a0562e']})

    # ── 2. ACTUALIZACIÓN AUTOMÁTICA DE INDEX.HTML ──
    index_path = 'index.html'
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        for key, new_url in urls.items():
            pattern = rf"{key}:\s*'https://earthengine\.googleapis\.com/v1/projects/[^']+'"
            replacement = f"{key}: '{new_url}'"
            html_content = re.sub(pattern, replacement, html_content)

        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print("✓ index.html actualizado con las 18 URLs optimizadas.", flush=True)

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

            # Consultar observaciones de los últimos 2 días
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
                            if not (val != val): # not NaN
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

                # Detalle de mediciones individuales para CSV completo
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

        # Descarga multihilo rápida
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

        # C. Crear Geodatabase Estándar OGC GeoPackage (.gpkg) usando SQLite nativo
        # Se genera en carpeta temporal para no ensuciar el repositorio (no se comitea)
        temp_gpkg = os.path.join(tempfile.gettempdir(), f'ANACAFE_Estaciones_Meteorologicas_{date_str}.gpkg')
        if os.path.exists(temp_gpkg):
            os.remove(temp_gpkg)

        conn = sqlite3.connect(temp_gpkg)
        cur = conn.cursor()

        # Tablas de metadatos OGC GeoPackage
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

        # Función para construir binario OGC GeoPackage Point (Header GPKG + WKB estándar)
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
                temp_downscaled_300m.rename('ecmwf_temp_300m'),
                recent_chirps_raw.rename('chirps_rain'),
                recent_soil1_300m.multiply(100).rename('ecmwf_soil_pct'),
                dew_downscaled_300m.rename('ecmwf_dew')
            ]).reduceRegions(
                collection=calib_fc,
                reducer=ee.Reducer.first(),
                scale=300
            )

            res = sampled.getInfo().get('features', [])
            calib_lines = ["ID_Estacion,Nombre,Fecha,Temp_ANACAFE,Temp_ECMWF_300m,Sesgo_Temp_C,Lluvia_ANACAFE,Lluvia_CHIRPS,Sesgo_Lluvia_mm,Suelo_ANACAFE,Suelo_ECMWF,Sesgo_Suelo_pct"]
            for f in res:
                p = f.get('properties', {})
                sid = p.get('st_id', '')
                snom = p.get('st_nombre', '')
                t_obs = round(p.get('obs_temp', 0) or 0, 1)
                t_mod = round(p.get('ecmwf_temp_300m', 0) or 0, 1)
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

            # A. Metadatos Diarios y Resumen JSON (en 01_DIARIOS)
            data_payload = json.dumps({
                "fecha": date_str,
                "actualizado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "modelo_atmosferico": "ECMWF ERA5-Land (Downscaling Topoclimático SRTM ~300m)",
                "modelo_precipitacion": "UCSB CHIRPS Daily v2.0",
                "urls": urls
            }, indent=2)
            upload_to_owncloud("01_DIARIOS", "datos_climaticos_inab.json", data_payload.encode('utf-8'), 'application/json')

            # B. Subir Productos de la Red ANACAFE a la Carpeta 05_ESTACIONES
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

            # C. Distribución Completa de Rásteres GeoTIFF (.tif)
            all_exports = [
                # 01_DIARIOS (Variables diarias ECMWF y CHIRPS)
                ("01_DIARIOS", recent_chirps_raw, f"CHIRPS_precipitacion_diaria_{date_str}", 5500),
                ("01_DIARIOS", temp_downscaled_300m, f"ECMWF_temperatura_300m_{date_str}", 9000),
                ("01_DIARIOS", recent_soil1_300m, f"ECMWF_humedad_suelo_superficial_{date_str}", 9000),
                ("01_DIARIOS", recent_soil2_300m, f"ECMWF_humedad_suelo_radicular_{date_str}", 9000),
                ("01_DIARIOS", dew_downscaled_300m, f"ECMWF_punto_rocio_{date_str}", 9000),
                ("01_DIARIOS", wind_spd_ecmwf, f"ECMWF_viento_velocidad_{date_str}", 9000),
                ("01_DIARIOS", gaussen_raw, f"Gaussen_deficit_hidrico_{date_str}", 9000),
                ("01_DIARIOS", estres_raw, f"Estres_hidrico_foliar_{date_str}", 9000),
                ("01_DIARIOS", susc_sequia_raw, f"Susceptibilidad_sequia_{date_str}", 9000),
                ("01_DIARIOS", temp_downscaled_300m, f"Susceptibilidad_heladas_{date_str}", 9000),

                # 02_SEMANALES (Compuestos MODIS)
                ("02_SEMANALES", recent_ndvi_raw.toFloat(), f"MODIS_ndvi_vigor_16D_{date_str}", 2000),
                ("02_SEMANALES", recent_et_raw.select('ET').multiply(0.1).toFloat(), f"MODIS_evapotranspiracion_real_8D_{date_str}", 2000),
                ("02_SEMANALES", recent_et_raw.select('PET').multiply(0.1).toFloat(), f"MODIS_evapotranspiracion_potencial_8D_{date_str}", 2000),

                # 03_MENSUALES (Acumulados CHIRPS y balances)
                ("03_MENSUALES", monthly_precip_raw, f"CHIRPS_precipitacion_mensual_acumulada_{date_str}", 5500),
                ("03_MENSUALES", martonne_raw, f"DeMartonne_indice_aridez_{date_str}", 9000),

                # 04_PRONOSTICOS_ESTACIONALES
                ("04_PRONOSTICOS_ESTACIONALES", gfs_7d_raw, f"Pronostico_lluvia_7D_{date_str}", 15000),
                ("04_PRONOSTICOS_ESTACIONALES", gfs_16d_raw, f"Pronostico_lluvia_16D_{date_str}", 15000)
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
