import os
import json
import re
import sys
import traceback
import requests
import ee

# 1. AUTENTICACIÓN ROBUSTA CON GOOGLE EARTH ENGINE EN GITHUB ACTIONS O LOCAL
def init_earth_engine():
    gee_key = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')
    if gee_key and gee_key.strip():
        print("Autenticando con Cuenta de Servicio de GEE desde Secret...")
        gee_key_clean = gee_key.strip()
        try:
            key_dict = json.loads(gee_key_clean)
        except Exception as e:
            print(f"ERROR al decodificar JSON del secret GEE_SERVICE_ACCOUNT_KEY: {e}")
            sys.exit(1)

        # Guardar en archivo temporal para asegurar compatibilidad 100% con google-auth y GEE API
        temp_key_file = 'temp_gee_key.json'
        with open(temp_key_file, 'w', encoding='utf-8') as f:
            f.write(gee_key_clean)

        try:
            credentials = ee.ServiceAccountCredentials(
                key_dict.get('client_email'),
                key_file=temp_key_file
            )
            ee.Initialize(credentials=credentials, project='monitoreoclimaticoforestal')
            print("✓ Autenticación GEE exitosa.")
        finally:
            if os.path.exists(temp_key_file):
                os.remove(temp_key_file)
    else:
        print("Autenticando con credenciales por defecto...")
        ee.Initialize(project='monitoreoclimaticoforestal')

try:
    init_earth_engine()

    # 2. DEFINICIÓN Y OBTENCIÓN DE TILE URLS DE LAS 18 CAPAS
    def get_map_tile_url(image, vis_params):
        map_id = ee.Image(image).getMapId(vis_params)
        return map_id['tile_fetcher'].url_format

    print("Generando nuevos tokens de mapas de Google Earth Engine...")

    # Cargar datasets
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
    era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
    gfs = ee.ImageCollection("NOAA/GFS0P25")
    modis_ndvi = ee.ImageCollection("MODIS/061/MOD13Q1")
    modis_et = ee.ImageCollection("MODIS/061/MOD16A2")
    worldcover = ee.Image("ESA/WorldCover/v200/2020").select('map')

    # Máscara continental suavizada y dilatada 1km para evitar bordes costeros recortados agresivamente
    land_mask = worldcover.neq(80).focalMax(1000, 'circle', 'meters')

    # Función auxiliar nativa de GEE para suavizar e interpolar capas ricas en cobertura sobre tierra sin huecos
    def prep_layer(img):
        # Rellenar micro-huecos con media focal de 10km nativa y suavizado bilineal continuo
        unmasked = img.unmask(img.focalMean(10000, 'circle', 'meters'))
        return unmasked.resample('bilinear').updateMask(land_mask)

    recent_chirps_raw = chirps.sort('system:time_start', False).first().select('precipitation')
    recent_era5_raw = era5.sort('system:time_start', False).first()
    recent_era5_temp_raw = recent_era5_raw.select('temperature_2m').subtract(273.15)
    recent_era5_dew_raw = recent_era5_raw.select('dewpoint_temperature_2m').subtract(273.15)
    recent_era5_soil1_raw = recent_era5_raw.select('soil_water_to_bottom_of_layer_1')
    recent_era5_soil2_raw = recent_era5_raw.select('soil_water_to_bottom_of_layer_2')
    recent_gfs_raw = gfs.sort('system:time_start', False).first()

    recent_chirps = prep_layer(recent_chirps_raw)
    recent_temp = prep_layer(recent_era5_temp_raw)
    recent_dew = prep_layer(recent_era5_dew_raw)
    recent_soil1 = prep_layer(recent_era5_soil1_raw)
    recent_soil2 = prep_layer(recent_era5_soil2_raw)

    urls = {}

    # 1. Precipitación Diaria CHIRPS
    urls['precip'] = get_map_tile_url(recent_chirps, {'min': 0, 'max': 30, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 2. Temperatura ERA5
    urls['temp'] = get_map_tile_url(recent_temp, {'min': 12, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']})

    # 3. Viento GFS
    wind_spd = recent_gfs_raw.select('u_component_of_wind_10m').pow(2).add(recent_gfs_raw.select('v_component_of_wind_10m').pow(2)).sqrt().multiply(1.94384)
    urls['wind'] = get_map_tile_url(prep_layer(wind_spd), {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']})

    # 4. Humedad Suelo (0-7cm)
    urls['soil'] = get_map_tile_url(recent_soil1, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 5. Punto de Rocío
    urls['dew'] = get_map_tile_url(recent_dew, {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']})

    # 6. Acumulada Diaria GFS
    urls['precipAccum'] = get_map_tile_url(prep_layer(recent_gfs_raw.select('total_precipitation_surface')), {'min': 0, 'max': 20, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 7. Precipitación Mensual (30 días CHIRPS)
    monthly_precip_raw = chirps.filterDate(ee.Date(recent_chirps_raw.get('system:time_start')).advance(-30, 'day'), ee.Date(recent_chirps_raw.get('system:time_start'))).sum().select('precipitation')
    urls['precipMensual'] = get_map_tile_url(prep_layer(monthly_precip_raw), {'min': 30, 'max': 300, 'palette': ['#74add1', '#4575b4', '#313695', '#2b8cbe', '#045a8d', '#023858']})

    # 8. Humedad Suelo Radicular (7-28cm)
    urls['soilRoot'] = get_map_tile_url(recent_soil2, {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})

    # 9. NDVI Vigorosidad
    recent_ndvi_raw = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001)
    urls['ndvi'] = get_map_tile_url(prep_layer(recent_ndvi_raw), {'min': 0.1, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']})

    # 10 & 11. ET Real & Potencial
    recent_et_raw = modis_et.sort('system:time_start', False).first()
    urls['etReal'] = get_map_tile_url(prep_layer(recent_et_raw.select('ET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
    urls['etPot'] = get_map_tile_url(prep_layer(recent_et_raw.select('PET').multiply(0.1)), {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})

    # 12 & 13. Pronósticos 7D y 16D GFS
    gfs_7d_raw = gfs.limit(56).select('total_precipitation_surface').sum()
    gfs_16d_raw = gfs.limit(128).select('total_precipitation_surface').sum()
    urls['p7d'] = get_map_tile_url(prep_layer(gfs_7d_raw), {'min': 0, 'max': 150, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})
    urls['p16d'] = get_map_tile_url(prep_layer(gfs_16d_raw), {'min': 0, 'max': 300, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

    # 14. Déficit Hídrico Gaussen
    gaussen_raw = recent_chirps_raw.subtract(recent_era5_temp_raw.multiply(2))
    urls['gaussen'] = get_map_tile_url(prep_layer(gaussen_raw), {'min': -40, 'max': 20, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#e0f3f8', '#67a9cf', '#023858']})

    # 15. Estrés Hídrico Foliar
    estres_raw = gaussen_raw.multiply(-1).clamp(0, 25)
    urls['estres'] = get_map_tile_url(prep_layer(estres_raw), {'min': 0, 'max': 15, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})

    # 16. Aridez De Martonne
    martonne_raw = recent_chirps_raw.multiply(12).divide(recent_era5_temp_raw.add(10))
    urls['martonne'] = get_map_tile_url(prep_layer(martonne_raw), {'min': 5, 'max': 35, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#abd9e9', '#2c7bb6']})

    # 17. Susceptibilidad Sequía
    susc_sequia_raw = gaussen_raw.multiply(-0.02).clamp(0, 1)
    urls['suscSequia'] = get_map_tile_url(prep_layer(susc_sequia_raw), {'min': 0.1, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})

    # 18. Susceptibilidad Heladas
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
        print("✓ index.html actualizado con las 18 nuevas URLs de Earth Engine.")

    # 4. SUBIDA OPCIONAL A OWNCLOUD / WEBDAV
    OWNCLOUD_PUBLIC_TOKEN = "d4d832be-c204-4791-97e8-ff42cce76a97"
    WEBDAV_URL = f"https://inab.ocis.nube4.cloud/public.php/webdav/datos_climaticos_inab.json"

    try:
        data_payload = json.dumps({"updated_at": ee.Date(ee.Date.now()).format().getInfo(), "urls": urls}, indent=2)
        res = requests.put(WEBDAV_URL, data=data_payload, auth=(OWNCLOUD_PUBLIC_TOKEN, ''), headers={'Content-Type': 'application/json'})
        if res.status_code in [200, 201, 204]:
            print("✓ Archivo de respaldo subido exitosamente a ownCloud vía WebDAV.")
        else:
            print(f"Respuesta ownCloud WebDAV ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Nota: Subida a ownCloud WebDAV omitida o no requerida ({e})")

except Exception as ex:
    print(f"🚨 ERROR CRÍTICO DURANTE LA EJECUCIÓN DEL SCRIPT: {ex}")
    traceback.print_exc()
    sys.exit(1)
