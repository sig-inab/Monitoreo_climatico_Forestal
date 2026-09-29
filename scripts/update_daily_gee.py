import os
import json
import re
import requests
import ee

# 1. AUTENTICACIÓN CON GOOGLE EARTH ENGINE EN GITHUB ACTIONS O LOCAL
def init_earth_engine():
    gee_key = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')
    if gee_key:
        print("Autenticando con Cuenta de Servicio de GEE...")
        key_dict = json.loads(gee_key)
        credentials = ee.ServiceAccountCredentials(
            key_dict.get('client_email'),
            key_data=gee_key
        )
        ee.Initialize(credentials, project='monitoreoclimaticoforestal')
    else:
        print("Autenticando con credenciales por defecto...")
        ee.Initialize(project='monitoreoclimaticoforestal')

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
land_mask = worldcover.neq(80) # Máscara para ocultar océanos

recent_chirps = chirps.sort('system:time_start', False).first().updateMask(land_mask)
recent_era5 = era5.sort('system:time_start', False).first().updateMask(land_mask)
recent_gfs = gfs.sort('system:time_start', False).first().updateMask(land_mask)

# Generación de URLs para las 18 capas
urls = {}

# 1. Precipitación Diaria CHIRPS (mm)
urls['precip'] = get_map_tile_url(
    recent_chirps.select('precipitation'),
    {'min': 0, 'max': 30, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']}
)

# 2. Temperatura ERA5 (°C)
urls['temp'] = get_map_tile_url(
    recent_era5.select('temperature_2m').subtract(273.15),
    {'min': 12, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']}
)

# 3. Viento GFS (kt)
urls['wind'] = get_map_tile_url(
    recent_gfs.select('u_component_of_wind_10m').pow(2).add(recent_gfs.select('v_component_of_wind_10m').pow(2)).sqrt().multiply(1.94384),
    {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']}
)

# 4. Humedad Suelo (0-7cm)
urls['soil'] = get_map_tile_url(
    recent_era5.select('soil_water_to_bottom_of_layer_1'),
    {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']}
)

# 5. Punto de Rocío ERA5 (°C)
urls['dew'] = get_map_tile_url(
    recent_era5.select('dewpoint_temperature_2m').subtract(273.15),
    {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']}
)

# 6. Acumulada Diaria GFS
urls['precipAccum'] = get_map_tile_url(
    recent_gfs.select('total_precipitation_surface'),
    {'min': 0, 'max': 20, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']}
)

# 7. Precipitación Mensual (30 días CHIRPS)
monthly_precip = chirps.filterDate(ee.Date(recent_chirps.get('system:time_start')).advance(-30, 'day'), ee.Date(recent_chirps.get('system:time_start'))).sum().updateMask(land_mask)
urls['precipMensual'] = get_map_tile_url(
    monthly_precip.select('precipitation'),
    {'min': 30, 'max': 300, 'palette': ['#74add1', '#4575b4', '#313695', '#2b8cbe', '#045a8d', '#023858']}
)

# 8. Humedad Suelo Radicular (7-28cm)
urls['soilRoot'] = get_map_tile_url(
    recent_era5.select('soil_water_to_bottom_of_layer_2'),
    {'min': 0.1, 'max': 0.5, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']}
)

# 9. NDVI Vigorosidad
recent_ndvi = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001).updateMask(land_mask)
urls['ndvi'] = get_map_tile_url(
    recent_ndvi,
    {'min': 0.1, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']}
)

# 10 & 11. ET Real & Potencial
recent_et = modis_et.sort('system:time_start', False).first().updateMask(land_mask)
urls['etReal'] = get_map_tile_url(
    recent_et.select('ET').multiply(0.1),
    {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']}
)
urls['etPot'] = get_map_tile_url(
    recent_et.select('PET').multiply(0.1),
    {'min': 0, 'max': 40, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']}
)

# 12 & 13. Pronósticos 7D y 16D GFS
gfs_7d = gfs.limit(56).select('total_precipitation_surface').sum().updateMask(land_mask)
gfs_16d = gfs.limit(128).select('total_precipitation_surface').sum().updateMask(land_mask)
urls['p7d'] = get_map_tile_url(gfs_7d, {'min': 0, 'max': 150, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})
urls['p16d'] = get_map_tile_url(gfs_16d, {'min': 0, 'max': 300, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})

# 14. Déficit Hídrico Gaussen (P - 2T)
gaussen = recent_chirps.select('precipitation').subtract(recent_era5.select('temperature_2m').subtract(273.15).multiply(2)).updateMask(land_mask)
urls['gaussen'] = get_map_tile_url(gaussen, {'min': -40, 'max': 20, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#e0f3f8', '#67a9cf', '#023858']})

# 15. Estrés Hídrico Foliar (Suave y Continuo 0 a 25)
urls['estres'] = get_map_tile_url(gaussen.multiply(-1).clamp(0, 25), {'min': 0, 'max': 15, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})

# 16. Aridez De Martonne
martonne = recent_chirps.select('precipitation').multiply(12).divide(recent_era5.select('temperature_2m').subtract(273.15).add(10)).updateMask(land_mask)
urls['martonne'] = get_map_tile_url(martonne, {'min': 5, 'max': 35, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#abd9e9', '#2c7bb6']})

# 17. Susceptibilidad Sequía
urls['suscSequia'] = get_map_tile_url(gaussen.multiply(-0.02).clamp(0, 1), {'min': 0.1, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})

# 18. Susceptibilidad Heladas
urls['suscHelada'] = get_map_tile_url(recent_era5.select('temperature_2m').subtract(273.15), {'min': -2, 'max': 14, 'palette': ['#1f6888', '#388385', '#529688', '#80a599', '#d4c4b0', '#b57448', '#a0562e']})

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
