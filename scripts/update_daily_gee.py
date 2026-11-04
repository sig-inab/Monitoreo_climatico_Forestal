import json
import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
import datetime
import ee

KEY_PATH = r"d:\mercy.aguilar\Documents\CAMBIO_CLIMATICO\monitoreoclimaticoforestal-08a9fb17b31f.json"
credentials = ee.ServiceAccountCredentials('', key_file=KEY_PATH)
ee.Initialize(credentials, project='monitoreoclimaticoforestal')

print("✓ Conectado a Google Earth Engine con monitoreoclimaticoforestal.", flush=True)

# 1. Cargar el Buffer oficial de 1 km de INAB (GuatemalaBuffer1km_WGS84_simplified.geojson)
buffer_json_path = r"C:\Users\mercy.aguilar\.gemini\antigravity\brain\e84d3f6a-800e-4bd6-a4ba-dd09d0461c2f\scratch\buffer_wgs84_simplified.geojson"
with open(buffer_json_path, "r", encoding="utf-8") as f:
    buf_data = json.load(f)

inab_coords = buf_data['features'][0]['geometry']['coordinates']
inab_buffer_geom = ee.Geometry.Polygon(inab_coords)
inab_mask = ee.Image.constant(1).clip(inab_buffer_geom).mask()

grid_bbox = [-92.30, 13.70, -88.15, 17.85]
grid_geom = ee.Geometry.BBox(*grid_bbox)

def get_map_tile_url(image, vis_params):
    map_id = ee.Image(image).clip(inab_buffer_geom).getMapId(vis_params)
    return map_id['tile_fetcher'].url_format

now = datetime.datetime.now(datetime.timezone.utc)
date_str = now.strftime('%Y-%m-%d')
date_era5_start = (now - datetime.timedelta(days=120)).strftime('%Y-%m-%d')
date_chirps_start = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
date_modis_start = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
date_s5p_start = (now - datetime.timedelta(days=15)).strftime('%Y-%m-%d')
date_today = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(date_chirps_start, date_today)
era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(date_era5_start, date_today)
modis_ndvi = ee.ImageCollection("MODIS/061/MOD13Q1").filterDate(date_modis_start, date_today)

# Colección Europea Copernicus Sentinel-5P para Calidad del Aire (Índice de Aerosoles UV / CAMS)
copernicus_aer = ee.ImageCollection("COPERNICUS/S5P/NRTI/L3_AER_AI").filterDate(date_s5p_start, date_today)

existing_assets = set()
try:
    asset_list = ee.data.listAssets({'parent': 'projects/monitoreoclimaticoforestal/assets'}).get('assets', [])
    existing_assets = {a.get('name', '').split('/')[-1] for a in asset_list}
except Exception:
    pass

srtm_raw = ee.Image("USGS/SRTMGL1_003").select('elevation')
srtm_30m = srtm_raw.unmask(0)
terrain_slope = ee.Terrain.slope(srtm_raw).unmask(0)

# MÁSCARA ESTRICTA CON BUFFER DE 1 KM DE INAB
land_mask = srtm_30m.gt(0).And(inab_mask)

if 'DEM_Guatemala_20m' in existing_assets:
    dem_20m = ee.Image("projects/monitoreoclimaticoforestal/assets/DEM_Guatemala_20m").select(0)
    fine_dem = dem_20m.unmask(srtm_30m)
else:
    fine_dem = srtm_30m

if 'delta_elevacion_20m' in existing_assets:
    delta_elevation = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_20m").select(0).unmask(0)
elif 'delta_elevacion_era5land' in existing_assets:
    delta_30m = ee.Image("projects/monitoreoclimaticoforestal/assets/delta_elevacion_era5land").select(0).unmask(0)
    delta_elevation = delta_30m.add(fine_dem.subtract(srtm_30m)).unmask(0)
else:
    coarse_dem = ee.Image("NOAA/NGDC/ETOPO1").select('bedrock').resample('bilinear')
    delta_elevation = fine_dem.subtract(coarse_dem).unmask(0)

def clean_coarse(img, radius_px=2):
    filled = img.unmask(img.focalMean(radius_px, 'circle', 'pixels'))
    return filled.resample('bilinear').updateMask(land_mask)

print("Calculando capas climáticas topoclimáticas...", flush=True)
recent_chirps_raw = chirps.sort('system:time_start', False).first().select('precipitation')
last_chirps_date = ee.Date(recent_chirps_raw.get('system:time_start'))

monthly_precip_raw = chirps.filterDate(
    last_chirps_date.advance(-30, 'day'),
    last_chirps_date.advance(1, 'day')
).sum().select('precipitation')

recent_era5_raw = era5.sort('system:time_start', False).first()
last_era5_date = ee.Date(recent_era5_raw.get('system:time_start'))
era5_8d = era5.filterDate(last_era5_date.advance(-8, 'day'), last_era5_date.advance(1, 'day'))

oro_factor = ee.Image(1).add(delta_elevation.multiply(0.00025)).add(terrain_slope.multiply(0.004)).clamp(0.72, 1.48)

recent_chirps = clean_coarse(recent_chirps_raw, 1).multiply(oro_factor).clamp(0, 300).updateMask(land_mask)
monthly_precip = clean_coarse(monthly_precip_raw, 1).multiply(oro_factor).clamp(0, 1500).updateMask(land_mask)

temp_base_c = clean_coarse(recent_era5_raw.select('temperature_2m').subtract(273.15))
recent_temp = temp_base_c.add(delta_elevation.multiply(-0.0065)).updateMask(land_mask)

temp_min_base_c = clean_coarse(recent_era5_raw.select('temperature_2m_min').subtract(273.15))
recent_temp_min = temp_min_base_c.add(delta_elevation.multiply(-0.0065)).updateMask(land_mask)

dew_base_c = clean_coarse(recent_era5_raw.select('dewpoint_temperature_2m').subtract(273.15))
recent_dew = dew_base_c.add(delta_elevation.multiply(-0.0045)).updateMask(land_mask)

u_wind_ecmwf = recent_era5_raw.select('u_component_of_wind_10m')
v_wind_ecmwf = recent_era5_raw.select('v_component_of_wind_10m')
wind_base_kt = clean_coarse(u_wind_ecmwf.hypot(v_wind_ecmwf).multiply(1.94384))
recent_wind = wind_base_kt.add(delta_elevation.multiply(0.003)).add(terrain_slope.multiply(0.08)).clamp(0, 80).updateMask(land_mask)

soil_topo_adj = delta_elevation.multiply(0.00006).subtract(terrain_slope.multiply(0.0022))
soil1_base = clean_coarse(recent_era5_raw.select('volumetric_soil_water_layer_1'))
soil2_base = clean_coarse(recent_era5_raw.select('volumetric_soil_water_layer_2'))
recent_soil1 = soil1_base.add(soil_topo_adj).clamp(0.10, 0.52).updateMask(land_mask)
recent_soil2 = soil2_base.add(soil_topo_adj.multiply(0.85)).clamp(0.10, 0.52).updateMask(land_mask)

# NDVI MODIS reproyectado WGS84
recent_ndvi_raw = modis_ndvi.sort('system:time_start', False).first().select('NDVI').multiply(0.0001)
recent_ndvi = recent_ndvi_raw.reproject('EPSG:4326', None, 250).updateMask(land_mask).clip(inab_buffer_geom)

et_real_base = clean_coarse(era5_8d.select('total_evaporation_sum').sum().multiply(-1000))
et_pot_base = clean_coarse(era5_8d.select('potential_evaporation_sum').sum().multiply(-1000))
recent_et_real = et_real_base.add(delta_elevation.multiply(-0.0035)).subtract(terrain_slope.multiply(0.08)).clamp(5, 45).updateMask(land_mask)
recent_et_pot = et_pot_base.add(delta_elevation.multiply(-0.012)).subtract(terrain_slope.multiply(0.15)).clamp(15, 110).updateMask(land_mask)

recent_gaussen = monthly_precip.subtract(recent_temp.multiply(2)).updateMask(land_mask)
soil_pct_20m = recent_soil1.add(recent_soil2).multiply(50)
recent_estres = ee.Image(42).subtract(soil_pct_20m).multiply(0.45).clamp(0, 8).updateMask(land_mask)
recent_martonne = monthly_precip.multiply(12).divide(recent_temp.add(10).max(1)).updateMask(land_mask)
recent_susc_sequia = ee.Image(44).subtract(soil_pct_20m).multiply(0.038).clamp(0.08, 0.95).updateMask(land_mask)

# NUEVA CAPA: Susceptibilidad a Deslizamientos Forestales
slope_factor = terrain_slope.divide(35).clamp(0, 1)
soil_sat_factor = recent_soil1.subtract(0.20).divide(0.28).clamp(0, 1)
rain_factor = monthly_precip.divide(300).clamp(0, 1)
veg_protection = ee.Image(1.2).subtract(recent_ndvi.multiply(0.4)).clamp(0.7, 1.2)
recent_susc_desliz = slope_factor.multiply(0.45).add(soil_sat_factor.multiply(0.30)).add(rain_factor.multiply(0.25)).multiply(veg_protection).clamp(0.05, 0.95).updateMask(land_mask)

# NUEVA CAPA: Calidad del Aire - Índice de Aerosoles Copernicus Sentinel-5P (CAMS Europeo)
recent_cams_raw = copernicus_aer.sort('system:time_start', False).first().select('absorbing_aerosol_index')
recent_cams_air = clean_coarse(recent_cams_raw, 2).clamp(-1.0, 4.0).updateMask(land_mask)

# CONTORNOS: Isotermas a cada 10 °C (línea delgada 1px) e Isoyetas a cada 10 mm (línea delgada 1px)
elev_smooth_iso = srtm_30m.focalMean(1200, 'circle', 'meters')
coarse_elev_img = srtm_30m.subtract(delta_elevation)
temp_iso_field = temp_base_c.add(elev_smooth_iso.subtract(coarse_elev_img).multiply(-0.0065))
isotermas_edges = ee.Algorithms.CannyEdgeDetector(temp_iso_field.divide(10).floor(), 0.4, 0).updateMask(land_mask)

rain_iso_field = clean_coarse(recent_chirps_raw, 1).focalMean(1000, 'circle', 'meters')
isoyetas_edges = ee.Algorithms.CannyEdgeDetector(rain_iso_field.divide(10).floor(), 0.4, 0).updateMask(land_mask)

urls = {}
print("Generando URLs de tiles recortados con GuatemalaBuffer1km...", flush=True)
urls['precip'] = get_map_tile_url(recent_chirps, {'min': 0, 'max': 25, 'palette': ['#e0f3f8', '#91bfdb', '#67a9cf', '#2b8cbe', '#045a8d', '#023858']})
urls['temp'] = get_map_tile_url(recent_temp, {'min': 10, 'max': 36, 'palette': ['#ffffcc', '#ffea46', '#ffaa00', '#ff5500', '#e60000', '#990000']})
urls['wind'] = get_map_tile_url(recent_wind, {'min': 0, 'max': 35, 'palette': ['#5e4fa2', '#3288bd', '#66c2a5', '#abdda4', '#e6f598', '#fee08b', '#f46d43', '#9e0142']})
urls['soil'] = get_map_tile_url(recent_soil1, {'min': 0.22, 'max': 0.48, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})
urls['dew'] = get_map_tile_url(recent_dew, {'min': 5, 'max': 25, 'palette': ['#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#1a9850']})
urls['precipMensual'] = get_map_tile_url(monthly_precip, {'min': 50, 'max': 380, 'palette': ['#e0f3f8', '#74add1', '#4575b4', '#313695', '#045a8d', '#023858']})
urls['soilRoot'] = get_map_tile_url(recent_soil2, {'min': 0.22, 'max': 0.48, 'palette': ['#8c510a', '#d8b365', '#f6e8c3', '#c7edd5', '#5ab4ac', '#01665e']})
urls['ndvi'] = get_map_tile_url(recent_ndvi, {'min': 0.15, 'max': 0.85, 'palette': ['#ffffe5', '#f7fcb9', '#d9f0a3', '#addd8e', '#78c679', '#31a354', '#006837']})
urls['etReal'] = get_map_tile_url(recent_et_real, {'min': 12, 'max': 36, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
urls['etPot'] = get_map_tile_url(recent_et_pot, {'min': 25, 'max': 95, 'palette': ['#fff7ec', '#fee8c8', '#fdd49e', '#fdbb84', '#fc8d59', '#ef6548', '#d7301f', '#990000']})
urls['gaussen'] = get_map_tile_url(recent_gaussen, {'min': -40, 'max': 280, 'palette': ['#7f0000', '#d73027', '#f46d43', '#fee08b', '#a7f3d0', '#38bdf8', '#0284c7', '#1e3a8a']})
urls['estres'] = get_map_tile_url(recent_estres, {'min': 0, 'max': 8, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})
urls['martonne'] = get_map_tile_url(recent_martonne, {'min': 10, 'max': 130, 'palette': ['#d7191c', '#fdae61', '#ffffbf', '#a6d96a', '#38bdf8', '#1d4ed8', '#1e3a8a']})
urls['suscSequia'] = get_map_tile_url(recent_susc_sequia, {'min': 0.1, 'max': 0.8, 'palette': ['#006837', '#31a354', '#78c679', '#addd8e', '#d9ef8b', '#fee08b', '#fdae61', '#f46d43', '#d73027', '#a50026']})
urls['suscHelada'] = get_map_tile_url(recent_temp_min, {'min': -2, 'max': 18, 'palette': ['#08306b', '#2171b5', '#6baed6', '#bdd7e7', '#fee08b', '#fdae61', '#a0562e']})
urls['suscDeslizamiento'] = get_map_tile_url(recent_susc_desliz, {'min': 0.1, 'max': 0.85, 'palette': ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027', '#7f0000']})
urls['calidadAire'] = get_map_tile_url(recent_cams_air, {'min': -0.5, 'max': 3.5, 'palette': ['#22c55e', '#a3e635', '#fef08a', '#f97316', '#ef4444', '#7f1d1d']})
urls['isotermas'] = get_map_tile_url(isotermas_edges.selfMask(), {'min': 0, 'max': 1, 'palette': ['#fef08a', '#f97316']})
urls['isoyetas'] = get_map_tile_url(isoyetas_edges.selfMask(), {'min': 0, 'max': 1, 'palette': ['#e0f2fe', '#0284c7']})

# Malla GEE_RASTER_GRID
coarse_elev_ref = srtm_30m.subtract(delta_elevation)
grid_multiband = ee.Image.cat([
    recent_chirps_raw.unmask(0).rename('precip'),
    monthly_precip_raw.unmask(0).rename('precipMensual'),
    temp_base_c.unmask(22).rename('temp_base'),
    temp_min_base_c.unmask(15).rename('temp_min_base'),
    dew_base_c.unmask(16).rename('dew_base'),
    wind_base_kt.unmask(5).rename('wind_base'),
    soil1_base.unmask(0.35).rename('soil1_base'),
    soil2_base.unmask(0.36).rename('soil2_base'),
    recent_ndvi_raw.unmask(0.5).rename('ndvi'),
    et_real_base.unmask(25).rename('et_real_base'),
    et_pot_base.unmask(55).rename('et_pot_base'),
    recent_susc_desliz.unmask(0.2).rename('susc_desliz'),
    recent_cams_air.unmask(0.5).rename('calidad_aire'),
    coarse_elev_ref.unmask(500).rename('coarse_elev')
]).clip(inab_buffer_geom)

scale_grid = 9000
rect = grid_multiband.reduceRegion(
    reducer=ee.Reducer.toList(),
    geometry=grid_geom,
    scale=scale_grid,
    maxPixels=1e7
).getInfo()

sample_arr = rect.get('precip', [])
n_total = len(sample_arr)
cols = int(round((grid_bbox[2] - grid_bbox[0]) * 111320 * 0.96 / scale_grid))
rows = n_total // cols if cols > 0 else 0

def round_grid(flat_list, decimals=1):
    matrix = []
    for r in range(rows):
        row_slice = flat_list[r * cols:(r + 1) * cols]
        matrix.append([round(v, decimals) if v is not None else 0.0 for v in row_slice])
    return matrix

grid_obj = {
    'bbox': grid_bbox,
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
    'susc_desliz': round_grid(rect.get('susc_desliz', []), 2),
    'calidad_aire': round_grid(rect.get('calidad_aire', []), 2),
    'coarse_elev': round_grid(rect['coarse_elev'], 0)
}
print(f"✓ Malla GEE_RASTER_GRID generada con {rows}x{cols} celdas.", flush=True)

out_bundle = {
    'urls': urls,
    'grid': grid_obj,
    'inab_coords': inab_coords
}
with open(r"C:\Users\mercy.aguilar\.gemini\antigravity\brain\e84d3f6a-800e-4bd6-a4ba-dd09d0461c2f\scratch\fresh_inab_buffer_gee.json", "w", encoding="utf-8") as f:
    json.dump(out_bundle, f)

print("✓ Proceso completado exitosamente y exportado a fresh_inab_buffer_gee.json", flush=True)
