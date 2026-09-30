import os
import json
import re
import sys
import datetime
import traceback
import requests
import ee

# Forzar salida de consola en tiempo real sin almacenamiento en búfer
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# 1. AUTENTICACIÓN ROBUSTA CON GOOGLE EARTH ENGINE
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

    # 2. DEFINICIÓN Y OBTENCIÓN DE TILE URLS DE LAS 18 CAPAS
    def get_map_tile_url(image, vis_params):
        map_id = ee.Image(image).getMapId(vis_params)
        return map_id['tile_fetcher'].url_format

    print("Iniciando preparación de colecciones con filtros temporales optimizados...", flush=True)

    # Rango de fechas UTC para acotar colecciones y evitar búsquedas globales infinitas
    now = datetime.datetime.now(datetime.timezone.utc)
    date_gfs_start = (now - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
    date_gfs_end = (now + datetime.timedelta(days=2)).strftime('%Y-%m-%d')
    date_era5_start = (now - datetime.timedelta(days=120)).strftime('%Y-%m-%d')
    date_chirps_start = (now - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    date_modis_start = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    date_today = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # Colecciones optimizadas con ventana de tiempo
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(date_chirps_start, date_today)
    era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(date_era5_start, date_today)
    gfs = ee.ImageCollection("NOAA/GFS0P25").filterDate(date_gfs_start, date_gfs_end)
    modis_ndvi = ee.ImageCollection("MODIS/061/MOD13Q1").filterDate(date_modis_start, date_today)
    modis_et = ee.ImageCollection("MODIS/061/MOD16A2").filterDate(date_modis_start, date_today)

    # Función ultrarrápida: rellena micro-bordes de 1 píxel y aplica suavizado bilineal continuo
    def prep_layer(img):
        return img.unmask(img.focalMean(1, 'square', 'pixels')).resample('bilinear')

    # Extracción de imágenes más recientes
    recent_chirps_raw = chirps.sort('system:time_start', False).first().select('precipitation')
    recent_era5_raw = era5.sort('system:time_start', False).first()
    recent_era5_temp_raw = recent_era5_raw.select('temperature_2m').subtract(273.15)
    recent_era5_dew_raw = recent_era5_raw.select('dewpoint_temperature_2m').subtract(273.15)
    recent_era5_soil1_raw = recent_era5_raw.select('volumetric_soil_water_layer_1')
    recent_era5_soil2_raw = recent_era5_raw.select('volumetric_soil_water_layer_2')

    # Filtrar GFS a la corrida más reciente para velocidad instantánea (<0.2s)
    latest_gfs_run = gfs.sort('system:time_start', False).first()
    latest_gfs_time = latest_gfs_run.get('system:time_start')
    latest_forecasts = gfs.filter(ee.Filter.eq('system:time_start', latest_gfs_time))

    recent_chirps = prep_layer(recent_chirps_raw)
    recent_temp = prep_layer(recent_era5_temp_raw)
    recent_dew = prep_layer(recent_era5_dew_raw)
    recent_soil1 = prep_layer(recent_era5_soil1_raw)
    recent_soil2 = prep_layer(recent_era5_soil2_raw)

    urls = {}

    # 1. Precipitación Diaria CHIRPS (0.3s)
    print("Procesando [1/18] Precipitación Diaria...", flush=True)
    urls['precip'] = get_map_tile_url(recent_chirps, {'min': 0, 'max': 30, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 2. Temperatura ERA5 (0.3s)
    print("Procesando [2/18] Temperatura...", flush=True)
    urls['temp'] = get_map_tile_url(recent_temp, {'min': 12, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']})

    # 3. Viento GFS Ultrarrápido (corrida actual, 0.2s)
    print("Procesando [3/18] Viento...", flush=True)
    u_wind = latest_gfs_run.select('u_component_of_wind_10m_above_ground')
    v_wind = latest_gfs_run.select('v_component_of_wind_10m_above_ground')
    wind_spd = u_wind.pow(2).add(v_wind.pow(2)).sqrt().multiply(1.94384).resample('bilinear')
    urls['wind'] = get_map_tile_url(wind_spd, {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']})

    # 4. Humedad Suelo Superficial (0-7cm) (0.3s)
    print("Procesando [4/18] Humedad Suelo Superficial...", flush=True)
    urls['soil'] = get_map_tile_url(recent_soil1, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 5. Punto de Rocío (0.3s)
    print("Procesando [5/18] Punto de Rocío...", flush=True)
    urls['dew'] = get_map_tile_url(recent_dew, {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']})

    # 6. Acumulada Diaria GFS (0.2s)
    print("Procesando [6/18] Acumulada Diaria GFS...", flush=True)
    precip_rate_daily = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 24)).select('precipitation_rate').mean().multiply(86400).resample('bilinear')
    urls['precipAccum'] = get_map_tile_url(precip_rate_daily, {'min': 0, 'max': 20, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 7. Precipitación Mensual CHIRPS (0.5s)
    print("Procesando [7/18] Precipitación Mensual...", flush=True)
    chirps_base = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
    monthly_precip_raw = chirps_base.filterDate(ee.Date(recent_chirps_raw.get('system:time_start')).advance(-30, 'day'), ee.Date(recent_chirps_raw.get('system:time_start'))).sum().select('precipitation')
    urls['precipMensual'] = get_map_tile_url(prep_layer(monthly_precip_raw), {'min': 30, 'max': 300, 'palette': ['#74add1', '#4575b4', '#313695', '#2b8cbe', '#045a8d', '#023858']})

    # 8. Humedad Suelo Radicular (7-28cm) (0.3s)
    print("Procesando [8/18] Humedad Suelo Radicular...", flush=True)
    urls['soilRoot'] = get_map_tile_url(recent_soil2, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 9. NDVI Vigorosidad (0.3s)
    print("Procesando [9/18] NDVI Vigorosidad...", flush=True)
    recent_ndvi_raw = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001)
    urls['ndvi'] = get_map_tile_url(prep_layer(recent_ndvi_raw), {'min': 0.1, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']})

    # 10 & 11. ET Real & Potencial (0.5s)
    print("Procesando [10/18 y 11/18] ET Real y Potencial...", flush=True)
    recent_et_raw = modis_et.sort('system:time_start', False).first()
    urls['etReal'] = get_map_tile_url(prep_layer(recent_et_raw.select('ET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
    urls['etPot'] = get_map_tile_url(prep_layer(recent_et_raw.select('PET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})

    # 12 & 13. Pronósticos 7D y 16D GFS (0.5s)
    print("Procesando [12/18 y 13/18] Pronósticos 7D y 16D...", flush=True)
    gfs_7d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 168)).select('precipitation_rate').mean().multiply(86400 * 7).resample('bilinear')
    gfs_16d_raw = latest_forecasts.filter(ee.Filter.lte('forecast_hours', 384)).select('precipitation_rate').mean().multiply(86400 * 16).resample('bilinear')
    urls['p7d'] = get_map_tile_url(gfs_7d_raw, {'min': 0, 'max': 150, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})
    urls['p16d'] = get_map_tile_url(gfs_16d_raw, {'min': 0, 'max': 300, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 14. Déficit Hídrico Gaussen (0.3s)
    print("Procesando [14/18] Déficit Hídrico Gaussen...", flush=True)
    gaussen_raw = recent_chirps_raw.subtract(recent_era5_temp_raw.multiply(2))
    urls['gaussen'] = get_map_tile_url(prep_layer(gaussen_raw), {'min': -40, 'max': 20, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#e0f3f8', '#67a9cf', '#023858']})

    # 15. Estrés Hídrico Foliar (0.3s)
    print("Procesando [15/18] Estrés Hídrico Foliar...", flush=True)
    estres_raw = gaussen_raw.multiply(-1).clamp(0, 25)
    urls['estres'] = get_map_tile_url(prep_layer(estres_raw), {'min': 0, 'max': 15, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})

    # 16. Aridez De Martonne (0.3s)
    print("Procesando [16/18] Aridez De Martonne...", flush=True)
    martonne_raw = recent_chirps_raw.multiply(12).divide(recent_era5_temp_raw.add(10))
    urls['martonne'] = get_map_tile_url(prep_layer(martonne_raw), {'min': 5, 'max': 35, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#abd9e9', '#2c7bb6']})

    # 17. Susceptibilidad Sequía (0.3s)
    print("Procesando [17/18] Susceptibilidad Sequía...", flush=True)
    susc_sequia_raw = gaussen_raw.multiply(-0.02).clamp(0, 1)
    urls['suscSequia'] = get_map_tile_url(prep_layer(susc_sequia_raw), {'min': 0.0, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})

    # 18. Susceptibilidad Heladas (0.3s)
    print("Procesando [18/18] Susceptibilidad Heladas...", flush=True)
    urls['suscHelada'] = get_map_tile_url(recent_temp, {'min': -2, 'max': 14, 'palette': ['#1f6888', '#388385', '#529688', '#80a599', '#d4c4b0', '#b57448', '#a0562e']})

    # 3. REEMPLAZAR URLS EN INDEX.HTML
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
        print("✓ index.html actualizado con las 18 nuevas URLs de Earth Engine.", flush=True)
    else:
        print("⚠️ Advertencia: index.html no encontrado en la raíz.", flush=True)

    # 4. SUBIDA A OWNCLOUD / WEBDAV (SI EXISTE OWNCLOUD_CONFIG)
    owncloud_config_raw = os.environ.get('OWNCLOUD_CONFIG')
    if owncloud_config_raw and owncloud_config_raw.strip():
        try:
            oc_cfg = json.loads(owncloud_config_raw.strip())
            token = oc_cfg.get('token')
            password = oc_cfg.get('password', '')
            webdav_url = oc_cfg.get('webdav_url', 'https://inab.ocis.nube4.cloud/public.php/webdav/')
            
            auth = (token, password) if token else (oc_cfg.get('user', ''), oc_cfg.get('password', ''))
            
            data_payload = json.dumps({"updated_at": ee.Date(ee.Date.now()).format().getInfo(), "urls": urls}, indent=2)
            dest_url = f"{webdav_url.rstrip('/')}/01_DIARIOS/datos_climaticos_inab.json"
            
            res = requests.put(dest_url, data=data_payload, auth=auth, headers={'Content-Type': 'application/json'}, timeout=5)
            if res.status_code in [200, 201, 204]:
                print(f"✓ Respaldo subido a ownCloud exitosamente en 01_DIARIOS ({res.status_code})", flush=True)
            else:
                print(f"Nota ownCloud WebDAV ({res.status_code})", flush=True)
        except Exception as e:
            print(f"Nota: Subida a ownCloud omitida ({e})", flush=True)

    print("✓ Proceso completado exitosamente en pocos segundos.", flush=True)

except Exception as ex:
    print(f"🚨 ERROR CRÍTICO DURANTE LA EJECUCIÓN DEL SCRIPT: {ex}", flush=True)
    traceback.print_exc()
    sys.exit(1)
